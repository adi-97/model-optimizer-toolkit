import hashlib
import json
import os
import shlex
import sys

from ..layers import CONFIG, TEMPLATE, plan
from ..layers import load as load_layers

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
BEGIN = "<!-- adtok:begin -->"
END = "<!-- adtok:end -->"
MARKER = "adtok.cli hook"
MANIFEST = os.path.join(".claude", "adtok.json")
PROFILES = ("native", "hybrid")

ENV = {
    "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5",
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
}


def agent_templates():
    folder = os.path.join(TEMPLATES, "agents")
    return sorted(f for f in os.listdir(folder) if f.endswith(".md"))


def read_template(*parts):
    with open(os.path.join(TEMPLATES, *parts), encoding="utf-8") as f:
        return f.read()


def hook_command(python, event, url, extra=""):
    quoted = '"%s"' % python if os.name == "nt" else shlex.quote(python)
    return ("%s -m adtok.cli hook %s --url %s %s" % (quoted, event, url, extra)).strip()


def merge_claude_md(path, block):
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    wrapped = "%s\n%s%s\n" % (BEGIN, block if block.endswith("\n") else block + "\n", END)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        text = head + wrapped + ("\n" + tail if tail else "")
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + wrapped
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def strip_claude_md(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if BEGIN not in text or END not in text:
        return
    head, rest = text.split(BEGIN, 1)
    tail = rest.split(END, 1)[1]
    text = (head.rstrip("\n") + "\n" + tail.lstrip("\n")).strip("\n")
    if text:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    else:
        os.remove(path)


def ours(entry):
    return any(MARKER in (h.get("command") or "") for h in entry.get("hooks", []))


def merge_settings(path, env, hooks, keys=None):
    settings = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
    kept = []
    for store, wanted in ((settings.setdefault("env", {}), env), (settings, keys or {})):
        for key, value in wanted.items():
            if key in store and store[key] != value:
                kept.append(key)
            else:
                store[key] = value
    if not settings["env"]:
        settings.pop("env")
    all_hooks = settings.setdefault("hooks", {})
    for event in list(all_hooks):
        all_hooks[event] = [e for e in all_hooks[event] if not ours(e)]
        if not all_hooks[event]:
            all_hooks.pop(event)
    for event, matcher, command, timeout in hooks:
        entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        all_hooks.setdefault(event, []).append(entry)
    if not all_hooks:
        settings.pop("hooks")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    return kept


def strip_settings(path, env_keys, keys=()):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)
    for key in env_keys:
        settings.get("env", {}).pop(key, None)
    for key in keys:
        settings.pop(key, None)
    if settings.get("env") == {}:
        settings.pop("env")
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not ours(e)]
        if not hooks[event]:
            hooks.pop(event)
    if hooks == {}:
        settings.pop("hooks", None)
    if settings:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
    else:
        os.remove(path)


def scope_files(target, user):
    if user:
        return os.path.join(target, ".claude", "CLAUDE.md"), os.path.join(target, ".claude", "settings.json")
    return os.path.join(target, "CLAUDE.md"), os.path.join(target, ".claude", "settings.local.json")


def install(target, profile=None, url="http://127.0.0.1:8787", python=None, force=False, switch_ask_usd=None,
            layers=None, user=False):
    claude_md, settings_path = scope_files(target, user)
    overrides = dict(layers or {})
    routing = dict(overrides.get("routing", {}))
    if profile:
        routing["profile"] = profile
    if switch_ask_usd is not None:
        routing["switch_ask_usd"] = switch_ask_usd
    overrides["routing"] = routing
    if os.path.exists(os.path.join(target, MANIFEST)):
        uninstall(target, keep_config=True, user=user)
    cfg = load_layers(target, overrides)
    profile = cfg["routing"]["profile"]
    if profile not in PROFILES:
        raise ValueError("profile must be one of %s" % (PROFILES,))
    python = python or sys.executable
    keys, env, layer_hooks = plan(cfg)
    hooks = [(event, matcher, hook_command(python, name, url, extra), 10) for event, matcher, name, extra in layer_hooks]
    written, skipped = [], []
    if cfg["routing"]["enabled"]:
        written, skipped = write_agents(target, force)
        merge_claude_md(claude_md, read_template("claude_md_block.md"))
        env = dict(ENV, **env)
        if profile == "hybrid" and cfg["routing"]["classifier"]:
            hooks.append(("UserPromptSubmit", None, hook_command(python, "user-prompt", url), 5))
        if profile == "hybrid":
            hooks += [("PreModelSwitch", None, hook_command(python, "pre-model-switch", url, "--ask-above-usd %.2f"
                                                            % cfg["routing"]["switch_ask_usd"]), 5)]
    kept = merge_settings(settings_path, env, hooks, keys)
    manifest = {"profile": profile, "scope": "user" if user else "project", "agents": written,
                "agent_sha": {n: digest_of(target, n) for n in written}, "env": [k for k in env if k not in kept],
                "settings": [k for k in keys if k not in kept], "url": url}
    with open(os.path.join(target, MANIFEST), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    return {"agents_written": written, "agents_skipped": skipped, "env_kept_existing": kept, "profile": profile,
            "routing": cfg["routing"]["enabled"], "settings": keys, "env": env,
            "hooks": sorted({h[0] for h in hooks})}


def digest_of(target, name):
    with open(os.path.join(target, ".claude", "agents", name), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_agents(target, force):
    agents_dir = os.path.join(target, ".claude", "agents")
    os.makedirs(agents_dir, exist_ok=True)
    written, skipped = [], []
    for name in agent_templates():
        dest = os.path.join(agents_dir, name)
        content = read_template("agents", name)
        if os.path.exists(dest) and not force:
            with open(dest, encoding="utf-8") as f:
                if f.read() != content:
                    skipped.append(name)
                    continue
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        written.append(name)
    return written, skipped


def uninstall(target, keep_config=False, user=False):
    claude_md, settings_path = scope_files(target, user)
    path = os.path.join(target, MANIFEST)
    manifest = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    agents_dir = os.path.join(target, ".claude", "agents")
    removed = []
    for name in manifest.get("agents", []):
        dest = os.path.join(agents_dir, name)
        if os.path.exists(dest) and (open(dest, encoding="utf-8").read() == read_template("agents", name)
                                     or manifest.get("agent_sha", {}).get(name) == digest_of(target, name)):
            os.remove(dest)
            removed.append(name)
    if os.path.isdir(agents_dir) and not os.listdir(agents_dir):
        os.rmdir(agents_dir)
    strip_claude_md(claude_md)
    strip_settings(settings_path, manifest.get("env", list(ENV)),
                   manifest.get("settings", []))
    if os.path.exists(path):
        os.remove(path)
    config = os.path.join(target, CONFIG)
    if not keep_config and os.path.exists(config) and open(config, encoding="utf-8").read() == TEMPLATE:
        os.remove(config)
    return {"agents_removed": removed}

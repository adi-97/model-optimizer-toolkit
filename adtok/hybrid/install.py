import hashlib
import json
import os
import shlex
import shutil
import sys
import time

from ..layers import CONFIG, TEMPLATE, plan, write_atomic
from ..layers import load as load_layers
from ..layers import save as save_layers
from ..models import LATEST

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
BEGIN = "<!-- adtok:begin -->"
END = "<!-- adtok:end -->"
MARKER = "adtok.cli hook"
MANIFEST = os.path.join(".claude", "adtok.json")
KEEP_BACKUPS = 5

ENV = {
    "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": LATEST["haiku"],
    "ANTHROPIC_DEFAULT_SONNET_MODEL": LATEST["sonnet"],
    "ANTHROPIC_DEFAULT_OPUS_MODEL": LATEST["opus"],
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
}


def agent_templates():
    folder = os.path.join(TEMPLATES, "agents")
    return sorted(f for f in os.listdir(folder) if f.endswith(".md"))


def read_template(*parts):
    with open(os.path.join(TEMPLATES, *parts), encoding="utf-8") as f:
        return f.read()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        data = json.loads(read_text(path) or "{}")
    except ValueError as e:
        raise ValueError("%s is not valid JSON (%s); fix it and run init again" % (path, e)) from None
    if not isinstance(data, dict):
        raise ValueError("%s must hold a JSON object" % path)
    return data


def write_json(path, data):
    write_atomic(path, json.dumps(data, indent=2) + "\n")


def hook_command(python, event, url, extra=""):
    quoted = '"%s"' % python if os.name == "nt" else shlex.quote(python)
    return ("%s -m adtok.cli hook %s --url %s %s" % (quoted, event, url, extra)).strip()


def merge_claude_md(path, block):
    text = read_text(path) if os.path.exists(path) else ""
    wrapped = "%s\n%s%s\n" % (BEGIN, block if block.endswith("\n") else block + "\n", END)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        text = head + wrapped + ("\n" + tail if tail else "")
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + wrapped
    write_atomic(path, text)


def strip_claude_md(path):
    if not os.path.exists(path):
        return
    text = read_text(path)
    if BEGIN not in text or END not in text:
        return
    head, rest = text.split(BEGIN, 1)
    tail = rest.split(END, 1)[1]
    text = (head.rstrip("\n") + "\n" + tail.lstrip("\n")).strip("\n")
    if text:
        write_atomic(path, text + "\n")
    else:
        os.remove(path)


def ours(entry):
    return any(MARKER in (h.get("command") or "") for h in entry.get("hooks", []))


def merge_settings(path, env, hooks, keys=None):
    settings = read_json(path)
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
    write_json(path, settings)
    return kept


def strip_values(store, installed):
    if isinstance(installed, dict):
        for key, value in installed.items():
            if store.get(key) == value:
                store.pop(key)
    else:
        for key in installed:
            store.pop(key, None)


def strip_settings(path, env, keys=()):
    if not os.path.exists(path):
        return
    settings = read_json(path)
    strip_values(settings.get("env", {}), env)
    strip_values(settings, keys)
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
        write_json(path, settings)
    else:
        os.remove(path)


def merge_overrides(path, wanted):
    settings = read_json(path)
    current = settings.setdefault("skillOverrides", {})
    added = {name: mode for name, mode in wanted.items() if name not in current}
    current.update(added)
    if not current:
        settings.pop("skillOverrides")
    if added:
        write_json(path, settings)
    return added


def strip_overrides(path, ours_map):
    if not ours_map or not os.path.exists(path):
        return
    settings = read_json(path)
    current = settings.get("skillOverrides", {})
    strip_values(current, ours_map)
    if "skillOverrides" in settings and not current:
        settings.pop("skillOverrides")
    if settings:
        write_json(path, settings)
    else:
        os.remove(path)


def scope_files(target, user):
    if user:
        return os.path.join(target, ".claude", "CLAUDE.md"), os.path.join(target, ".claude", "settings.json")
    return os.path.join(target, "CLAUDE.md"), os.path.join(target, ".claude", "settings.local.json")


def install(target, profile=None, url="http://127.0.0.1:8787", python=None, force=False, switch_ask_usd=None,
            layers=None, user=False, save=None):
    claude_md, settings_path = scope_files(target, user)
    overrides = dict(layers or {})
    routing = dict(overrides.get("routing", {}))
    if profile:
        routing["profile"] = profile
    if switch_ask_usd is not None:
        routing["switch_ask_usd"] = switch_ask_usd
    overrides["routing"] = routing
    cfg = load_layers(target, overrides, create=False)
    read_json(settings_path)
    manifest_path = os.path.join(target, MANIFEST)
    read_json(manifest_path)
    if save:
        save_layers(target, save)
    backup = backup_settings(settings_path) if user else None
    if os.path.exists(manifest_path):
        uninstall(target, keep_config=True, user=user)
    cfg = load_layers(target, overrides)
    profile = cfg["routing"]["profile"]
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
    pruned = {}
    if cfg["skills"]["prune_unused"]:
        from ..skills import prune_plan

        pruned = merge_overrides(settings_path, prune_plan(target, user, int(cfg["skills"]["unused_days"]),
                                                           cfg["skills"]["mode"]))
    manifest = {"profile": profile, "scope": "user" if user else "project", "agents": written,
                "agent_sha": {n: digest_of(target, n) for n in written},
                "env": {k: v for k, v in env.items() if k not in kept},
                "settings": {k: v for k, v in keys.items() if k not in kept}, "skill_overrides": pruned, "url": url}
    write_json(manifest_path, manifest)
    return {"agents_written": written, "agents_skipped": skipped, "env_kept_existing": kept, "profile": profile,
            "routing": cfg["routing"]["enabled"], "settings": keys, "env": env, "skill_overrides": pruned,
            "hooks": sorted({h[0] for h in hooks}), "backup": backup}


def backup_settings(path, keep=KEEP_BACKUPS):
    if not os.path.exists(path):
        return None
    folder, prefix = os.path.dirname(path), os.path.basename(path) + ".adtok-"
    dest = os.path.join(folder, "%s%s.bak" % (prefix, time.strftime("%Y%m%d-%H%M%S")))
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(folder, "%s%s-%d.bak" % (prefix, time.strftime("%Y%m%d-%H%M%S"), n))
    shutil.copyfile(path, dest)
    old = sorted((f for f in os.listdir(folder) if f.startswith(prefix) and f.endswith(".bak")),
                 key=lambda f: (os.path.getmtime(os.path.join(folder, f)), f))
    for name in old[:-keep]:
        if os.path.join(folder, name) != dest:
            os.remove(os.path.join(folder, name))
    return dest


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
        if os.path.exists(dest) and not force and read_text(dest) != content:
            skipped.append(name)
            continue
        write_atomic(dest, content)
        written.append(name)
    return written, skipped


def uninstall(target, keep_config=False, user=False):
    claude_md, settings_path = scope_files(target, user)
    path = os.path.join(target, MANIFEST)
    manifest = read_json(path)
    read_json(settings_path)
    agents_dir = os.path.join(target, ".claude", "agents")
    removed = []
    for name in manifest.get("agents", []):
        dest = os.path.join(agents_dir, name)
        if os.path.exists(dest) and (read_text(dest) == read_template("agents", name)
                                     or manifest.get("agent_sha", {}).get(name) == digest_of(target, name)):
            os.remove(dest)
            removed.append(name)
    if os.path.isdir(agents_dir) and not os.listdir(agents_dir):
        os.rmdir(agents_dir)
    strip_claude_md(claude_md)
    strip_overrides(settings_path, manifest.get("skill_overrides", {}))
    strip_settings(settings_path, manifest.get("env", list(ENV)), manifest.get("settings", []))
    if os.path.exists(path):
        os.remove(path)
    config = os.path.join(target, CONFIG)
    if not keep_config and os.path.exists(config) and read_text(config) == TEMPLATE:
        os.remove(config)
    return {"agents_removed": removed}

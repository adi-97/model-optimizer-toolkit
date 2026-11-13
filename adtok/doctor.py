import json
import os
import re

from .hybrid.install import BEGIN, END, MARKER
from .ledger import project_slug, projects_root

BUILTIN_STYLES = {"default", "explanatory", "learning", "concise"}
MAX_LINES = 200
MAX_SKILL_DESC = 1536
MAX_ON_DEMAND = 40000
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
IMPORT = re.compile(r"^\s*@(\S+)", re.MULTILINE)
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def tokens(text):
    return (len(text) + 3) // 4


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def frontmatter(text):
    match = FRONT.match(text or "")
    out = {}
    for line in (match.group(1) if match else "").splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip().strip("\"'")
    return out


def load_json(path):
    text = read(path)
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def memory_files(target, home):
    seen, out = set(), []
    folder = os.path.abspath(target)
    candidates = [os.path.join(home, "CLAUDE.md")]
    while True:
        candidates += [os.path.join(folder, "CLAUDE.md"), os.path.join(folder, ".claude", "CLAUDE.md"),
                       os.path.join(folder, "CLAUDE.local.md")]
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    for path in candidates:
        if path not in seen and os.path.exists(path):
            seen.add(path)
            out.append(path)
    return out


def expand_imports(path, depth=0, seen=None):
    seen = seen if seen is not None else set()
    text = COMMENT.sub("", read(path) or "")
    parts = [(path, text)]
    if depth >= 4:
        return parts
    for ref in IMPORT.findall(text):
        child = os.path.normpath(os.path.join(os.path.dirname(path), os.path.expanduser(ref)))
        if child not in seen and os.path.isfile(child):
            seen.add(child)
            parts += expand_imports(child, depth + 1, seen)
    return parts


def audit(target, home=None):
    home = home or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    components, findings = [], []

    def warn(layer, message):
        findings.append({"level": "warn", "layer": layer, "message": message})

    def info(layer, message):
        findings.append({"level": "info", "layer": layer, "message": message})

    for path in memory_files(target, home):
        for i, (part, text) in enumerate(expand_imports(path)):
            components.append({"kind": "import" if i else "memory", "path": part, "lines": text.count("\n"),
                               "tokens": tokens(text)})
            if text.count("\n") > MAX_LINES:
                warn("prefix", "%s has %d lines (keep each file under %d; move workflows to skills or path-scoped "
                     "rules)" % (part, text.count("\n"), MAX_LINES))
            if i == 1:
                info("prefix", "%s uses @imports; imported files load with it every session, so they save nothing"
                     % path)
            if re.search(r"!`", text):
                warn("cache", "%s embeds !`command` output, which changes between sessions" % part)
            raw = read(part) or ""
            if BEGIN in raw and END in raw:
                block = raw.split(BEGIN, 1)[1].split(END, 1)[0]
                components.append({"kind": "adtok block", "path": part, "lines": block.count("\n"),
                                   "tokens": tokens(block)})

    rules_dir = os.path.join(target, ".claude", "rules")
    for name in sorted(os.listdir(rules_dir)) if os.path.isdir(rules_dir) else []:
        text = read(os.path.join(rules_dir, name)) or ""
        if name.endswith(".md") and "paths" not in frontmatter(text):
            components.append({"kind": "rule", "path": os.path.join(rules_dir, name), "lines": text.count("\n"),
                               "tokens": tokens(text)})
        elif len(text) > MAX_ON_DEMAND:
            warn("prefix", "%s is over %d characters and is cut off when it loads" % (name, MAX_ON_DEMAND))

    memory = os.path.join(projects_root(), project_slug(target), "memory", "MEMORY.md")
    text = read(memory)
    if text is not None:
        components.append({"kind": "auto memory", "path": memory, "lines": text.count("\n"), "tokens": tokens(text)})
        if text.count("\n") > 200 or len(text.encode("utf-8")) > 25000:
            warn("prefix", "MEMORY.md is past 200 lines or 25KB; the rest is silently dropped")

    listing = 0
    for base in (os.path.join(target, ".claude"), home):
        for kind in ("skills", "commands"):
            folder = os.path.join(base, kind)
            for root, _, files in os.walk(folder) if os.path.isdir(folder) else []:
                for name in files:
                    if not name.endswith(".md") or (kind == "skills" and name != "SKILL.md"):
                        continue
                    fm = frontmatter(read(os.path.join(root, name)))
                    desc = fm.get("description", "")
                    if fm.get("disable-model-invocation") != "true":
                        listing += len(desc) + len(fm.get("name", name))
                    if len(desc) > MAX_SKILL_DESC:
                        warn("prefix", "%s description is %d characters (cap %d)" % (root, len(desc), MAX_SKILL_DESC))
                    if fm.get("model") and fm["model"] not in ("inherit",):
                        warn("cache", "%s sets model: %s; invoking it switches model and rebuilds the cache"
                             % (os.path.join(root, name), fm["model"]))
    if listing:
        components.append({"kind": "skill listing", "path": "skills + commands", "lines": 0,
                           "tokens": tokens("x" * listing)})

    settings = {}
    for path in (os.path.join(home, "settings.json"), os.path.join(target, ".claude", "settings.json"),
                 os.path.join(target, ".claude", "settings.local.json")):
        data = load_json(path)
        env = dict(settings.get("env", {}), **data.get("env", {}))
        hooks = settings.get("hooks", {})
        for event, entries in data.get("hooks", {}).items():
            hooks = dict(hooks, **{event: hooks.get(event, []) + entries})
        settings.update(data)
        settings["env"], settings["hooks"] = env, hooks
    env = settings["env"]

    if settings.get("model") == "opusplan" or env.get("ANTHROPIC_MODEL") == "opusplan":
        warn("cache", "opusplan switches model on every plan-mode toggle and rebuilds the cache each time")
    if env.get("FORCE_PROMPT_CACHING_5M"):
        warn("cache", "FORCE_PROMPT_CACHING_5M is set; idle gaps over 5 minutes re-write the whole context")
    if env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
        warn("safety", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS also disables tool search, context editing and "
             "cache-safe effort changes")
    if env.get("CLAUDE_CODE_EFFORT_LEVEL"):
        warn("safety", "CLAUDE_CODE_EFFORT_LEVEL=%s overrides every agent's effort: setting"
             % env["CLAUDE_CODE_EFFORT_LEVEL"])
    if str(settings.get("effortLevel")) == "max":
        warn("output", "effortLevel max can overthink; medium is the 5.5 default")
    if env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
        info("context", "CLAUDE_CODE_MAX_OUTPUT_TOKENS shrinks the room left before auto-compaction")
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    if base_url and not re.match(r"https?://(127\.0\.0\.1|localhost)", base_url):
        info("cache", "ANTHROPIC_BASE_URL=%s: check that the gateway forwards anthropic-beta and cache_control"
             % base_url)
    style = settings.get("outputStyle")
    if style and style.lower() not in BUILTIN_STYLES and not any(
            os.path.exists(os.path.join(d, "output-styles", style + ".md"))
            for d in (os.path.join(target, ".claude"), home)):
        warn("output", "outputStyle %r matches no file and silently falls back to Default" % style)
    if not settings.get("autoCompactWindow") and not env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
        info("context", "no autoCompactWindow: 1M-context models compact near 967K tokens; set e.g. 250000")

    mcp = load_json(os.path.join(target, ".mcp.json")).get("mcpServers", {})
    if str(env.get("ENABLE_TOOL_SEARCH", "")).lower() == "false" and len(mcp) > 1:
        warn("prefix", "tool search is off with %d MCP servers; every tool definition loads on every request"
             % len(mcp))
    for name, server in mcp.items():
        if isinstance(server, dict) and server.get("alwaysLoad"):
            info("prefix", "MCP server %s has alwaysLoad: its tools skip tool search" % name)

    rewriters = []
    for entry in settings["hooks"].get("PreToolUse", []):
        matcher = entry.get("matcher") or "*"
        for h in entry.get("hooks", []):
            command = h.get("command") or ""
            if MARKER not in command and re.search(r"\*|Bash|Read|Grep", matcher):
                rewriters.append("%s -> %s" % (matcher, command[:60]))
    ours = any(MARKER in (h.get("command") or "") for e in settings["hooks"].get("PreToolUse", [])
               for h in e.get("hooks", []))
    for r in rewriters:
        if "rtk" in r.lower() or ours:
            warn("safety", "another PreToolUse hook may rewrite tool input (%s); when two hooks rewrite the same "
                 "call, the last to finish wins" % r)

    agents_dir = os.path.join(target, ".claude", "agents")
    for name in sorted(os.listdir(agents_dir)) if os.path.isdir(agents_dir) else []:
        fm = frontmatter(read(os.path.join(agents_dir, name)))
        if fm.get("model", "").startswith("haiku") or "haiku" in fm.get("model", ""):
            window = fm.get("autoCompactWindow", "")
            if not window.isdigit() or int(window) > 100000:
                warn("context", "%s runs on Haiku without autoCompactWindow: 100000; prompts over 100K cost 5x"
                     % name)
        if fm.get("effort") == "max":
            warn("output", "%s uses effort: max" % name)

    return {"components": components, "findings": findings,
            "prefix_tokens": sum(c["tokens"] for c in components if c["kind"] != "adtok block")}

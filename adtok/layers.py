import copy
import json
import os
import re
import tempfile

CONFIG = os.path.join(".claude", "adtok.toml")
PROFILES = ("native", "hybrid")
SKILL_MODES = ("name-only", "user-invocable-only", "off")

DEFAULTS = {
    "routing": {"enabled": True, "profile": "hybrid", "classifier": True, "switch_ask_usd": 0.25},
    "context": {"compact_window": 250000, "handoff": True, "quiet_background": False},
    "tools": {"test_output": False, "read_guard": False, "read_max_lines": 2000, "grep_head_limit": 200,
              "output_caps": False, "bash_max_chars": 15000, "mcp_max_tokens": 10000},
    "output": {"concise": False},
    "skills": {"prune_unused": False, "unused_days": 30, "mode": "name-only"},
}

TEMPLATE = """[routing]
enabled = true
profile = "hybrid"
classifier = true
switch_ask_usd = 0.25

[context]
compact_window = 250000
handoff = true
quiet_background = false

[tools]
test_output = false
read_guard = false
read_max_lines = 2000
grep_head_limit = 200
output_caps = false
bash_max_chars = 15000
mcp_max_tokens = 10000

[output]
concise = false

[skills]
prune_unused = false
unused_days = 30
mode = "name-only"
"""


def merge(base, extra):
    out = copy.deepcopy(base)
    for section, values in (extra or {}).items():
        if section not in out or not isinstance(values, dict):
            raise ValueError("unknown adtok.toml section %r" % section)
        unknown = set(values) - set(out[section])
        if unknown:
            raise ValueError("unknown keys in [%s]: %s" % (section, ", ".join(sorted(unknown))))
        out[section].update(values)
    return out


def validate(cfg):
    for section, values in cfg.items():
        for key, value in values.items():
            default = DEFAULTS[section][key]
            number = isinstance(value, (int, float)) and not isinstance(value, bool)
            if isinstance(default, bool):
                ok, kind = isinstance(value, bool), "true or false"
            elif isinstance(default, int):
                ok, kind = number and isinstance(value, int) and value >= 0, "a whole number >= 0"
            elif isinstance(default, float):
                ok, kind = number and value >= 0, "a number >= 0"
            else:
                ok, kind = isinstance(value, str), "a string"
            if not ok:
                raise ValueError("%s.%s must be %s, got %r" % (section, key, kind, value))
    for name, value, allowed in (("routing.profile", cfg["routing"]["profile"], PROFILES),
                                 ("skills.mode", cfg["skills"]["mode"], SKILL_MODES)):
        if value not in allowed:
            raise ValueError("%s must be one of %s, got %r" % (name, ", ".join(allowed), value))
    return cfg


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))


def save(target, overrides):
    import tomllib

    path = os.path.join(target, CONFIG)
    merge(DEFAULTS, overrides)
    text = TEMPLATE
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    lines = text.splitlines()
    for section, values in overrides.items():
        header = re.compile(r"^\s*\[%s\]\s*(#.*)?$" % re.escape(section))
        start = next((i for i, line in enumerate(lines) if header.match(line)), None)
        if start is None:
            lines += ["", "[%s]" % section]
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
        for key, value in values.items():
            line = "%s = %s" % (key, toml_value(value))
            match = re.compile(r"^\s*%s\s*=" % re.escape(key))
            found = next((i for i in range(start + 1, end) if match.match(lines[i])), None)
            if found is None:
                lines.insert(start + 1, line)
                end += 1
            else:
                lines[found] = line
    text = "\n".join(lines) + "\n"
    validate(merge(DEFAULTS, tomllib.loads(text)))
    write_atomic(path, text)


def read_config(path):
    import tomllib

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError("%s is not valid TOML: %s" % (path, e)) from None


def load(target, overrides=None, create=True):
    path = os.path.join(target, CONFIG)
    data = {}
    if os.path.exists(path):
        data = read_config(path)
    elif create:
        write_atomic(path, TEMPLATE)
    return validate(merge(merge(DEFAULTS, data), overrides))


def plan(layers):
    settings, env, hooks = {}, {}, []
    ctx, tools, output = layers["context"], layers["tools"], layers["output"]
    if ctx["compact_window"]:
        settings["autoCompactWindow"] = int(ctx["compact_window"])
    if ctx["quiet_background"]:
        settings["crossSessionInbound"] = "hold"
        env["CLAUDE_CODE_GOAL_CHECKIN_MINUTES"] = "0"
    if tools["output_caps"]:
        settings["bashOutputMaxChars"] = int(tools["bash_max_chars"])
        env["MAX_MCP_OUTPUT_TOKENS"] = str(int(tools["mcp_max_tokens"]))
    if output["concise"]:
        settings["outputStyle"] = "Concise"
    if ctx["handoff"]:
        hooks += [("PreCompact", None, "pre-compact", ""), ("PostCompact", None, "post-compact", "")]
    tool_flags = []
    if tools["test_output"]:
        tool_flags.append("--test-output")
    if tools["read_guard"]:
        tool_flags += ["--read-max-lines %d" % tools["read_max_lines"], "--grep-head-limit %d" % tools["grep_head_limit"]]
    if tool_flags:
        hooks.append(("PreToolUse", "Bash|Read|Grep", "pre-tool", " ".join(tool_flags)))
    return settings, env, hooks

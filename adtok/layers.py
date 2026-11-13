import copy
import os
import tomllib

CONFIG = os.path.join(".claude", "adtok.toml")

DEFAULTS = {
    "routing": {"enabled": True, "profile": "hybrid", "classifier": True, "switch_ask_usd": 0.25},
    "context": {"compact_window": 250000, "handoff": True, "quiet_background": False},
    "tools": {"test_output": False, "read_guard": False, "read_max_lines": 2000, "grep_head_limit": 200,
              "output_caps": False, "bash_max_chars": 15000, "mcp_max_tokens": 10000},
    "output": {"concise": False},
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


def load(target, overrides=None, create=True):
    path = os.path.join(target, CONFIG)
    data = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = tomllib.load(f)
    elif create:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(TEMPLATE)
    return merge(merge(DEFAULTS, data), overrides)


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

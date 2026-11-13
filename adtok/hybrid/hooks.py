import json
import os
import re
import shlex
import socket
import sys
import urllib.parse
import urllib.request

MIN_WORDS = 5

TEST_COMMAND = re.compile(
    r"^\s*(?:\S*python(?:3|\.exe)?\s+(?:-m\s+(?:pytest|unittest)|\S*tests?\S*\.py)|pytest|py\.test|"
    r"(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:test|build|lint)|npx\s+(?:jest|vitest|tsc|eslint)|go\s+(?:test|build|vet)|"
    r"cargo\s+(?:test|build|clippy)|make(?:\s+(?:test|check|build))?|mvn\s+\S*(?:test|verify)|gradle\w*\s+test|"
    r"dotnet\s+(?:test|build)|tsc|ruff|eslint|mypy)(?:\s|$)")
UNSAFE = re.compile(r"[;&|<>`$()]|\n")
PRESERVE = ("When summarizing, keep: the user's current task and acceptance criteria; files changed and why; "
            "failing tests with their exact error lines; decisions made and approaches rejected; open next steps. "
            "Drop tool output that has been acted on.")

HINTS = {
    "simple": "Task tier: simple ({conf}). Do it directly and briefly; no plan and no subagents.",
    "mid": ("Task tier: mid ({conf}). A scoped change: locate the code (use Explore only if it spans many files), "
            "then make the change yourself, or hand it to implementer if it does not depend on this conversation. "
            "Verify with tests."),
    "complex": ("Task tier: complex ({conf}). Plan first. Use Explore for broad searches, delegate independent "
                "pieces that can run in parallel to implementer, and keep integration and decisions here."),
}


def read_event(stream=None):
    try:
        return json.load(stream or sys.stdin)
    except ValueError:
        return {}


def classify(url, text, timeout=3.0):
    parts = urllib.parse.urlsplit(url)
    socket.create_connection((parts.hostname, parts.port or 80), timeout=0.25).close()
    req =urllib.request.Request(url.rstrip("/") + "/__adtok/classify", method="POST",
                                 data=json.dumps({"text": text}).encode("utf-8"),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def user_prompt(event, url, classify_fn=classify):
    prompt = str(event.get("prompt") or "").strip()
    if len(prompt.split()) < MIN_WORDS or prompt.startswith("/") or event.get("agent_id"):
        return None
    try:
        result = classify_fn(url, prompt)
    except Exception:
        return None
    tier = result.get("tier")
    if tier not in HINTS:
        return None
    conf = "confidence %.2f%s" % (float(result.get("confidence") or 0.0),
                                 ", raised one tier because the classifier was unsure" if result.get("escalated") else "")
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": HINTS[tier].format(conf=conf)}}


def pre_model_switch(event, ask_above_usd):
    cost = event.get("estimated_cache_write_usd")
    if not event.get("prompt_cache_warm") or cost is None or float(cost) <= ask_above_usd:
        return None
    reason = ("Switching %s -> %s re-writes about %s tokens of cached context (about $%.2f at list price) because "
              "each model has its own prompt cache. For a different model on part of the work, a subagent is cheaper."
              % (event.get("from_model"), event.get("to_model"), event.get("context_tokens"), float(cost)))
    return {"hookSpecificOutput": {"hookEventName": "PreModelSwitch", "permissionDecision": "ask",
                                   "permissionDecisionReason": reason}}


def wrap_test_command(command, cwd, python=None):
    if not TEST_COMMAND.match(command) or UNSAFE.search(command) or "adtok.cli digest" in command:
        return None
    log_dir = os.path.join(cwd or ".", ".claude", "adtok-logs").replace("\\", "/")
    digest = "%s -m adtok.cli digest --log-dir %s" % (shlex.quote((python or sys.executable).replace("\\", "/")),
                                                       shlex.quote(log_dir))
    return "(set -o pipefail; (%s) 2>&1 | %s)" % (command, digest)


def line_count(path, stop):
    count = 0
    with open(path, "rb") as f:
        for count, _ in enumerate(f, 1):
            if count > stop:
                break
    return count


def pre_tool(event, test_output=False, read_max_lines=0, grep_head_limit=0, python=None):
    if event.get("permission_mode") == "auto":
        return None
    name, given = event.get("tool_name"), event.get("tool_input") or {}
    updated = None
    if name == "Bash" and test_output:
        wrapped = wrap_test_command(str(given.get("command") or ""), event.get("cwd"), python)
        if wrapped:
            updated = dict(given, command=wrapped)
    elif name == "Read" and read_max_lines and not given.get("limit"):
        path = str(given.get("file_path") or "")
        try:
            big = os.path.getsize(path) > read_max_lines * 8 and line_count(path, read_max_lines) > read_max_lines
        except OSError:
            big = False
        if big:
            updated = {k: v for k, v in given.items() if k != "allow_large"}
            updated["limit"] = read_max_lines
    elif name == "Grep" and grep_head_limit and given.get("output_mode") == "content" and not given.get("head_limit"):
        updated = dict(given, head_limit=grep_head_limit)
    if updated is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


def pre_compact(event):
    lines = [PRESERVE]
    if event.get("custom_instructions"):
        lines.append(str(event["custom_instructions"]))
    return "\n".join(lines)


def post_compact(event):
    summary = event.get("compact_summary")
    if not summary:
        return None
    path = os.path.join(event.get("cwd") or ".", ".claude", "adtok-handoff.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Handoff (%s, %s)\n\n%s\n" % (event.get("session_id"), event.get("trigger"), summary))
    return None


def run(event_name, url, ask_above_usd=0.25, stream=None, out=None, **opts):
    event = read_event(stream)
    if event_name == "user-prompt":
        result = user_prompt(event, url)
    elif event_name == "pre-model-switch":
        result = pre_model_switch(event, ask_above_usd)
    elif event_name == "pre-tool":
        result = pre_tool(event, **opts)
    elif event_name == "pre-compact":
        result = pre_compact(event)
    elif event_name == "post-compact":
        result = post_compact(event)
    else:
        raise ValueError("unknown hook %r" % event_name)
    if result:
        (out or sys.stdout).write(result if isinstance(result, str) else json.dumps(result))
    return 0

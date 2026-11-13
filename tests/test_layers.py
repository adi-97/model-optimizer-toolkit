import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adtok import ledger
from adtok.digest import digest
from adtok.doctor import audit
from adtok.hybrid import install, uninstall
from adtok.hybrid.hooks import post_compact, pre_compact, pre_tool, run, wrap_test_command
from adtok.layers import CONFIG, DEFAULTS, load, merge


def settings_of(target):
    with open(os.path.join(target, ".claude", "settings.local.json"), encoding="utf-8") as f:
        return json.load(f)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def test_config_created_and_validated():
    target = tempfile.mkdtemp()
    cfg = load(target)
    assert os.path.exists(os.path.join(target, CONFIG)) and cfg == DEFAULTS
    write(os.path.join(target, CONFIG), "[tools]\ntest_output = true\n")
    assert load(target)["tools"]["test_output"] is True and load(target)["context"]["handoff"] is True
    for bad in ({"tools": {"nope": 1}}, {"nope": {}}):
        try:
            merge(DEFAULTS, bad)
            raise AssertionError("accepted %r" % bad)
        except ValueError:
            pass


def test_install_default_layers_and_uninstall_restores():
    target = tempfile.mkdtemp()
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps({"permissions": {"allow": ["Read"]}}))
    result = install(target, python="/py")
    s = settings_of(target)
    assert s["autoCompactWindow"] == 250000 and "outputStyle" not in s and "bashOutputMaxChars" not in s
    assert set(s["hooks"]) == {"UserPromptSubmit", "PreModelSwitch", "PreCompact", "PostCompact"}
    assert "adtok.cli hook pre-compact" in s["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    assert result["routing"] and "PreToolUse" not in s["hooks"]
    uninstall(target)
    assert settings_of(target) == {"permissions": {"allow": ["Read"]}}
    assert not os.path.exists(os.path.join(target, CONFIG))
    assert not os.path.exists(os.path.join(target, "CLAUDE.md"))


def test_install_tools_keeps_user_values_and_reinstall_follows_config():
    target = tempfile.mkdtemp()
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps({"autoCompactWindow": 500000}))
    result = install(target, python="/py", layers={"tools": {"test_output": True, "output_caps": True},
                                                   "output": {"concise": True}})
    s = settings_of(target)
    assert s["autoCompactWindow"] == 500000 and "autoCompactWindow" in result["env_kept_existing"]
    assert s["bashOutputMaxChars"] == 15000 and s["env"]["MAX_MCP_OUTPUT_TOKENS"] == "10000"
    assert s["outputStyle"] == "Concise"
    entry = s["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash|Read|Grep" and "--test-output" in entry["hooks"][0]["command"]
    install(target, python="/py", layers={"routing": {"enabled": False}})
    s = settings_of(target)
    assert "PreToolUse" not in s.get("hooks", {}) and "UserPromptSubmit" not in s.get("hooks", {})
    assert "outputStyle" not in s and "CLAUDE_CODE_SUBAGENT_MODEL" not in s.get("env", {})
    assert s["autoCompactWindow"] == 500000
    assert not os.path.exists(os.path.join(target, ".claude", "agents"))
    uninstall(target)
    assert settings_of(target) == {"autoCompactWindow": 500000}


def test_classifier_switch_controls_tier_hook():
    target = tempfile.mkdtemp()
    install(target, python="/py", layers={"routing": {"classifier": False}})
    hooks = settings_of(target)["hooks"]
    assert "UserPromptSubmit" not in hooks and "PreModelSwitch" in hooks
    assert os.path.exists(os.path.join(target, ".claude", "agents", "Explore.md"))
    install(target, python="/py")
    assert "UserPromptSubmit" in settings_of(target)["hooks"]


def test_reinstall_upgrades_agents_it_wrote():
    target = tempfile.mkdtemp()
    install(target, python="/py")
    path = os.path.join(target, ".claude", "agents", "Explore.md")
    manifest = os.path.join(target, ".claude", "adtok.json")
    data = json.load(open(manifest, encoding="utf-8"))
    write(path, "---\nname: Explore\nmodel: haiku\n---\nold template\n")
    data["agent_sha"]["Explore.md"] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    write(manifest, json.dumps(data))
    result = install(target, python="/py")
    assert "Explore.md" in result["agents_written"] and "autoCompactWindow: 100000" in open(path).read()
    write(path, "mine\n")
    assert "Explore.md" in install(target, python="/py")["agents_skipped"]


def test_pre_tool_bash_wraps_only_allowlisted_and_keeps_fields():
    ev = {"tool_name": "Bash", "cwd": "/w", "tool_input": {"command": "pytest -q tests", "timeout": 600000}}
    out = pre_tool(ev, test_output=True, python="/py")["hookSpecificOutput"]["updatedInput"]
    assert out["timeout"] == 600000 and out["command"].startswith("(set -o pipefail; (pytest -q tests) 2>&1 |")
    assert "adtok.cli digest --log-dir /w/.claude/adtok-logs" in out["command"]
    for cmd in ("npm publish", "rm -rf build", "pytest; curl x", "pytest | tee log", "git status"):
        assert pre_tool(dict(ev, tool_input={"command": cmd}), test_output=True) is None, cmd
    assert pre_tool(dict(ev, permission_mode="auto"), test_output=True) is None
    assert pre_tool(ev, test_output=False) is None
    for cmd in (".venv/Scripts/python tests/test_proxy.py", "python -m pytest -x", "go test ./...", "npm run build"):
        assert wrap_test_command(cmd, "/w", "/py"), cmd


def test_wrapped_command_keeps_exit_code_and_digests():
    bash = shutil.which("bash")
    if not bash:
        print("  (skipped: no bash)")
        return
    work = tempfile.mkdtemp()
    script = os.path.join(work, "test_noisy.py")
    write(script, "import sys\nfor i in range(3000):\n    print('line %d ok' % i)\nprint('AssertionError: boom')\n"
                  "sys.exit(3)\n")
    py = sys.executable.replace("\\", "/")
    cmd = wrap_test_command("%s %s" % (py, script.replace("\\", "/")), work, py)
    proc = subprocess.run([bash, "-c", cmd], capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 3, (proc.returncode, proc.stderr)
    assert "AssertionError: boom" in proc.stdout and "lines omitted; full log at" in proc.stdout
    assert len(proc.stdout) < 8000
    log = proc.stdout.strip().rsplit("full log at ", 1)[1]
    assert "line 1500 ok" in open(log, encoding="utf-8").read()


def test_digest_threshold():
    d = tempfile.mkdtemp()
    assert digest("short\n", d) == ("short\n", None)
    text = "\n".join(["x" * 50] * 400 + ["FAILED test_a - KeyError"] + ["y" * 50] * 100)
    shown, path = digest(text, d, tail=20)
    assert path and os.path.exists(path) and "FAILED test_a" in shown and shown.count("\n") < 40


def test_pre_tool_read_and_grep_guard():
    work = tempfile.mkdtemp()
    big, small = os.path.join(work, "big.py"), os.path.join(work, "small.py")
    write(big, "x = 1\n" * 5000)
    write(small, "x = 1\n" * 10)
    ev = {"tool_name": "Read", "tool_input": {"file_path": big, "allow_large": True}}
    out = pre_tool(ev, read_max_lines=2000)["hookSpecificOutput"]["updatedInput"]
    assert out == {"file_path": big, "limit": 2000}
    assert pre_tool(dict(ev, tool_input={"file_path": big, "limit": 50}), read_max_lines=2000) is None
    assert pre_tool(dict(ev, tool_input={"file_path": small}), read_max_lines=2000) is None
    assert pre_tool(dict(ev, tool_input={"file_path": os.path.join(work, "nope")}), read_max_lines=2000) is None
    g = {"tool_name": "Grep", "tool_input": {"pattern": "x", "output_mode": "content"}}
    assert pre_tool(g, grep_head_limit=200)["hookSpecificOutput"]["updatedInput"]["head_limit"] == 200
    assert pre_tool(dict(g, tool_input={"pattern": "x"}), grep_head_limit=200) is None


def test_compact_hooks():
    work = tempfile.mkdtemp()
    assert "failing tests" in pre_compact({"trigger": "auto"})
    assert "keep the API notes" in pre_compact({"custom_instructions": "keep the API notes"})
    post_compact({"cwd": work, "compact_summary": "did X, next Y", "session_id": "s1", "trigger": "auto"})
    assert "did X, next Y" in open(os.path.join(work, ".claude", "adtok-handoff.md"), encoding="utf-8").read()
    out = io.StringIO()
    run("pre-compact", "u", stream=io.StringIO(json.dumps({"trigger": "manual"})), out=out)
    assert "acceptance criteria" in out.getvalue()


def assistant(session, mid, req, model, ts, usage, sidechain=False, agent=None, version="2.1.322"):
    return {"type": "assistant", "sessionId": session, "requestId": req, "isSidechain": sidechain, "agentId": agent,
            "timestamp": ts, "version": version, "message": {"id": mid, "model": model, "usage": usage}}


def u(inp=0, w1h=0, w5m=0, read=0, out=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": w1h + w5m, "cache_read_input_tokens": read,
            "output_tokens": out, "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m}}


def transcripts():
    root = tempfile.mkdtemp()
    folder = os.path.join(root, ledger.project_slug("/proj"))
    opus, sonnet = "claude-opus-5-5", "claude-sonnet-5-5"
    main = [
        assistant("s1", "m1", "r1", opus, "2026-11-10T10:00:00Z", u(10, w1h=20000, out=1)),
        assistant("s1", "m1", "r1", opus, "2026-11-10T10:00:00Z", u(10, w1h=20000, out=300)),
        assistant("s1", "m2", "r2", opus, "2026-11-10T10:01:00Z", u(5, w1h=1000, read=20000, out=200)),
        assistant("s1", "m3", "r3", sonnet, "2026-11-10T10:02:00Z", u(5, w1h=21000, out=100)),
        assistant("s1", "m4", "r4", sonnet, "2026-11-10T12:00:00Z", u(5, w1h=21500, out=100)),
        {"type": "system", "subtype": "compact_boundary", "sessionId": "s1", "timestamp": "2026-11-10T12:30:00Z"},
        assistant("s1", "m5", "r5", sonnet, "2026-11-10T12:31:00Z", u(5, w1h=15000, out=100)),
        assistant("s1", "x", "rx", "<synthetic>", "2026-11-10T12:32:00Z", u(out=5)),
        {"type": "cost-state", "sessionId": "s1", "totalCostUSD": 1.0},
    ]
    sub = [assistant("s1", "a1", "ra1", "claude-haiku-5-5", "2026-11-10T10:01:30Z", u(100, w5m=5000, out=50),
                     sidechain=True, agent="ag1")]
    write(os.path.join(folder, "s1.jsonl"), "\n".join(json.dumps(r) for r in main) + "\nnot json\n")
    write(os.path.join(folder, "s1", "subagents", "agent-ag1.jsonl"), json.dumps(sub[0]) + "\n")
    write(os.path.join(folder, "s0.orphaned-1.jsonl"), json.dumps(main[0]) + "\n")
    return root


def test_ledger_dedups_prices_and_attributes_misses():
    root = transcripts()
    files = ledger.session_files("/proj", root=root)
    assert len(files) == 2
    rows, compactions, state = ledger.read_requests(files)
    assert len(rows) == 6 and state == {"s1": {"usd": 1.0, "since": 0.0}}
    first = rows[0]
    assert first["output"] == 300 and first["write_1h"] == 20000
    assert abs(first["cost"] - (10 * 4 + 20000 * 8 + 300 * 20) / 1e6) < 1e-9
    by_agent = {g["group"]: g for g in ledger.summarize(rows, "agent")}
    assert by_agent["subagent"]["requests"] == 1 and by_agent["main"]["requests"] == 5
    causes = [m["cause"] for m in sorted(ledger.misses(rows, compactions), key=lambda m: m["ts"])]
    assert causes[0].startswith("model switch claude-opus-5-5 -> claude-sonnet-5-5")
    assert causes[1].startswith("idle 118 min") and causes[2] == "compaction"
    advice = ledger.ttl_advice(rows)
    assert advice["cheaper"] in ("5m", "1h") and advice["cache_usd_1h"] > 0


def test_doctor_finds_prefix_cache_and_conflict_issues():
    target, home = tempfile.mkdtemp(), tempfile.mkdtemp()
    write(os.path.join(target, "CLAUDE.md"), "# Notes\n@docs/extra.md\n" + "line\n" * 250 + "<!-- " + "z" * 9000 + " -->\n")
    write(os.path.join(target, "docs", "extra.md"), "Run !`date` first\n")
    write(os.path.join(target, ".claude", "skills", "big", "SKILL.md"),
          "---\nname: big\ndescription: %s\nmodel: opus\n---\nbody\n" % ("d" * 1600))
    write(os.path.join(target, ".claude", "agents", "cheap.md"), "---\nname: cheap\nmodel: haiku\n---\nx\n")
    write(os.path.join(target, ".claude", "settings.json"), json.dumps({
        "model": "opusplan", "outputStyle": "Terse",
        "env": {"CLAUDE_CODE_EFFORT_LEVEL": "high", "FORCE_PROMPT_CACHING_5M": "1"},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook"}]}]}}))
    result = audit(target, home=home)
    text = "\n".join(f["message"] for f in result["findings"])
    for needle in ("lines (keep each file under 200", "@imports", "!`command`", "description is 1600", "sets model: opus", "opusplan",
                   "FORCE_PROMPT_CACHING_5M", "CLAUDE_CODE_EFFORT_LEVEL", "'Terse'", "rtk hook",
                   "cheap.md runs on Haiku", "no autoCompactWindow"):
        assert needle in text, needle
    memory = [c for c in result["components"] if c["kind"] == "memory"][0]
    assert memory["tokens"] < 1000
    clean = tempfile.mkdtemp()
    install(clean, python="/py")
    findings = audit(clean, home=home)["findings"]
    assert [f for f in findings if f["level"] == "warn"] == [], findings
    assert any(c["kind"] == "adtok block" for c in audit(clean, home=home)["components"])


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception:
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

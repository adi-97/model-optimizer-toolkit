import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adtok import ledger
from adtok.digest import digest
from adtok.doctor import audit
from adtok.hybrid import install, uninstall
from adtok.hybrid.hooks import post_compact, pre_compact, pre_tool, run, wrap_test_command
from adtok.hybrid.install import hook_command
from adtok.layers import CONFIG, DEFAULTS, load, merge, save


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


def test_user_scope_uses_global_files_and_restores_them():
    home = tempfile.mkdtemp()
    write(os.path.join(home, ".claude", "settings.json"), json.dumps({"permissions": {"allow": ["WebSearch"]}}))
    write(os.path.join(home, ".claude", "CLAUDE.md"), "# Mine\n")
    install(home, python="/py", user=True)
    s = json.load(open(os.path.join(home, ".claude", "settings.json"), encoding="utf-8"))
    assert s["autoCompactWindow"] == 250000 and "UserPromptSubmit" in s["hooks"] and s["permissions"]
    assert "## Model routing" in open(os.path.join(home, ".claude", "CLAUDE.md"), encoding="utf-8").read()
    assert os.path.exists(os.path.join(home, ".claude", "agents", "Explore.md"))
    assert not os.path.exists(os.path.join(home, "CLAUDE.md"))
    assert not os.path.exists(os.path.join(home, ".claude", "settings.local.json"))
    uninstall(home, user=True)
    assert json.load(open(os.path.join(home, ".claude", "settings.json"))) == {"permissions": {"allow": ["WebSearch"]}}
    assert open(os.path.join(home, ".claude", "CLAUDE.md"), encoding="utf-8").read() == "# Mine\n"
    assert not os.path.exists(os.path.join(home, ".claude", "agents"))


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
    assert not any("rtk hook" in f["message"] for f in audit(target, home=home)["findings"])
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash|Read|Grep", "hooks": [{"type": "command", "command": hook_command(sys.executable, "pre-tool",
                                                                                         "http://x", "--test-output")}]}]}}))
    result = audit(target, home=home)
    text = "\n".join(f["message"] for f in result["findings"])
    for needle in ("lines (keep each file under 200", "@imports", "!`command`", "description is 1600", "sets model: opus", "opusplan",
                   "FORCE_PROMPT_CACHING_5M", "CLAUDE_CODE_EFFORT_LEVEL", "'Terse'", "rtk hook",
                   "cheap.md runs on Haiku", "no autoCompactWindow"):
        assert needle in text, needle
    memory = [c for c in result["components"] if c["kind"] == "memory"][0]
    assert memory["tokens"] < 1000
    assert not any("cannot import adtok" in f["message"] or "does not exist" in f["message"] for f in result["findings"])
    clean = tempfile.mkdtemp()
    install(clean, python=sys.executable)
    findings = audit(clean, home=home)["findings"]
    assert [f for f in findings if f["level"] == "warn"] == [], findings
    assert any(c["kind"] == "adtok block" for c in audit(clean, home=home)["components"])
    install(clean, python=os.path.join(clean, "gone", "python"))
    broken = [f["message"] for f in audit(clean, home=home)["findings"] if f["layer"] == "hooks"]
    assert broken and all("does not exist" in m for m in broken), broken


def test_init_set_is_saved_to_config():
    target = tempfile.mkdtemp()
    write(os.path.join(target, CONFIG), "# mine\n[routing]\nclassifier = true  \n\n[tools]\nread_guard = false\n")
    install(target, python="/py", layers={"routing": {"classifier": False}},
            save={"routing": {"classifier": False}, "tools": {"read_max_lines": 500}, "output": {"concise": True}})
    text = open(os.path.join(target, CONFIG), encoding="utf-8").read()
    assert text.startswith("# mine\n") and "classifier = false" in text and "read_max_lines = 500" in text
    cfg = load(target)
    assert cfg["routing"]["classifier"] is False and cfg["output"]["concise"] is True
    install(target, python="/py")
    assert "UserPromptSubmit" not in settings_of(target)["hooks"]
    fresh = tempfile.mkdtemp()
    save(fresh, {"routing": {"classifier": False}})
    assert load(fresh)["routing"]["classifier"] is False and load(fresh)["context"]["handoff"] is True
    try:
        save(fresh, {"routing": {"nope": 1}})
        raise AssertionError("saved an unknown key")
    except ValueError:
        pass


def snapshot(root):
    out = {}
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            out[os.path.relpath(path, root)] = open(path, "rb").read()
    return out


def test_bad_values_are_rejected_before_any_write():
    target = tempfile.mkdtemp()
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps({"permissions": {"allow": ["Read"]}}))
    install(target, python="/py")
    before = snapshot(target)
    for bad in ({"skills": {"mode": "bogus"}}, {"routing": {"profile": "bogus"}}, {"tools": {"read_max_lines": "x"}},
                {"routing": {"enabled": "yes"}}, {"context": {"compact_window": -1}}):
        for kwargs in ({"layers": bad}, {"layers": bad, "save": bad}):
            try:
                install(target, python="/py", **kwargs)
                raise AssertionError("accepted %r" % bad)
            except ValueError:
                pass
            assert snapshot(target) == before, bad
    write(os.path.join(target, ".claude", "settings.local.json"), "{not json")
    before = snapshot(target)
    try:
        install(target, python="/py")
        raise AssertionError("accepted broken settings")
    except ValueError as e:
        assert "settings.local.json is not valid JSON" in str(e)
    assert snapshot(target) == before


def test_uninstall_keeps_values_changed_after_install():
    target = tempfile.mkdtemp()
    install(target, python="/py", layers={"tools": {"output_caps": True}})
    s = settings_of(target)
    s["autoCompactWindow"] = 300000
    s["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = "haiku"
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps(s))
    uninstall(target)
    assert settings_of(target) == {"autoCompactWindow": 300000, "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}}
    old = tempfile.mkdtemp()
    install(old, python="/py")
    manifest = os.path.join(old, ".claude", "adtok.json")
    data = json.load(open(manifest, encoding="utf-8"))
    data["env"], data["settings"] = list(data["env"]), list(data["settings"])
    write(manifest, json.dumps(data))
    uninstall(old)
    assert not os.path.exists(os.path.join(old, ".claude", "settings.local.json"))


def test_writes_leave_no_temp_files():
    target = tempfile.mkdtemp()
    install(target, python="/py")
    install(target, python="/py", save={"tools": {"read_guard": True}})
    uninstall(target)
    assert not [p for p in snapshot(target) if p.endswith(".tmp")], snapshot(target)


def test_user_install_backs_up_settings():
    home = tempfile.mkdtemp()
    path = os.path.join(home, ".claude", "settings.json")
    write(path, json.dumps({"permissions": {"allow": ["Read"]}}))
    result = install(home, python="/py", user=True)
    assert result["backup"] and json.load(open(result["backup"])) == {"permissions": {"allow": ["Read"]}}
    assert install(tempfile.mkdtemp(), python="/py")["backup"] is None
    for _ in range(7):
        install(home, python="/py", user=True)
    backups = [f for f in os.listdir(os.path.join(home, ".claude")) if ".adtok-" in f and f.endswith(".bak")]
    assert len(backups) == 5, backups
    assert not any(f.endswith(".tmp") for f in os.listdir(os.path.join(home, ".claude")))


def cli(argv, code=None):
    from contextlib import redirect_stderr, redirect_stdout
    from adtok.cli import main

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        result = main(argv)
    assert result == code, (result, out.getvalue())
    return out.getvalue()


def test_uninstall_without_manifest_keeps_user_values():
    target = tempfile.mkdtemp()
    mine = {"env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "my-own-model", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5"},
            "autoCompactWindow": 250000,
            "hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": hook_command("/py", "pre-compact", "u")}]},
                                     {"hooks": [{"type": "command", "command": "mine"}]}]}}
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps(mine))
    uninstall(target)
    assert settings_of(target) == dict(mine, hooks={"PreCompact": [{"hooks": [{"type": "command", "command": "mine"}]}]})


def test_old_manifest_upgrade_keeps_values_changed_since():
    target = tempfile.mkdtemp()
    install(target, python="/py")
    manifest = os.path.join(target, ".claude", "adtok.json")
    data = json.load(open(manifest, encoding="utf-8"))
    data["env"], data["settings"] = list(data["env"]), list(data["settings"])
    data.pop("installed_at")
    write(manifest, json.dumps(data))
    s = settings_of(target)
    s["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "my-own-model"
    s["autoCompactWindow"] = 400000
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps(s))
    result = install(target, python="/py", save={"context": {"compact_window": 300000}},
                     layers={"context": {"compact_window": 300000}})
    s = settings_of(target)
    assert s["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "my-own-model" and s["autoCompactWindow"] == 400000
    assert {"ANTHROPIC_DEFAULT_OPUS_MODEL", "autoCompactWindow"} <= set(result["env_kept_existing"])
    assert s["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"
    first = json.load(open(manifest, encoding="utf-8"))
    assert first["installed_at"] and isinstance(first["env"], dict)
    install(target, python="/py")
    assert json.load(open(manifest, encoding="utf-8"))["installed_at"] == first["installed_at"]


def test_failed_install_restores_previous_state():
    module = sys.modules["adtok.hybrid.install"]
    target = tempfile.mkdtemp()
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps({"permissions": {"allow": ["Read"]}}))
    install(target, python="/py")
    before = snapshot(target)
    real = module.write_agents

    def broken(*args):
        real(*args)
        raise OSError(28, "No space left on device")

    module.write_agents = broken
    try:
        out = cli(["init", target, "--set", "tools.read_guard=true"], code=2)
        assert "No space left" in out and "every file was restored" in out, out
        assert snapshot(target) == before
        fresh = tempfile.mkdtemp()
        cli(["init", fresh], code=2)
        assert snapshot(fresh) == {}, snapshot(fresh)
    finally:
        module.write_agents = real
    assert "nothing was changed" in cli(["init", target, "--set", "skills.mode=bad"], code=2)
    assert snapshot(target) == before


def test_cache_and_prefix_layers_install_and_uninstall():
    target = tempfile.mkdtemp()
    install(target, python="/py")
    assert "promptCacheTtl" not in settings_of(target)
    layers = {"cache": {"ttl": "1h", "subagent_ttl": "5m", "pin_updates": True},
              "prefix": {"trim_sections": True, "git_instructions": False}, "skills": {"max_desc_chars": 400}}
    install(target, python="/py", layers=layers, save=layers)
    s = settings_of(target)
    assert s["promptCacheTtl"] == "1h" and s["subagentPromptCacheTtl"] == "5m"
    assert s["skillListingMaxDescChars"] == 400
    for name in ("DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_CFC_PROMPT", "CLAUDE_CODE_DISABLE_WORKFLOWS",
                 "CLAUDE_CODE_DISABLE_CRON", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"):
        assert s["env"][name] == "1", name
    assert 'ttl = "1h"' in open(os.path.join(target, CONFIG), encoding="utf-8").read()
    findings = "\n".join(f["message"] for f in audit(target, home=tempfile.mkdtemp())["findings"])
    assert "promptCacheTtl is" not in findings
    uninstall(target, keep_config=True)
    assert not os.path.exists(os.path.join(target, ".claude", "settings.local.json"))
    try:
        install(target, python="/py", layers={"cache": {"ttl": "2h"}})
        raise AssertionError("accepted ttl 2h")
    except ValueError as e:
        assert "cache.ttl" in str(e)


def test_doctor_flags_parent_claude_md_and_bad_ttl():
    root, home = tempfile.mkdtemp(), tempfile.mkdtemp()
    target = os.path.join(root, "proj")
    write(os.path.join(root, "CLAUDE.md"), "unrelated notes\n")
    write(os.path.join(target, ".claude", "settings.json"), json.dumps({"promptCacheTtl": "2h",
                                                                         "idleCompaction": False}))
    text = "\n".join(f["message"] for f in audit(target, home=home)["findings"])
    assert "claudeMdExcludes" in text and "promptCacheTtl is '2h'" in text and "idleCompaction is off" in text
    write(os.path.join(target, ".claude", "settings.json"), json.dumps({
        "claudeMdExcludes": [os.path.join(root, "CLAUDE.md").replace("\\", "/")]}))
    assert "claudeMdExcludes" not in "\n".join(f["message"] for f in audit(target, home=home)["findings"])


def test_hooks_log_activity_and_ledger_compares_sessions():
    work = tempfile.mkdtemp()
    events = [("pre-tool", {"tool_name": "Grep", "tool_input": {"pattern": "x", "output_mode": "content"}},
               {"grep_head_limit": 50}),
              ("pre-model-switch", {"prompt_cache_warm": True, "estimated_cache_write_usd": 1.0}, {}),
              ("post-compact", {"compact_summary": "next: tests", "trigger": "auto"}, {}),
              ("pre-tool", {"tool_name": "Grep", "tool_input": {"pattern": "x"}}, {"grep_head_limit": 50})]
    for name, event, opts in events:
        event = dict(event, session_id="s1", cwd=work)
        run(name, "u", stream=io.StringIO(json.dumps(event)), out=io.StringIO(), **opts)
    run("pre-tool", "u", stream=io.StringIO(json.dumps({"tool_name": "Grep", "cwd": work, "tool_input": {
        "output_mode": "content"}})), out=io.StringIO(), grep_head_limit=50)
    hooks = ledger.read_activity(work)
    assert hooks == {"s1": {"rewrite": 1, "switch_ask": 1, "handoff": 1}}, hooks
    rows, compactions, _ = ledger.read_requests(ledger.session_files("/proj", root=transcripts()))
    late = dict(rows[-1], session="s2", ts=rows[-1]["ts"] + 86400, cost=0.01)
    rows.append(late)
    found = {s["session"]: s for s in ledger.activity(rows, compactions, hooks)}
    s1 = found["s1"]
    assert s1["main"] == 5 and s1["subagent"] == 1 and s1["compactions"] == 1
    assert s1["switch_asks"] == 1 and s1["rewrites"] == 1 and s1["handoffs"] == 1 and found["s2"]["hints"] == 0
    split = ledger.compare(rows, late["ts"] - 1)
    assert split["before"]["sessions"] == 1 and split["after"]["sessions"] == 1
    assert split["before"]["requests"] == 6 and abs(split["after"]["cost"] - 0.01) < 1e-12
    assert abs(split["before"]["subagent_share"] - 1 / 6) < 1e-9
    install(work, python="/py")
    ts, path = ledger.install_time(work, home=tempfile.mkdtemp())
    assert path.endswith("adtok.json") and abs(ts - __import__("time").time()) < 120


def test_ledger_compare_and_activity_cli():
    config = tempfile.mkdtemp()
    shutil.copytree(transcripts(), os.path.join(config, "projects"))
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = config
    try:
        out = cli(["ledger", "compare", "--project", "/proj", "--installed-at", "2026-11-10T11:00:00Z"], code=0)
        assert "from --installed-at" in out and "before" in out and "after" in out, out
        out = cli(["ledger", "activity", "--project", "/proj"], code=0)
        assert "s1" in out and "no hook activity logged" in out, out
        out = cli(["ledger", "ttl", "--project", "/proj"], code=0)
        assert "main conversation" in out and "subagents" in out and "--set cache.ttl=" in out, out
        try:
            cli(["ledger", "compare", "--project", os.path.join(config, "nowhere")])
            raise AssertionError("compare ran without an install time")
        except SystemExit as e:
            assert "installed_at" in str(e)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR")
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


def test_init_unset_restores_default_and_hook_path_stays_light():
    target = tempfile.mkdtemp()
    out = cli(["init", target, "--set", "tools.read_guard=true", "--set", "tools.read_max_lines=500"])
    assert "saved --set/--unset values" in out
    assert load(target)["tools"]["read_guard"] is True
    cli(["init", target, "--unset", "tools.read_guard", "--unset", "tools.read_max_lines"])
    cfg = load(target)
    assert cfg["tools"]["read_guard"] is False and cfg["tools"]["read_max_lines"] == 2000
    for bad in (["--unset", "tools.nope"], ["--set", "tools.read_guard=true", "--unset", "tools.read_guard"]):
        try:
            cli(["init", target] + bad)
            raise AssertionError("accepted %r" % bad)
        except SystemExit:
            pass
    code = ("import sys; from adtok.cli import main; from adtok.hybrid import hooks; "
            "print(sorted(m for m in sys.modules if m.startswith(('adtok.proxy', "
            "'urllib.request', 'tomllib'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def skill_fixture():
    import time
    target, config = tempfile.mkdtemp(), tempfile.mkdtemp()
    skills = os.path.join(target, ".claude", "skills")
    write(os.path.join(skills, "used", "SKILL.md"), "---\nname: used\ndescription: >\n  runs the release\n  checklist\n---\nx\n")
    write(os.path.join(skills, "stale", "SKILL.md"), "---\nname: stale\ndescription: %s\n---\n%s" % ("s" * 300, "l\n" * 600))
    write(os.path.join(skills, "manual", "SKILL.md"), "---\nname: manual\ndescription: m\ndisable-model-invocation: true\n---\nx\n")
    write(os.path.join(target, ".claude", "commands", "deploy.md"), "---\ndescription: ship it now\n---\nx\n")
    write(os.path.join(target, ".claude", "commands", "kept.md"), "---\ndescription: keep me\n---\nx\n")
    write(os.path.join(target, ".claude", "settings.local.json"), json.dumps({"skillOverrides": {"kept": "off"}}))
    stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    records = [{"type": "user", "timestamp": stamp(60), "message": {"content": "hi"}},
               {"type": "assistant", "timestamp": stamp(2), "message": {"content": [
                   {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "used"}}]}},
               {"type": "user", "timestamp": stamp(45), "message": {"content": "<command-name>/deploy</command-name>"}}]
    write(os.path.join(config, "projects", ledger.project_slug(target), "s.jsonl"),
          "\n".join(json.dumps(r) for r in records) + "\n")
    return target, config


def test_skills_report_and_prune_layer():
    from adtok.skills import frontmatter, report
    assert frontmatter("---\ndescription: >\n  a\n  b\nname: n\n---\n") == {"description": "a b", "name": "n"}
    target, config = skill_fixture()
    os.environ["CLAUDE_CONFIG_DIR"] = config
    try:
        base = os.path.join(target, ".claude")
        result = report([("project", base)], [os.path.join(base, "settings.local.json")], target, days=30)
        by = {s["name"]: s for s in result["skills"]}
        assert by["used"]["uses"] == 1 and by["deploy"]["uses"] == 1 and by["used"]["desc_chars"] == 26
        assert by["manual"]["listed_chars"] == 0 and by["kept"]["listed_chars"] == 0
        assert {n for n, s in by.items() if s["prune"]} == {"stale", "deploy"}
        assert any("keep SKILL.md under 500" in f["message"] for f in result["findings"])
        short = report([("project", base)], [], target, days=90)
        assert not any(s["prune"] for s in short["skills"])
        out = install(target, python="/py", layers={"skills": {"prune_unused": True}})
        assert out["skill_overrides"] == {"stale": "name-only", "deploy": "name-only"}
        assert settings_of(target)["skillOverrides"] == {"kept": "off", "stale": "name-only", "deploy": "name-only"}
        doctor = audit(target, home=tempfile.mkdtemp())
        assert any("keep SKILL.md under 500" in f["message"] for f in doctor["findings"])
        install(target, python="/py")
        assert settings_of(target)["skillOverrides"] == {"kept": "off"}
        install(target, python="/py", layers={"skills": {"prune_unused": True}})
        uninstall(target)
        assert settings_of(target) == {"skillOverrides": {"kept": "off"}}
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)


if __name__ == "__main__":
    from runner import run as run_tests

    sys.exit(run_tests(globals()))

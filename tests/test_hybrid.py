import io
import json
import os
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from starlette.testclient import TestClient

from adtok.hybrid import install, uninstall
from adtok.hybrid.hooks import pre_model_switch, run, user_prompt
from adtok.hybrid.install import BEGIN, END
from adtok.proxy import load_config
from adtok.proxy.server import StubClassifier, create_app
from adtok.proxy.tracker import Tracker


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def settings_of(target):
    return json.loads(read(os.path.join(target, ".claude", "settings.local.json")))


def test_install_writes_agents_block_settings_and_hooks():
    target = tempfile.mkdtemp()
    result = install(target, profile="hybrid", url="http://127.0.0.1:9999", python="/py")
    assert sorted(result["agents_written"]) == ["Explore.md", "architect.md", "fixer.md", "implementer.md"]
    explore = read(os.path.join(target, ".claude", "agents", "Explore.md"))
    assert "name: Explore" in explore and "model: haiku" in explore and "tools: Read, Grep, Glob" in explore
    assert "model: sonnet" in read(os.path.join(target, ".claude", "agents", "implementer.md"))
    assert "effort: high" in read(os.path.join(target, ".claude", "agents", "fixer.md"))
    claude_md = read(os.path.join(target, "CLAUDE.md"))
    assert claude_md.count(BEGIN) == 1 and "## Model routing" in claude_md
    s = settings_of(target)
    assert s["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"
    assert "CLAUDE_CODE_SUBAGENT_MODEL_FORCE" not in s["env"] and "CLAUDE_CODE_EFFORT_LEVEL" not in s["env"]
    assert s["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-5-5"
    ups = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert ups["type"] == "command" and ups["timeout"] == 5
    assert "adtok.cli hook user-prompt --url http://127.0.0.1:9999" in ups["command"]
    assert "pre-model-switch" in s["hooks"]["PreModelSwitch"][0]["hooks"][0]["command"]


def test_native_profile_has_no_hooks():
    target = tempfile.mkdtemp()
    install(target, profile="native")
    assert not {"UserPromptSubmit", "PreModelSwitch"} & set(settings_of(target).get("hooks", {}))
    assert os.path.exists(os.path.join(target, ".claude", "agents", "Explore.md"))


def test_install_preserves_user_content_and_is_idempotent():
    target = tempfile.mkdtemp()
    os.makedirs(os.path.join(target, ".claude", "agents"))
    with open(os.path.join(target, "CLAUDE.md"), "w") as f:
        f.write("# My project\n\nUse tabs.\n")
    with open(os.path.join(target, ".claude", "agents", "Explore.md"), "w") as f:
        f.write("---\nname: Explore\nmodel: sonnet\n---\nmine\n")
    user_hook = {"hooks": [{"type": "command", "command": "echo mine"}]}
    with open(os.path.join(target, ".claude", "settings.local.json"), "w") as f:
        json.dump({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku", "OTHER": "1"},
                   "hooks": {"UserPromptSubmit": [user_hook]}, "permissions": {"allow": ["Bash(ls)"]}}, f)
    first = install(target, profile="hybrid")
    second = install(target, profile="hybrid")
    assert first["agents_skipped"] == ["Explore.md"] and second["agents_skipped"] == ["Explore.md"]
    assert "mine" in read(os.path.join(target, ".claude", "agents", "Explore.md"))
    assert first["env_kept_existing"] == ["CLAUDE_CODE_SUBAGENT_MODEL"]
    s = settings_of(target)
    assert s["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku" and s["env"]["OTHER"] == "1"
    assert s["permissions"] == {"allow": ["Bash(ls)"]}
    ups = s["hooks"]["UserPromptSubmit"]
    assert len(ups) == 2 and ups[0] == user_hook, ups
    text = read(os.path.join(target, "CLAUDE.md"))
    assert text.startswith("# My project\n\nUse tabs.\n") and text.count(BEGIN) == 1 and text.count(END) == 1
    uninstall(target)
    assert read(os.path.join(target, "CLAUDE.md")) == "# My project\n\nUse tabs.\n"
    assert "mine" in read(os.path.join(target, ".claude", "agents", "Explore.md"))
    assert not os.path.exists(os.path.join(target, ".claude", "agents", "implementer.md"))
    s = settings_of(target)
    assert s["env"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku", "OTHER": "1"}
    assert s["hooks"] == {"UserPromptSubmit": [user_hook]}


def test_uninstall_removes_everything_it_created():
    target = tempfile.mkdtemp()
    install(target, profile="hybrid")
    uninstall(target)
    assert os.listdir(os.path.join(target, ".claude")) == []
    assert not os.path.exists(os.path.join(target, "CLAUDE.md"))


def test_user_prompt_hook():
    seen = []

    def fake(url, text):
        seen.append((url, text))
        return {"tier": "mid", "confidence": 0.93, "escalated": False}

    out = user_prompt({"prompt": "Add pagination to the orders endpoint and tests"}, "http://x", fake)
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "UserPromptSubmit" and ctx["additionalContext"].startswith("Task tier: mid")
    assert "0.93" in ctx["additionalContext"] and seen[0][0] == "http://x"
    assert user_prompt({"prompt": "yes do it"}, "http://x", fake) is None
    assert user_prompt({"prompt": "/compact please keep the important stuff"}, "http://x", fake) is None
    assert user_prompt({"prompt": "Add pagination to the orders endpoint", "agent_id": "a1"}, "u", fake) is None

    def broken(url, text):
        raise OSError("down")

    assert user_prompt({"prompt": "Add pagination to the orders endpoint and tests"}, "http://x", broken) is None
    esc = user_prompt({"prompt": "Refactor the billing module into separate services"}, "u",
                      lambda u, t: {"tier": "complex", "confidence": 0.41, "escalated": True})
    assert "raised one tier" in esc["hookSpecificOutput"]["additionalContext"]


def test_pre_model_switch_hook():
    warm = {"prompt_cache_warm": True, "estimated_cache_write_usd": 0.9, "from_model": "claude-opus-5-5",
            "to_model": "claude-haiku-5-5", "context_tokens": 180000}
    out = pre_model_switch(warm, 0.25)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreModelSwitch" and out["permissionDecision"] == "ask"
    assert "subagent" in out["permissionDecisionReason"]
    assert pre_model_switch(dict(warm, estimated_cache_write_usd=0.1), 0.25) is None
    assert pre_model_switch(dict(warm, prompt_cache_warm=False), 0.25) is None
    assert pre_model_switch({}, 0.25) is None


def test_hook_cli_fails_open_when_service_is_down():
    proc = subprocess.run([sys.executable, "-m", "adtok.cli", "hook", "user-prompt", "--url",
                           "http://127.0.0.1:9"], input=json.dumps({"prompt": "Fix the flaky test in the api"}),
                          capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert proc.returncode == 0 and proc.stdout == "", (proc.returncode, proc.stdout, proc.stderr)


def test_classify_endpoint_and_hook_together():
    config = load_config(None, db_path=":memory:", classifier_model="stub:complex")
    app = create_app(config, classifier=StubClassifier("complex"), tracker=Tracker(":memory:"))
    with TestClient(app) as client:
        r = client.post("/__adtok/classify", json={"text": "Rewrite the auth layer"})
        assert r.status_code == 200 and r.json()["tier"] == "complex", r.text
        assert client.post("/__adtok/classify", json={}).status_code == 400

        def via_app(url, text):
            return client.post("/__adtok/classify", json={"text": text}).json()

        out = io.StringIO()
        run("user-prompt", "http://testserver", stream=io.StringIO(json.dumps(
            {"prompt": "Rewrite the whole auth layer to use sessions"})), out=out)
        assert out.getvalue() == ""
        hint = user_prompt({"prompt": "Rewrite the whole auth layer to use sessions"}, "x", via_app)
        assert hint["hookSpecificOutput"]["additionalContext"].startswith("Task tier: complex")


if __name__ == "__main__":
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
    sys.exit(1 if failed else 0)

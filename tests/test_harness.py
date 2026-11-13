import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adtok.harness import load_suite
from adtok.harness import workspace
from adtok.harness.report import build, verdict
from adtok.harness.runner import load_results, run_suite

FAKE_CLAUDE = os.path.join(ROOT, "tests", "fake_claude.py")
RECEIVED = []


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        RECEIVED.append(body)
        payload = json.dumps({"id": "m", "model": body.get("model"), "content": [{"type": "text", "text": "ok"}],
                              "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                        "cache_read_input_tokens": 0, "output_tokens": 200}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def start_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def write_suite(tmp, upstream, budget=10.0, prompt="please FIX the broken thing in this small repository",
                template=None):
    os.makedirs(os.path.join(tmp, "task"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "tpl", "tests"), exist_ok=True)
    with open(os.path.join(tmp, "tpl", "tests", "guard.txt"), "w") as f:
        f.write("original")
    with open(os.path.join(tmp, "task", "prompt.md"), "w") as f:
        f.write(prompt)
    claude = json.dumps([sys.executable, FAKE_CLAUDE])
    suite = """name = "mini"
results_dir = "results"
repeats = 1
budget_usd = %s
per_run_usd = 1.0
baseline_arm = "baseline"
allow_subscription_proxy = true
claude = %s

[[task]]
id = "t1"
prompt = "task/prompt.md"
timeout_min = 2
template = %s

[[task.check]]
name = "fixed"
run = ["{python}", "-c", "import os,sys; sys.exit(0 if os.path.exists('done.txt') else 1)"]

[[task.check]]
name = "tests_unchanged"
unchanged = "tests"

[[arm]]
id = "baseline"
proxy = { shadow = true, upstream = "%s", classifier_model = "stub:simple" }

[[arm]]
id = "routed"
proxy = { shadow = false, upstream = "%s", classifier_model = "stub:simple" }
""" % (budget, claude, template or '{ copy = "tpl" }', upstream, upstream)
    path = os.path.join(tmp, "suite.toml")
    with open(path, "w") as f:
        f.write(suite)
    return path


def port_open(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_core_suite_parses():
    suite = load_suite(os.path.join(ROOT, "bench", "core", "suite.toml"))
    assert [t.id for t in suite.tasks] == ["bugfix-structured", "greenfield-genexp"]
    assert [a.id for a in suite.arms] == ["baseline", "concise", "native", "hybrid", "ensemble", "ensemble-tools",
                                          "routed"]
    assert [a.route_traffic for a in suite.arms] == [False] * 6 + [True]
    assert suite.arm("ensemble-tools").layers["tools"]["test_output"] is True
    assert suite.arm("hybrid").install == "hybrid" and not suite.allow_subscription_proxy
    assert suite.task("bugfix-structured").template["patch"].endswith("bug.patch")
    assert "genexp run" in suite.task("greenfield-genexp").prompt


def test_git_template_applies_planted_bug():
    suite = load_suite(os.path.join(ROOT, "bench", "core", "suite.toml"))
    dest = os.path.join(tempfile.mkdtemp(), "ws")
    protected = workspace.prepare(suite.task("bugfix-structured"), dest)
    src = open(os.path.join(dest, "laya", "structured.py"), encoding="utf-8").read()
    assert "range(lo, hi)]" in src and "values[f.name] = idx\n" in src
    log = subprocess.run(["git", "log", "--oneline"], cwd=dest, capture_output=True, text=True).stdout
    assert len(log.strip().splitlines()) == 1, log
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True).stdout
    assert status.strip() == "", status
    assert "tests/test_structured.py" in protected["tests"]
    run = subprocess.run([sys.executable, "tests/test_structured.py"], cwd=dest, capture_output=True, text=True)
    assert run.returncode != 0
    with open(os.path.join(dest, "tests", "test_structured.py"), "a") as f:
        f.write("\n")
    assert workspace.changed_files(dest, "tests", protected["tests"]) == ["tests/test_structured.py"]


def test_end_to_end_run_resume_and_report():
    server, url = start_upstream()
    try:
        tmp = tempfile.mkdtemp()
        suite = load_suite(write_suite(tmp, url))
        RECEIVED.clear()
        logs = []
        outcomes = run_suite(suite, log=logs.append)
        assert len(outcomes) == 2, logs
        by_arm = {r["arm"]: r for r in outcomes}
        for arm, r in by_arm.items():
            assert r["status"] == "ok", (arm, r["status"], logs)
            assert r["accepted"], r["checks"]
            assert r["usage"]["requests"] == 1 and r["usage"]["errors"] == 0, r["usage"]
            assert r["agent"]["num_turns"] == 2 and r["agent"]["subagents"] == 1
            assert r["agent"]["api_key_source"] == "none"
        assert [b["model"] for b in RECEIVED] == ["claude-opus-5-5", "claude-haiku-5-5"], RECEIVED
        assert by_arm["routed"]["usage"]["models"] == {"claude-haiku-5-5": 1}
        assert by_arm["routed"]["usage"]["cost_usd"] < by_arm["baseline"]["usage"]["cost_usd"]
        for r in outcomes:
            cfg = json.load(open(os.path.join(suite.results_dir, "t1", r["arm"], "rep-1", "proxy.json")))
            assert not port_open(cfg["port"]), "proxy for %s still listening" % r["arm"]
        again = run_suite(suite, log=logs.append)
        assert again == [] and sum("already done" in line for line in logs) == 2
        text = build(load_results(suite.results_dir), "baseline")
        assert "## t1" in text and "n<2 per arm" in text and "claude-haiku-5-5 $" in text and "proxy" in text
    finally:
        server.shutdown()


def test_unchanged_check_and_budget_skip():
    server, url = start_upstream()
    try:
        tmp = tempfile.mkdtemp()
        path = write_suite(tmp, url, budget=10.0, prompt="please FIX the bug and also TAMPER with the tests")
        first = run_suite(load_suite(path), arms=["baseline"], log=lambda *_: None)
        assert len(first) == 1 and first[0]["status"] == "ok", first
        tight = load_suite(path)
        tight.budget_usd = first[0]["usage"]["cost_usd"] * 1.5
        outcomes = run_suite(tight, log=lambda *_: None)
        assert [r["status"] for r in outcomes] == ["skipped_budget"], outcomes
        ran = first
        checks = {c["name"]: c for c in ran[0]["checks"]}
        assert checks["fixed"]["passed"] and not checks["tests_unchanged"]["passed"]
        assert "tests/guard.txt" in checks["tests_unchanged"]["output"]
        assert ran[0]["accepted"] is False
    finally:
        server.shutdown()


def test_hybrid_arm_installs_config_and_measures_without_proxy_traffic():
    server, url = start_upstream()
    try:
        tmp = tempfile.mkdtemp()
        path = write_suite(tmp, url)
        with open(path, "a") as f:
            f.write('\n[[arm]]\nid = "hybrid"\nroute_traffic = false\ninstall = "hybrid"\n'
                    'proxy = { classifier_model = "stub:mid" }\n')
        suite = load_suite(path)
        suite.allow_subscription_proxy = False
        RECEIVED.clear()
        outcomes = run_suite(suite, arms=["hybrid", "routed"], log=lambda *_: None)
        statuses = {r["arm"]: r["status"] for r in outcomes}
        assert statuses == {"hybrid": "ok", "routed": "skipped_needs_api_key"}, outcomes
        r = next(o for o in outcomes if o["arm"] == "hybrid")
        assert RECEIVED == [] and r["usage"]["requests"] == 0
        assert r["tokens"]["source"] == "agent_report" and abs(r["tokens"]["cost_usd"] - 0.0124) < 1e-9
        assert r["tokens"]["cache_read"] == 5000 and r["tokens"]["output"] == 250
        ws = os.path.join(suite.results_dir, "t1", "hybrid", "rep-1", "workspace")
        seen = json.load(open(os.path.join(ws, "seen_settings.json")))
        cfg = json.load(open(os.path.join(suite.results_dir, "t1", "hybrid", "rep-1", "proxy.json")))
        hook = seen["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        assert "--url http://127.0.0.1:%d" % cfg["port"] in hook
        assert os.path.exists(os.path.join(ws, ".claude", "agents", "Explore.md"))
        tracked = subprocess.run(["git", "ls-files", ".claude", "CLAUDE.md"], cwd=ws, capture_output=True,
                                 text=True).stdout.split()
        assert "CLAUDE.md" in tracked and ".claude/agents/Explore.md" in tracked
        text = build(load_results(suite.results_dir), "baseline")
        assert "agent_report" in text
    finally:
        server.shutdown()


def test_verdict():
    assert verdict([1.0], [2.0])[1].startswith("n<2")
    delta, note = verdict([1.0, 1.1, 0.9], [2.0, 2.1, 1.9])
    assert delta == "-50.0%" and note.startswith("likely real")
    assert verdict([1.0, 3.0, 2.0], [2.0, 1.0, 3.0])[1].startswith("within noise")


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

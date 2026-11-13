import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from . import metrics, workspace


def find_claude(explicit=None):
    if explicit:
        return list(explicit) if isinstance(explicit, (list, tuple)) else [explicit]
    found = shutil.which("claude")
    if found:
        return [found]
    pattern = os.path.expanduser("~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude*")
    candidates = [p for p in glob.glob(pattern) if os.path.basename(p) in ("claude", "claude.exe")]
    if not candidates:
        raise FileNotFoundError("Claude Code CLI not found; set 'claude' in the suite file")
    return [max(candidates, key=os.path.getmtime)]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def wait_ready(port, proc, timeout_s=600):
    deadline = time.time() + timeout_s
    url = "http://127.0.0.1:%d/__adtok/health" % port
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("proxy exited with code %s before becoming ready" % proc.returncode)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if json.loads(resp.read()).get("ready"):
                    return
        except OSError:
            pass
        time.sleep(1)
    raise TimeoutError("proxy not ready after %ds" % timeout_s)


def cost_of(result):
    tokens = result.get("tokens") or metrics.combined(result.get("usage") or {}, result.get("agent") or {})
    return float(tokens.get("cost_usd") or 0.0)


def run_dir_for(results_dir, task, arm, rep):
    return os.path.join(results_dir, task.id, arm.id, "rep-%d" % rep)


def load_results(results_dir):
    out = []
    for path in glob.glob(os.path.join(results_dir, "*", "*", "rep-*", "result.json")):
        with open(path, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def plan(suite, tasks=None, arms=None, repeats=None):
    tasks = [suite.task(t) for t in tasks] if tasks else suite.tasks
    arms = [suite.arm(a) for a in arms] if arms else suite.arms
    reps = repeats or suite.repeats
    return [(task, arm, rep) for rep in range(1, reps + 1) for task in tasks for arm in arms]


def proxy_config(suite, arm, port, run_dir):
    cfg = dict(arm.proxy)
    cfg.update(host="127.0.0.1", port=port, db_path=os.path.join(run_dir, "usage.sqlite"), store_prompts=True,
               budget={"session_usd": suite.per_run_usd, "action": "block"})
    return cfg


def claude_command(suite, task, arm, claude):
    cmd = list(claude) + ["-p", task.prompt, "--output-format", "stream-json", "--verbose", "--permission-mode",
           "acceptEdits", "--max-budget-usd", "%.2f" % (suite.per_run_usd * 1.25), "--allowedTools"]
    cmd += suite.allowed_tools
    if arm.append_system_prompt:
        cmd += ["--append-system-prompt", arm.append_system_prompt]
    if arm.model:
        cmd += ["--model", arm.model]
    return cmd


def run_checks(suite, task, ws, protected):
    results = []
    values = {"{python}": suite.python, "{workspace}": ws, "{suite}": suite.root, "{task_dir}": task.dir}
    for check in task.checks:
        started = time.time()
        if check.unchanged:
            changed = workspace.changed_files(ws, check.unchanged, protected.get(check.unchanged, {}))
            results.append({"name": check.name, "passed": not changed, "output": "changed: %s" % changed[:20]
                            if changed else "", "seconds": 0.0})
            continue
        args = [values.get(a, a) for a in check.run]
        args = [a.replace("{task_dir}", task.dir).replace("{suite}", suite.root) for a in args]
        try:
            proc = subprocess.run(args, cwd=ws, capture_output=True, text=True, timeout=check.timeout_s,
                                  env=dict(os.environ, PYTHONIOENCODING="utf-8", USE_TF="0"))
            passed, output = proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
        except subprocess.TimeoutExpired:
            passed, output = False, "timed out after %ds" % check.timeout_s
        results.append({"name": check.name, "passed": passed, "output": output,
                        "seconds": round(time.time() - started, 1)})
    return results


def run_one(suite, task, arm, rep, results_dir, claude, log=print):
    run_dir = run_dir_for(results_dir, task, arm, rep)
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    ws = os.path.join(run_dir, "workspace")
    port = free_port()
    before_commit = None
    if arm.install:
        from adtok.hybrid import install

        def before_commit(dest):
            install(dest, profile=arm.install, url="http://127.0.0.1:%d" % port, python=sys.executable,
                    layers=arm.layers)
    protected = workspace.prepare(task, ws, before_commit)
    cfg_path = os.path.join(run_dir, "proxy.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(proxy_config(suite, arm, port, run_dir), f, indent=1)
    env = dict(os.environ, USE_TF="0", PYTHONUNBUFFERED="1")
    status, proxy, started = "ok", None, time.time()
    try:
        with open(os.path.join(run_dir, "proxy.log"), "w", encoding="utf-8") as plog:
            proxy = subprocess.Popen([sys.executable, "-m", "adtok.cli", "serve", "--config", cfg_path, "-v"],
                                     stdout=plog, stderr=subprocess.STDOUT, env=env)
            wait_ready(port, proxy)
            agent_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
            if arm.route_traffic:
                agent_env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:%d" % port
            started = time.time()
            with open(os.path.join(run_dir, "transcript.jsonl"), "w", encoding="utf-8") as out, \
                    open(os.path.join(run_dir, "agent.stderr"), "w", encoding="utf-8") as err:
                agent = subprocess.Popen(claude_command(suite, task, arm, claude), cwd=ws, stdout=out, stderr=err,
                                         env=agent_env)
                try:
                    agent.wait(timeout=task.timeout_min * 60)
                except subprocess.TimeoutExpired:
                    kill_tree(agent)
                    status = "timeout"
                if status == "ok" and agent.returncode != 0:
                    status = "agent_exit_%s" % agent.returncode
    except Exception as exc:
        status = "harness_error: %s" % exc
    finally:
        kill_tree(proxy)
    wall = time.time() - started
    checks = run_checks(suite, task, ws, protected) if not status.startswith("harness_error") else []
    usage = metrics.usage(os.path.join(run_dir, "usage.sqlite"))
    agent_info = metrics.transcript(os.path.join(run_dir, "transcript.jsonl"))
    tokens = metrics.combined(usage, agent_info)
    result = {"task": task.id, "kind": task.kind, "arm": arm.id, "rep": rep, "status": status,
              "accepted": bool(checks) and all(c["passed"] for c in checks), "checks": checks,
              "wall_min": round(wall / 60, 2), "tokens": tokens, "usage": usage, "agent": agent_info,
              "route_traffic": arm.route_traffic, "install": arm.install, "finished_at": time.time()}
    with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    if not suite.keep_venv:
        workspace.prune(ws)
    log("%-22s %-10s rep %d  %-10s accepted=%s cost=$%.2f (%s)  %.1f min" % (
        task.id, arm.id, rep, status, result["accepted"], tokens["cost_usd"], tokens["source"], result["wall_min"]))
    return result


def run_suite(suite, tasks=None, arms=None, repeats=None, claude=None, log=print):
    claude = find_claude(claude or suite.claude)
    done = {(r["task"], r["arm"], r["rep"]): r for r in load_results(suite.results_dir)}
    spent = sum(cost_of(r) for r in done.values())
    outcomes = []
    for task, arm, rep in plan(suite, tasks, arms, repeats):
        key = (task.id, arm.id, rep)
        if key in done:
            log("%-22s %-10s rep %d  already done, skipping" % key)
            continue
        if arm.route_traffic and not suite.allow_subscription_proxy and not os.environ.get("ANTHROPIC_API_KEY"):
            log("%-22s %-10s rep %d  skipped: sends traffic through the proxy; set ANTHROPIC_API_KEY or "
                "allow_subscription_proxy = true" % key)
            outcomes.append({"task": task.id, "arm": arm.id, "rep": rep, "status": "skipped_needs_api_key"})
            continue
        same_task = [cost_of(r) for r in done.values() if r["task"] == task.id]
        expected = sum(same_task) / len(same_task) if same_task else suite.per_run_usd
        if spent + expected > suite.budget_usd:
            log("%-22s %-10s rep %d  skipped: suite budget $%.2f would be exceeded (spent $%.2f, expect $%.2f)"
                % (task.id, arm.id, rep, suite.budget_usd, spent, expected))
            outcomes.append({"task": task.id, "arm": arm.id, "rep": rep, "status": "skipped_budget"})
            continue
        result = run_one(suite, task, arm, rep, suite.results_dir, claude, log)
        done[key] = result
        spent += cost_of(result)
        outcomes.append(result)
    return outcomes

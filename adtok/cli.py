import argparse
import json
import logging
import re
import sys
import time


def parse_since(value):
    if value in (None, "all"):
        return 0
    match = re.fullmatch(r"(\d+)([mhd])", value)
    if not match:
        raise argparse.ArgumentTypeError("use e.g. 30m, 24h, 7d or all")
    seconds = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return time.time() - seconds


def cmd_serve(args):
    import uvicorn

    from .proxy import create_app, load_config

    config = load_config(args.config, host=args.host, port=args.port, upstream=args.upstream,
                         classifier_model=args.classifier_model, classifier_device=args.device,
                         shadow=args.shadow, db_path=args.db)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning")


def cmd_stats(args):
    from .proxy import load_config
    from .proxy.tracker import Tracker

    config = load_config(args.config, db_path=args.db)
    tracker = Tracker(config.db_path)
    rows = tracker.summary(by=args.by, since=parse_since(args.since))
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    if not rows:
        print("no requests recorded in %s" % config.db_path)
        return
    print("%-44s %8s %7s %12s %12s %10s %10s %10s %10s" % (args.by, "requests", "routed", "prompt_tok", "cache_read",
                                                          "output_tok", "cost_$", "unrouted_$", "saved_$"))
    total = {k: 0 for k in rows[0] if k != "group"}
    for r in rows:
        for k in total:
            total[k] += r[k] or 0
        print_row(r)
    print_row(dict(total, group="TOTAL"))
    if total["counterfactual_usd"]:
        print("estimated saving: %.1f%%  (unrouted_$ prices the same tokens at the model the agent asked for)"
              % (100 * (total["counterfactual_usd"] - total["cost_usd"]) / total["counterfactual_usd"]))


def print_row(r):
    print("%-44s %8d %7d %12d %12d %10d %10.4f %10.4f %10.4f" % (
        str(r["group"])[:44], r["requests"], r["routed"] or 0, r["prompt_tokens"], r["cache_read_tokens"],
        r["output_tokens"], r["cost_usd"], r["counterfactual_usd"], r["counterfactual_usd"] - r["cost_usd"]))


def cmd_classify(args):
    from .proxy import load_config
    from .proxy.policy import Router
    from .proxy.pricing import Pricing
    from . import TierClassifier

    config = load_config(args.config, classifier_model=args.classifier_model, classifier_device=args.device)
    classifier = TierClassifier.load(config.classifier_model, device=config.classifier_device,
                                     min_confidence=config.min_confidence)
    router = Router(config, classifier, Pricing(config.prices))
    for text in args.text:
        body = {"model": args.model, "messages": [{"role": "user", "content": text}]}
        d = router.decide(body)
        print("%-7s %.3f  %s -> %s%s  %s" % (d.tier, d.confidence, d.requested_model, d.target_model,
                                            " effort=%s" % d.effort if d.effort else "", text[:80]))


def cmd_bench_plan(args):
    from .harness import load_suite
    from .harness.runner import load_results, plan

    suite = load_suite(args.suite)
    runs = plan(suite, args.task, args.arm, args.repeats)
    done = {(r["task"], r["arm"], r["rep"]) for r in load_results(suite.results_dir)}
    todo = [(t, a, r) for t, a, r in runs if (t.id, a.id, r) not in done]
    print("suite %s -> %s" % (suite.name, suite.results_dir))
    for task, arm, rep in runs:
        print("  %-24s %-12s rep %d  %s" % (task.id, arm.id, rep, "done" if (task.id, arm.id, rep) in done else "todo"))
    print("%d runs to do; spend is capped at $%.2f per run and $%.2f for the suite (list-price equivalent)"
          % (len(todo), suite.per_run_usd, suite.budget_usd))


def cmd_bench_run(args):
    from .harness import load_suite
    from .harness.report import build
    from .harness.runner import load_results, run_suite

    suite = load_suite(args.suite)
    run_suite(suite, args.task, args.arm, args.repeats)
    text = build(load_results(suite.results_dir), suite.baseline_arm, "Token benchmark: %s" % suite.name)
    write_report(suite.results_dir, text)


def cmd_bench_report(args):
    import os

    from .harness import load_suite
    from .harness.report import build
    from .harness.runner import load_results

    if args.target.endswith(".toml"):
        suite = load_suite(args.target)
        results_dir, baseline, name = suite.results_dir, args.baseline or suite.baseline_arm, suite.name
    else:
        results_dir, baseline, name = args.target, args.baseline, os.path.basename(args.target.rstrip("/\\"))
    write_report(results_dir, build(load_results(results_dir), baseline, "Token benchmark: %s" % name))


def write_report(results_dir, text):
    import os

    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print("written to %s" % path)


def parse_sets(values):
    import tomllib

    out = {}
    for item in values or []:
        key, _, value = item.partition("=")
        section, _, name = key.strip().partition(".")
        if not name or not value:
            raise SystemExit("--set takes section.key=value, e.g. tools.test_output=true")
        try:
            parsed = tomllib.loads("v = %s" % value.strip())["v"]
        except tomllib.TOMLDecodeError:
            parsed = value.strip()
        out.setdefault(section, {})[name] = parsed
    return out


def cmd_init(args):
    from .hybrid import install

    result = install(args.target, profile=args.profile, url=args.url, force=args.force,
                     switch_ask_usd=args.ask_above_usd, layers=parse_sets(args.set))
    print("installed adtok into %s (config: .claude/adtok.toml)" % args.target)
    if result["routing"]:
        print("  routing (%s): agents %s; CLAUDE.md block between adtok markers" % (
            result["profile"], ", ".join(result["agents_written"]) or "unchanged"))
    if result["agents_skipped"]:
        print("  agents kept (already customised; use --force to replace): %s" % ", ".join(result["agents_skipped"]))
    for key, value in sorted(result["settings"].items()):
        print("  setting %s = %s" % (key, json.dumps(value)))
    if result["env_kept_existing"]:
        print("  kept your existing values for: %s" % ", ".join(result["env_kept_existing"]))
    print("  hooks: %s" % (", ".join(result["hooks"]) or "none"))
    if "UserPromptSubmit" in result["hooks"]:
        print("  the tier hint needs the classifier service running: adtok serve  (no ANTHROPIC_BASE_URL needed)")
    print("  check the result with: adtok doctor %s" % args.target)


def cmd_doctor(args):
    from .doctor import audit

    result = audit(args.target)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    print("user-controlled prefix, re-read on every request (about %d tokens):" % result["prefix_tokens"])
    for c in sorted(result["components"], key=lambda c: -c["tokens"]):
        print("  %7d tok %5d lines  %-12s %s" % (c["tokens"], c["lines"], c["kind"], c["path"]))
    for f in result["findings"]:
        print("%-4s [%s] %s" % (f["level"], f["layer"], f["message"]))
    if not result["findings"]:
        print("no findings")
    return 1 if args.strict and any(f["level"] == "warn" for f in result["findings"]) else 0


def cmd_ledger(args):
    from . import ledger

    files = ledger.session_files(args.project)
    if args.session:
        files = [f for f in files if args.session in f]
    rows, compactions, _ = ledger.read_requests(files, since=parse_since(args.since))
    if not rows:
        print("no requests found (%d transcript files)" % len(files))
        return 0
    if args.view == "misses":
        found = ledger.misses(rows, compactions)
        print("%d cache rebuilds, about $%.2f above a cache read" % (len(found), sum(m["usd"] for m in found)))
        for m in found[:args.top]:
            print("  $%6.3f %9d tok  %-18s %-8s %s  %s" % (
                m["usd"], m["tokens"], m["model"][:18], "main" if m["agent"] == "main" else "subagent",
                time.strftime("%m-%d %H:%M", time.localtime(m["ts"])), m["cause"]))
        return 0
    if args.view == "ttl":
        a = ledger.ttl_advice(rows)
        print("cache cost re-priced on your real request gaps: 5m TTL $%.2f, 1h TTL $%.2f -> %s is cheaper by $%.2f"
              % (a["cache_usd_5m"], a["cache_usd_1h"], a["cheaper"], a["saving_usd"]))
        print("set it with promptCacheTtl / subagentPromptCacheTtl in settings (\"5m\" or \"1h\")")
        return 0
    groups = ledger.summarize(rows, args.by)
    print("%-38s %6s %9s %9s %9s %11s %9s %6s %9s" % (args.by, "req", "input", "write5m", "write1h", "read",
                                                     "output", "hit%", "cost_$"))
    for g in groups[:args.top]:
        print("%-38s %6d %9d %9d %9d %11d %9d %5.1f%% %9.3f" % (
            str(g["group"])[:38], g["requests"], g["input"], g["write_5m"], g["write_1h"], g["read"], g["output"],
            100 * g["hit_rate"], g["cost"]))
    total = sum(r["cost"] for r in rows)
    print("total $%.3f over %d requests" % (total, len(rows)))
    return 0


def cmd_digest(args):
    from .digest import main as digest_main

    return digest_main(args.log_dir, args.min_chars, args.tail)


def cmd_uninstall(args):
    from .hybrid import uninstall

    result = uninstall(args.target)
    print("removed adtok configuration from %s (agents removed: %s)" % (
        args.target, ", ".join(result["agents_removed"]) or "none"))


def cmd_hook(args):
    from .hybrid.hooks import run

    opts = {}
    if args.event == "pre-tool":
        opts = {"test_output": args.test_output, "read_max_lines": args.read_max_lines,
                "grep_head_limit": args.grep_head_limit}
    return run(args.event, args.url, args.ask_above_usd, **opts)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="adtok")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the routing proxy")
    serve.add_argument("--config")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--upstream")
    serve.add_argument("--classifier-model")
    serve.add_argument("--device")
    serve.add_argument("--db")
    serve.add_argument("--shadow", action="store_const", const=True)
    serve.add_argument("-v", "--verbose", action="store_true")
    serve.set_defaults(func=cmd_serve)

    stats = sub.add_parser("stats", help="summarize tracked usage")
    stats.add_argument("--config")
    stats.add_argument("--db")
    stats.add_argument("--since", default="24h")
    stats.add_argument("--by", default="tier", choices=["tier", "model", "route", "source", "role", "session", "day"])
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    classify = sub.add_parser("classify", help="show the tier and route for prompts")
    classify.add_argument("text", nargs="+")
    classify.add_argument("--config")
    classify.add_argument("--model", default="claude-opus-5-5")
    classify.add_argument("--classifier-model")
    classify.add_argument("--device")
    classify.set_defaults(func=cmd_classify)

    bench = sub.add_parser("bench", help="measure token use and task success across configurations")
    bench_sub = bench.add_subparsers(dest="bench_command", required=True)
    for name, func, help_text in (("plan", cmd_bench_plan, "list the runs a suite would do"),
                                  ("run", cmd_bench_run, "run a suite (resumes; skips finished runs)")):
        p = bench_sub.add_parser(name, help=help_text)
        p.add_argument("suite")
        p.add_argument("--task", action="append")
        p.add_argument("--arm", action="append")
        p.add_argument("--repeats", type=int)
        p.set_defaults(func=func)
    rep = bench_sub.add_parser("report", help="rebuild the report from a suite file or a results directory")
    rep.add_argument("target")
    rep.add_argument("--baseline")
    rep.set_defaults(func=cmd_bench_report)

    init = sub.add_parser("init", help="install the enabled layers from .claude/adtok.toml (created on first run)")
    init.add_argument("target", nargs="?", default=".")
    init.add_argument("--profile", choices=["native", "hybrid"])
    init.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE",
                      help="override a layer setting for this install, e.g. tools.test_output=true")
    init.add_argument("--url", default="http://127.0.0.1:8787")
    init.add_argument("--ask-above-usd", type=float)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="audit the prompt prefix, cache invalidators and layer conflicts")
    doctor.add_argument("target", nargs="?", default=".")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--strict", action="store_true", help="exit 1 on any warning")
    doctor.set_defaults(func=cmd_doctor)

    led = sub.add_parser("ledger", help="token and cost ledger from Claude Code transcripts")
    led.add_argument("view", nargs="?", default="usage", choices=["usage", "misses", "ttl"])
    led.add_argument("--project", help="project directory (default: all projects)")
    led.add_argument("--session")
    led.add_argument("--since", default="all")
    led.add_argument("--by", default="session", choices=["session", "model", "agent", "day"])
    led.add_argument("--top", type=int, default=20)
    led.set_defaults(func=cmd_ledger)

    dig = sub.add_parser("digest", help="shorten long command output from stdin, keeping the full log on disk")
    dig.add_argument("--log-dir", required=True)
    dig.add_argument("--min-chars", type=int, default=8000)
    dig.add_argument("--tail", type=int, default=60)
    dig.set_defaults(func=cmd_digest)

    uninst = sub.add_parser("uninstall", help="remove what init installed")
    uninst.add_argument("target", nargs="?", default=".")
    uninst.set_defaults(func=cmd_uninstall)

    hook = sub.add_parser("hook", help="entry point for Claude Code hooks")
    hook.add_argument("event", choices=["user-prompt", "pre-model-switch", "pre-tool", "pre-compact", "post-compact"])
    hook.add_argument("--url", default="http://127.0.0.1:8787")
    hook.add_argument("--ask-above-usd", type=float, default=0.25)
    hook.add_argument("--test-output", action="store_true")
    hook.add_argument("--read-max-lines", type=int, default=0)
    hook.add_argument("--grep-head-limit", type=int, default=0)
    hook.set_defaults(func=cmd_hook)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

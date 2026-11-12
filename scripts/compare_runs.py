import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_report import fmt_int, fmt_usd, load_result, load_rows, table, tokens


def summarize(run, checks):
    rows = load_rows(os.path.join(run, "usage.sqlite"))
    result, tools, subagents = load_result(os.path.join(run, "transcript.jsonl"))
    started = float(open(os.path.join(run, "started_at")).read().strip())
    finished = float(open(os.path.join(run, "finished_at")).read().strip())
    t = tokens(rows)
    shadow = bool(rows and rows[0]["shadow"])
    models = {}
    for r in rows:
        models[r["sent_model"]] = models.get(r["sent_model"], 0) + 1
    data = json.load(open(checks, encoding="utf-8")) if checks and os.path.exists(checks) else {}
    return {"rows": rows, "t": t, "result": result or {}, "tools": tools, "subagents": subagents,
            "minutes": (finished - started) / 60, "shadow": shadow, "models": models, "checks": data}


def per(value, denom):
    return value / denom if denom else None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--routed", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--routed-checks")
    parser.add_argument("--baseline-checks")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    a = summarize(args.routed, args.routed_checks)
    b = summarize(args.baseline, args.baseline_checks)

    def row(label, fa, fb, fmt, lower_is_better=True):
        va, vb = fa(a), fb(b) if fb else fa(b)
        if va is None or vb is None:
            delta = "-"
        else:
            delta = "%+.1f%%" % (100 * (va - vb) / vb) if vb else "-"
        return [label, fmt(va) if va is not None else "-", fmt(vb) if vb is not None else "-", delta]

    num = lambda x: fmt_int(x)
    usd = lambda x: fmt_usd(x)
    mins = lambda x: "%.1f" % x
    pct = lambda x: "%.1f%%" % x
    small = lambda x: "%.2f" % x
    cost = lambda s: s["t"]["cost"]
    loc = lambda s: s["checks"].get("python_loc")
    passed = lambda s: s["checks"].get("tests_passed")
    rows = [
        row("API requests", lambda s: s["t"]["requests"], None, num),
        row("agent turns", lambda s: s["result"].get("num_turns"), None, num),
        row("wall clock (min)", lambda s: s["minutes"], None, mins),
        row("tool calls", lambda s: sum(s["tools"].values()), None, num),
        row("subagents", lambda s: s["subagents"], None, num),
        row("input, uncached", lambda s: s["t"]["input"], None, num),
        row("cache write", lambda s: s["t"]["cache_write"], None, num),
        row("cache read", lambda s: s["t"]["cache_read"], None, num),
        row("**total input side**", lambda s: s["t"]["prompt_total"], None, num),
        row("**output tokens**", lambda s: s["t"]["output"], None, num),
        row("cache hit rate", lambda s: per(100 * s["t"]["cache_read"], s["t"]["prompt_total"]), None, pct, False),
        row("**cost, list-price equivalent**", cost, None, usd),
        row("python lines delivered", loc, None, num, False),
        row("tests passed", passed, None, num, False),
        row("cost per 100 lines", lambda s: per(100 * cost(s), loc(s)), None, usd),
        row("output tokens per line", lambda s: per(s["t"]["output"], loc(s)), None, small),
        row("cost per passing test", lambda s: per(cost(s), passed(s)), None, usd),
    ]
    lines = ["# Efficiency comparison: routed vs baseline", "",
             "Same task, same agent (Claude Code 2.1.322, headless, default model `claude-opus-5-5`), same $20 cap. "
             "Routed: laya-tier rewrote requests by tier. Baseline: laya-tier in shadow mode (measured, never "
             "rewritten).", "",
             table(["metric", "routed", "baseline", "routed vs baseline"], rows), "",
             "## Models that served the requests", "",
             table(["run", "models (requests)"],
                   [["routed", ", ".join("%s (%d)" % kv for kv in sorted(a["models"].items(), key=lambda kv: -kv[1]))],
                    ["baseline", ", ".join("%s (%d)" % kv for kv in sorted(b["models"].items(),
                                                                           key=lambda kv: -kv[1]))]]), "",
             "## Build outcome", "",
             table(["check", "routed", "baseline"],
                   [[k, a["checks"].get(k, "-"), b["checks"].get(k, "-")]
                    for k in sorted(set(a["checks"]) | set(b["checks"]))]), ""]
    shadow_est = b["t"]["unrouted"]
    lines += ["## What routing would have done to the baseline", "",
              "In shadow mode the proxy still classified every request. Pricing the baseline's own tokens at the "
              "models it would have picked gives %s against %s actual (%s)." % (
                  fmt_usd(shadow_est), fmt_usd(b["t"]["cost"]),
                  "%+.1f%%" % (100 * (shadow_est - b["t"]["cost"]) / b["t"]["cost"]) if b["t"]["cost"] else "-"), ""]
    text = "\n".join(lines) + "\n"
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()

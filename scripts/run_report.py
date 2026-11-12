import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from laya_tier.proxy.pricing import Pricing


def load_rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM requests ORDER BY id")]
    conn.close()
    return rows


def load_result(transcript):
    result, tools, subagents = None, defaultdict(int), 0
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                result = event
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        tools[block.get("name")] += 1
                        if block.get("name") in ("Task", "Agent"):
                            subagents += 1
    return result, dict(tools), subagents


def tokens(rows):
    out = defaultdict(float)
    for r in rows:
        out["requests"] += 1
        out["input"] += r["input_tokens"] or 0
        out["cache_write"] += r["cache_write_tokens"] or 0
        out["cache_write_1h"] += r["cache_write_1h_tokens"] or 0
        out["cache_read"] += r["cache_read_tokens"] or 0
        out["output"] += r["output_tokens"] or 0
        out["cost"] += r["cost_usd"] or 0
        out["unrouted"] += r["counterfactual_usd"] or 0
        out["routed"] += r["applied"] or 0
    out["prompt_total"] = out["input"] + out["cache_write"] + out["cache_read"]
    return out


def fmt_int(x):
    return "{:,}".format(int(x))


def fmt_usd(x):
    return "${:,.4f}".format(x)


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def group_table(rows, key, label):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    body = []
    for name, items in sorted(groups.items(), key=lambda kv: -tokens(kv[1])["cost"]):
        t = tokens(items)
        body.append([name, fmt_int(t["requests"]), fmt_int(t["input"]), fmt_int(t["cache_write"]),
                     fmt_int(t["cache_read"]), fmt_int(t["output"]), fmt_usd(t["cost"]), fmt_usd(t["unrouted"])])
    return table([label, "requests", "input", "cache write", "cache read", "output", "cost", "unrouted cost"], body)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--checks", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    rows = load_rows(os.path.join(args.run, "usage.sqlite"))
    result, tools, subagents = load_result(os.path.join(args.run, "transcript.jsonl"))
    started = float(open(os.path.join(args.run, "started_at")).read().strip())
    finished = float(open(os.path.join(args.run, "finished_at")).read().strip())
    total = tokens(rows)
    ok = [r for r in rows if r["status"] == 200]
    main_rows = [r for r in rows if r["role"] == "main"]
    branch_rows = [r for r in rows if r["role"] == "branch"]
    pricing = Pricing()
    opus_only = sum(pricing.cost("claude-opus-5-5", {
        "input_tokens": r["input_tokens"], "cache_creation_input_tokens": r["cache_write_tokens"],
        "cache_read_input_tokens": r["cache_read_tokens"], "output_tokens": r["output_tokens"],
        "cache_creation": {"ephemeral_1h_input_tokens": r["cache_write_1h_tokens"]}}) or 0 for r in rows)

    lines = ["# Token usage report: gene-expression platform built through laya-tier", ""]
    lines += ["## Run", "",
              table(["item", "value"], [
                  ["task", "`laya_tier/proxy/test.md` (end-to-end gene expression analysis platform)"],
                  ["agent", "Claude Code 2.1.322, headless (`claude -p`), default model `claude-opus-5-5`"],
                  ["proxy", "laya-tier, classifier `models/tier-full-v1`, session lock on, budget $20 (block)"],
                  ["workspace", "`%s`" % args.workspace],
                  ["wall clock", "%.1f min" % ((finished - started) / 60)],
                  ["agent result", (result or {}).get("subtype", "?") + (", " + str(result.get("num_turns")) + " turns"
                                                                       if result and result.get("num_turns") else "")],
                  ["requests through proxy", "%d (%d returned 200)" % (total["requests"], len(ok))],
                  ["tool calls", ", ".join("%s %d" % kv for kv in sorted(tools.items(), key=lambda kv: -kv[1]))],
                  ["subagents spawned", str(subagents)],
              ]), ""]
    lines += ["## Tokens", "",
              table(["", "tokens"], [
                  ["input (uncached)", fmt_int(total["input"])],
                  ["cache write", "%s (of which 1h TTL: %s)" % (fmt_int(total["cache_write"]),
                                                               fmt_int(total["cache_write_1h"]))],
                  ["cache read", fmt_int(total["cache_read"])],
                  ["**total input side**", "**%s**" % fmt_int(total["prompt_total"])],
                  ["**output**", "**%s**" % fmt_int(total["output"])],
                  ["cache hit rate (read / input side)",
                   "%.1f%%" % (100 * total["cache_read"] / total["prompt_total"] if total["prompt_total"] else 0)],
              ]), ""]
    lines += ["## Cost (list-price equivalent)", "",
              table(["", "USD"], [
                  ["actual, as routed by laya-tier", fmt_usd(total["cost"])],
                  ["same tokens if every request had used the model Claude Code asked for", fmt_usd(total["unrouted"])],
                  ["same tokens all on claude-opus-5-5", fmt_usd(opus_only)],
                  ["estimated saving", "%s (%.1f%%)" % (fmt_usd(total["unrouted"] - total["cost"]),
                                                       100 * (total["unrouted"] - total["cost"]) / total["unrouted"]
                                                       if total["unrouted"] else 0)],
                  ["Claude Code's own `total_cost_usd`", fmt_usd((result or {}).get("total_cost_usd") or 0)],
              ]), "",
              "Claude Code prices every request at the model it asked for, so its figure does not see the routing.",
              ""]
    lines += ["## By role", "", group_table(rows, lambda r: r["role"] or "-", "role"), ""]
    lines += ["## By model actually used", "", group_table(rows, lambda r: r["sent_model"] or "-", "model"), ""]
    lines += ["## By tier", "", group_table(rows, lambda r: r["tier"] or "(unclassified)", "tier"), ""]
    threads = defaultdict(list)
    for r in branch_rows:
        threads[r["thread"]].append(r)
    branch_table = []
    for thread, items in sorted(threads.items(), key=lambda kv: kv[1][0]["id"]):
        t = tokens(items)
        first = items[0]
        branch_table.append([(first["prompt"] or "").replace("|", "/").replace("\n", " ")[:90],
                             first["tier"] or "-", first["requested_model"], first["sent_model"], fmt_int(t["requests"]),
                             fmt_int(t["prompt_total"]), fmt_int(t["output"]), fmt_usd(t["cost"]),
                             fmt_usd(t["unrouted"])])
    lines += ["## Branches (subagents and side calls)", "",
              table(["first prompt", "tier", "asked for", "served by", "requests", "input side", "output", "cost",
                     "unrouted"], branch_table) if branch_table else "none", ""]
    if main_rows:
        m = main_rows[0]
        lines += ["## Main conversation", "",
                  "Classified **%s** (confidence %.3f) on its first prompt and locked to `%s` for %d requests." % (
                      m["tier"], m["confidence"] or 0, m["sent_model"], len(main_rows)), ""]
    reasons = defaultdict(int)
    for r in rows:
        if r["reason"]:
            reasons[r["reason"]] += 1
    if reasons:
        lines += ["## Requests not routed, and why", "",
                  table(["reason", "requests"], sorted(reasons.items(), key=lambda kv: -kv[1])), ""]
    errors = [r for r in rows if r["status"] != 200]
    if errors:
        lines += ["## Non-200 responses", "",
                  table(["status", "role", "model", "reason"], [[r["status"], r["role"], r["sent_model"], r["reason"]]
                                                              for r in errors]), ""]
    if args.checks and os.path.exists(args.checks):
        lines += ["## Independent check of the build", "", open(args.checks, encoding="utf-8").read().strip(), ""]
    text = "\n".join(lines) + "\n"
    out = args.out or os.path.join(args.run, "report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()

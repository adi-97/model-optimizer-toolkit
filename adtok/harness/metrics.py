import json
import os
import sqlite3
from collections import Counter


def usage(db_path):
    totals = {"requests": 0, "input": 0, "cache_write": 0, "cache_write_1h": 0, "cache_read": 0, "output": 0,
              "cost_usd": 0.0, "counterfactual_usd": 0.0, "routed_requests": 0, "errors": 0}
    models = Counter()
    if not os.path.exists(db_path):
        return dict(totals, models={})
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT sent_model, status, applied, input_tokens, cache_write_tokens, "
                            "cache_write_1h_tokens, cache_read_tokens, output_tokens, cost_usd, counterfactual_usd "
                            "FROM requests").fetchall()
    finally:
        conn.close()
    for model, status, applied, inp, cw, cw1, cr, out, cost, cf in rows:
        totals["requests"] += 1
        totals["input"] += inp or 0
        totals["cache_write"] += cw or 0
        totals["cache_write_1h"] += cw1 or 0
        totals["cache_read"] += cr or 0
        totals["output"] += out or 0
        totals["cost_usd"] += cost or 0.0
        totals["counterfactual_usd"] += cf or 0.0
        totals["routed_requests"] += applied or 0
        totals["errors"] += int(status != 200)
        models[model or "-"] += 1
    totals["input_side"] = totals["input"] + totals["cache_write"] + totals["cache_read"]
    totals["cache_miss"] = totals["input"] + totals["cache_write"]
    totals["cache_hit_rate"] = totals["cache_read"] / totals["input_side"] if totals["input_side"] else 0.0
    return dict(totals, models=dict(models))


def combined(proxy_usage, agent_info):
    if proxy_usage.get("requests"):
        keys = ("input", "cache_write", "cache_read", "output", "cost_usd", "input_side", "cache_miss",
                "cache_hit_rate", "models")
        return dict({k: proxy_usage[k] for k in keys}, source="proxy")
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "cost_usd": 0.0}
    models = {}
    for model, u in (agent_info.get("model_usage") or {}).items():
        totals["input"] += int(u.get("inputTokens") or 0)
        totals["cache_write"] += int(u.get("cacheCreationInputTokens") or 0)
        totals["cache_read"] += int(u.get("cacheReadInputTokens") or 0)
        totals["output"] += int(u.get("outputTokens") or 0)
        totals["cost_usd"] += float(u.get("costUSD") or 0.0)
        models[model] = round(float(u.get("costUSD") or 0.0), 4)
    totals["input_side"] = totals["input"] + totals["cache_write"] + totals["cache_read"]
    totals["cache_miss"] = totals["input"] + totals["cache_write"]
    totals["cache_hit_rate"] = totals["cache_read"] / totals["input_side"] if totals["input_side"] else 0.0
    return dict(totals, models=models, source="agent_report")


def transcript(path):
    info = {"result": None, "num_turns": None, "agent_cost_usd": None, "duration_ms": None, "is_error": None,
            "tools": {}, "subagents": 0, "api_key_source": None, "model_usage": {}}
    if not os.path.exists(path):
        return info
    tools = Counter()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                info["api_key_source"] = event.get("apiKeySource")
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools[block.get("name")] += 1
            elif kind == "result":
                info.update(result=event.get("subtype"), num_turns=event.get("num_turns"),
                            agent_cost_usd=event.get("total_cost_usd"), duration_ms=event.get("duration_ms"),
                            is_error=event.get("is_error"), model_usage=event.get("modelUsage") or {})
    info["tools"] = dict(tools)
    info["subagents"] = tools.get("Agent", 0) + tools.get("Task", 0)
    return info

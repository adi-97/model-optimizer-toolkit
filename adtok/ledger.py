import glob
import json
import os
import re
from datetime import datetime

from .proxy.pricing import Pricing

SKIP = (".orphaned-", ".superseded-")
TTL_SECONDS = {"5m": 300, "1h": 3600}
WRITE_MULT = {"5m": 1.25, "1h": 2.0}


def project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def projects_root():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")


def session_files(project=None, root=None):
    root = root or projects_root()
    folders = [os.path.join(root, project_slug(project))] if project else glob.glob(os.path.join(root, "*"))
    if project and not os.path.isdir(folders[0]):
        wanted = project_slug(project).lower()
        folders = [f for f in glob.glob(os.path.join(root, "*")) if os.path.basename(f).lower() == wanted]
    files = []
    for folder in folders:
        files += glob.glob(os.path.join(folder, "*.jsonl"))
        files += glob.glob(os.path.join(folder, "*", "subagents", "agent-*.jsonl"))
    return sorted(f for f in files if not any(s in os.path.basename(f) for s in SKIP))


def iter_records(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_requests(paths, pricing=None, since=0):
    pricing = pricing or Pricing()
    best, compactions, cost_state = {}, {}, {}
    for path in paths:
        for r in iter_records(path):
            kind = r.get("type")
            if kind == "cost-state":
                cost_state[r.get("sessionId")] = {"usd": r.get("totalCostUSD") or 0.0,
                                                    "since": (r.get("startTime") or 0) / 1000}
            elif kind == "system" and r.get("subtype") == "compact_boundary":
                compactions.setdefault(r.get("sessionId"), []).append(parse_ts(r.get("timestamp")) or 0)
            if kind != "assistant":
                continue
            msg = r.get("message") or {}
            usage = msg.get("usage")
            model = msg.get("model")
            if not usage or not model or model == "<synthetic>":
                continue
            key = (msg.get("id"), r.get("requestId"))
            prev = best.get(key)
            if prev and int(prev["usage"].get("output_tokens") or 0) >= int(usage.get("output_tokens") or 0):
                continue
            best[key] = {"session": r.get("sessionId"), "agent": r.get("agentId") if r.get("isSidechain") else "main",
                         "model": model, "ts": parse_ts(r.get("timestamp")) or 0, "version": r.get("version"),
                         "usage": usage}
    rows = sorted((v for v in best.values() if v["ts"] >= since), key=lambda v: v["ts"])
    for row in rows:
        u = row["usage"]
        split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        write = int(u.get("cache_creation_input_tokens") or 0)
        row["input"] = int(u.get("input_tokens") or 0)
        row["write_1h"] = int(split.get("ephemeral_1h_input_tokens") or 0)
        row["write_5m"] = write - row["write_1h"]
        row["write"] = write
        row["read"] = int(u.get("cache_read_input_tokens") or 0)
        row["output"] = int(u.get("output_tokens") or 0)
        row["cost"] = pricing.cost(row["model"], u) or 0.0
    return rows, compactions, cost_state


def group_key(row, by):
    if by == "day":
        return datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d")
    if by == "agent":
        return "main" if row["agent"] == "main" else "subagent"
    return row[by]


def summarize(rows, by="session"):
    groups = {}
    for row in rows:
        g = groups.setdefault(group_key(row, by), {"group": group_key(row, by), "requests": 0, "input": 0,
                                                     "write_5m": 0, "write_1h": 0, "read": 0, "output": 0,
                                                     "cost": 0.0})
        g["requests"] += 1
        for k in ("input", "write_5m", "write_1h", "read", "output", "cost"):
            g[k] += row[k]
    out = sorted(groups.values(), key=lambda g: -g["cost"])
    for g in out:
        prompt = g["input"] + g["write_5m"] + g["write_1h"] + g["read"]
        g["hit_rate"] = g["read"] / prompt if prompt else 0.0
    return out


def streams(rows):
    by = {}
    for row in rows:
        by.setdefault((row["session"], row["agent"]), []).append(row)
    return by


def misses(rows, compactions=None, min_tokens=2000, share=0.5):
    compactions = compactions or {}
    pricing = Pricing()
    found = []
    for (session, agent), seq in streams(rows).items():
        for prev, cur in zip(seq, seq[1:]):
            context = prev["input"] + prev["write"] + prev["read"]
            if cur["write"] < min_tokens or cur["write"] < share * context:
                continue
            gap = cur["ts"] - prev["ts"]
            ttl = TTL_SECONDS["1h"] if prev["write_1h"] or cur["write_1h"] else TTL_SECONDS["5m"]
            if cur["model"] != prev["model"]:
                cause = "model switch %s -> %s" % (prev["model"], cur["model"])
            elif any(prev["ts"] <= t <= cur["ts"] + 600 for t in compactions.get(session, [])):
                cause = "compaction"
            elif cur["version"] != prev["version"]:
                cause = "Claude Code upgrade %s -> %s" % (prev["version"], cur["version"])
            elif gap > ttl:
                cause = "idle %d min > %d min cache lifetime" % (gap // 60, ttl // 60)
            else:
                cause = "prefix changed (tools, MCP, effort, fast mode or settings)"
            price = pricing.lookup(cur["model"]) or {"input": 0}
            mult = 2.0 if cur["write_1h"] else 1.25
            found.append({"session": session, "agent": agent, "ts": cur["ts"], "model": cur["model"],
                          "tokens": cur["write"], "cause": cause,
                          "usd": cur["write"] * price["input"] * (mult - 0.05) / 1e6})
    return sorted(found, key=lambda m: -m["usd"])


def ttl_cost(seq, ttl, pricing=None):
    pricing = pricing or Pricing()
    total, prev = 0.0, None
    for row in seq:
        price = pricing.lookup(row["model"]) or {"input": 0}
        p_in = price["input"]
        p_read = price.get("cache_read", p_in * 0.1)
        context = row["write"] + row["read"]
        warm = prev is not None and row["ts"] - prev["ts"] <= TTL_SECONDS[ttl] and row["model"] == prev["model"]
        kept = min(prev["write"] + prev["read"], context) if warm else 0
        total += (kept * p_read + (context - kept) * p_in * WRITE_MULT[ttl]) / 1e6
        prev = row
    return total


def ttl_advice(rows, pricing=None):
    result = {"5m": 0.0, "1h": 0.0}
    for seq in streams(rows).values():
        for ttl in result:
            result[ttl] += ttl_cost(seq, ttl, pricing)
    best = min(result, key=result.get)
    return {"cache_usd_5m": result["5m"], "cache_usd_1h": result["1h"], "cheaper": best,
            "saving_usd": abs(result["5m"] - result["1h"])}

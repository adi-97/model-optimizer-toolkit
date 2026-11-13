import math
from collections import defaultdict

from .metrics import combined


def tok(r):
    return r.get("tokens") or combined(r.get("usage") or {}, r.get("agent") or {})


METRICS = [
    ("cost_usd", "cost $", lambda r: tok(r)["cost_usd"], "%.2f"),
    ("output", "output tok", lambda r: tok(r)["output"], "%.0f"),
    ("cache_miss", "cache-miss tok", lambda r: tok(r)["cache_miss"], "%.0f"),
    ("cache_read", "cache-read tok", lambda r: tok(r)["cache_read"], "%.0f"),
    ("wall_min", "wall min", lambda r: r["wall_min"], "%.1f"),
    ("turns", "turns", lambda r: r["agent"]["num_turns"] or 0, "%.0f"),
    ("tool_calls", "tool calls", lambda r: sum(r["agent"]["tools"].values()), "%.0f"),
]


def mean_sd(values):
    values = [float(v) for v in values]
    if not values:
        return None, None
    m = sum(values) / len(values)
    if len(values) < 2:
        return m, None
    return m, math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def fmt(m, sd, pattern):
    if m is None:
        return "-"
    return (pattern % m) + ("" if sd is None else " ± " + pattern % sd)


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return "\n".join(lines + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def verdict(a, b):
    ma, sa = mean_sd(a)
    mb, sb = mean_sd(b)
    if ma is None or mb is None:
        return "-", "-"
    delta = "%+.1f%%" % (100 * (ma - mb) / mb) if mb else "-"
    if sa is None or sb is None:
        return delta, "n<2 per arm: cannot separate from noise"
    se = math.sqrt(sa ** 2 / len(a) + sb ** 2 / len(b))
    if se == 0:
        return delta, "no variance"
    z = abs(ma - mb) / se
    return delta, "likely real (%.1f SE)" % z if z >= 2 else "within noise (%.1f SE)" % z


def build(results, baseline_arm=None, title="Token benchmark"):
    groups = defaultdict(list)
    for r in results:
        if not str(r.get("status", "")).startswith("skipped") and ("usage" in r or "tokens" in r):
            groups[(r["task"], r["arm"])].append(r)
    lines = ["# %s" % title, "", "%d runs. Values are mean ± sample standard deviation across repeats; cost is the "
             "list-price equivalent measured by the proxy." % sum(len(v) for v in groups.values()), ""]
    for task in sorted({t for t, _ in groups}):
        arms = sorted({a for t, a in groups if t == task}, key=lambda a: (a != baseline_arm, a))
        lines += ["## %s" % task, ""]
        rows = []
        for arm in arms:
            runs = groups[(task, arm)]
            accepted = [r for r in runs if r["accepted"]]
            total_cost = sum(tok(r)["cost_usd"] for r in runs)
            per_accepted = "$%.2f" % (total_cost / len(accepted)) if accepted else "no accepted run"
            row = [arm, len(runs), "%d/%d" % (len(accepted), len(runs)), per_accepted]
            for _, _, getter, pattern in METRICS:
                row.append(fmt(*mean_sd([getter(r) for r in runs]), pattern))
            rows.append(row)
        lines += [table(["arm", "n", "accepted", "$ per accepted task"] + [m[1] for m in METRICS], rows), ""]
        if baseline_arm and (task, baseline_arm) in groups:
            base = groups[(task, baseline_arm)]
            comp = []
            for arm in arms:
                if arm == baseline_arm:
                    continue
                runs = groups[(task, arm)]
                for key, label, getter, _ in METRICS[:5]:
                    delta, note = verdict([getter(r) for r in runs], [getter(r) for r in base])
                    comp.append([arm, label, delta, note])
            if comp:
                lines += ["vs `%s`:" % baseline_arm, "", table(["arm", "metric", "change", "significance"], comp), ""]
        failures = defaultdict(int)
        for arm in arms:
            for r in groups[(task, arm)]:
                for c in r["checks"]:
                    if not c["passed"]:
                        failures[(arm, c["name"])] += 1
        if failures:
            lines += ["Failed checks:", "", table(["arm", "check", "runs failed"],
                                                  [[a, c, n] for (a, c), n in sorted(failures.items())]), ""]
        models = defaultdict(lambda: defaultdict(float))
        sources = defaultdict(set)
        for arm in arms:
            for r in groups[(task, arm)]:
                t = tok(r)
                sources[arm].add(t.get("source", "proxy"))
                share = {m: float(v) for m, v in t["models"].items()}
                if t.get("source") == "proxy":
                    share = {m: n / max(1, sum(share.values())) * t["cost_usd"] for m, n in share.items()}
                for m, v in share.items():
                    models[arm][m] += v
        lines += ["Cost by serving model (measured by `proxy`, or by Claude Code's own `agent_report` when the arm "
                  "sends no traffic through the proxy; proxy rows split cost by request count):", "",
                  table(["arm", "measured by", "models"], [
                      [a, ", ".join(sorted(sources[a])),
                       ", ".join("%s $%.2f" % kv for kv in sorted(models[a].items(), key=lambda kv: -kv[1]))]
                      for a in arms]), ""]
    return "\n".join(lines) + "\n"

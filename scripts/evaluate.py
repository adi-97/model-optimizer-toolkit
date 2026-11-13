import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adtok import QUESTION_ID, TIERS, TierClassifier


def read(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def score(gold, preds):
    confusion = {g: {p: 0 for p in TIERS} for g in TIERS}
    for g, p in zip(gold, preds):
        confusion[g][p] += 1
    n = max(1, len(gold))
    rank = TIERS.index
    return {"n": len(gold),
            "accuracy": round(sum(confusion[t][t] for t in TIERS) / n, 4),
            "under_tiered": round(sum(v for g in TIERS for p, v in confusion[g].items() if rank(p) < rank(g)) / n, 4),
            "over_tiered": round(sum(v for g in TIERS for p, v in confusion[g].items() if rank(p) > rank(g)) / n, 4),
            "confusion": confusion}


def evaluate(clf, rows):
    tasks = [r["state"]["task"] for r in rows]
    gold = [r["expected"][QUESTION_ID] for r in rows]
    sources = [r.get("source", "own") for r in rows]
    clf.classify_batch(tasks[:4])
    start = time.perf_counter()
    preds = clf.classify_batch(tasks)
    batch_ms = (time.perf_counter() - start) * 1000 / len(tasks)
    single = sorted(clf.classify(t)["latency_ms"] for t in tasks[:20])
    tiers = [p["tier"] for p in preds]
    report = {"overall": score(gold, tiers), "by_source": {}}
    for s in sorted(set(sources)):
        idx = [i for i, x in enumerate(sources) if x == s]
        report["by_source"][s] = score([gold[i] for i in idx], [tiers[i] for i in idx])
    report["batch_ms_per_item"] = round(batch_ms, 2)
    report["single_call_ms_median"] = round(single[len(single) // 2], 2)
    report["misses"] = [{"source": s, "gold": g, "pred": p["tier"], "confidence": round(p["confidence"], 3), "task": t}
                        for t, g, p, s in zip(tasks, gold, preds, sources) if g != p["tier"]]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="convaiinnovations/laya")
    parser.add_argument("--device", default=None)
    parser.add_argument("--data", default=os.path.join(ROOT, "data", "eval.jsonl"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    clf = TierClassifier.load(args.model, device=args.device)
    report = evaluate(clf, read(args.data))
    report["model"] = args.model
    print(json.dumps({k: v for k, v in report.items() if k != "misses"}, indent=1))
    print("misses: %d" % len(report["misses"]))
    for m in report["misses"]:
        print("  %-4s %-7s -> %-7s %.3f  %s" % (m["source"], m["gold"], m["pred"], m["confidence"], m["task"][:90]))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()

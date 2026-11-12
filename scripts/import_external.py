import argparse
import csv
import hashlib
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LABELS = {"simple": "simple", "medium": "mid", "mid": "mid", "complex": "complex"}

RELABEL = {
    "Build thread-safe in-memory LRU cache with TTL expiration and lock management.": "mid",
    "Implement optimistic concurrency control mechanism for concurrent DB edits.": "mid",
    "Implement batch async request throttling with exponential backoff and jitter.": "mid",
    "Resolve N+1 query bottleneck in nested object serialization using eager joins.": "mid",
    "Write aggregated SQL query for monthly user growth statistics.": "simple",
    "Add tenant scoping to all repository queries": "complex",
}

VARIANT = re.compile(r"\s*\((?:variant|Variant) \d+[^)]*\)")


def from_csv(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            title = VARIANT.sub("", r["task_title"]).strip()
            prompt = VARIANT.sub("", r["task_prompt"]).strip()
            rows.append({"source": "csv", "id": r["task_id"], "key": title, "text": prompt,
                         "original": LABELS[r["complexity"].strip().lower()]})
    return rows


def from_json(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in json.load(f):
            text = VARIANT.sub("", r["task_description"]).strip()
            rows.append({"source": "json", "id": r["id"], "key": text, "text": text,
                         "original": LABELS[r["tier"].strip().lower()]})
    return rows


def dedupe(rows):
    seen = {}
    for r in rows:
        seen.setdefault((r["source"], r["key"]), r)
    return list(seen.values())


def split(rows, eval_frac):
    out = []
    groups = {}
    for r in rows:
        groups.setdefault((r["source"], r["label"]), []).append(r)
    for _, group in sorted(groups.items()):
        group.sort(key=lambda r: hashlib.sha256(r["key"].encode("utf-8")).hexdigest())
        n_eval = round(len(group) * eval_frac)
        for i, r in enumerate(group):
            out.append(dict(r, split="eval" if i < n_eval else "train"))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--json", required=True)
    parser.add_argument("--eval-frac", type=float, default=0.2)
    parser.add_argument("--out", default=os.path.join(ROOT, "data", "external.json"))
    args = parser.parse_args(argv)
    raw = from_csv(args.csv) + from_json(args.json)
    rows = dedupe(raw)
    unused = set(RELABEL) - {r["key"] for r in rows}
    if unused:
        raise SystemExit("relabel keys not found: %r" % sorted(unused))
    for r in rows:
        r["label"] = RELABEL.get(r["key"], r["original"])
    rows = split(rows, args.eval_frac)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    print("read %d rows, %d unique, %d relabelled" % (len(raw), len(rows),
                                                     sum(r["label"] != r["original"] for r in rows)))
    for s in ("train", "eval"):
        for src in ("csv", "json"):
            counts = {t: sum(1 for r in rows if r["split"] == s and r["source"] == src and r["label"] == t)
                      for t in ("simple", "mid", "complex")}
            print("  %-5s %-4s %s" % (s, src, counts))
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()

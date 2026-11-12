import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "data"))

from laya_tier.questions import QUESTION_ID, QUESTIONS, state_for
import prompts

EXTERNAL = os.path.join(ROOT, "data", "external.json")


def load_rows():
    rows = [("own", "train", l, t) for l, t in prompts.TRAIN] + [("own", "eval", l, t) for l, t in prompts.EVAL]
    if os.path.exists(EXTERNAL):
        with open(EXTERNAL, encoding="utf-8") as f:
            rows += [(r["source"], r["split"], r["label"], r["text"]) for r in json.load(f)]
    return rows


def write(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        for source, _, label, task in rows:
            f.write(json.dumps({"state": state_for(task), "questions": QUESTIONS,
                                "expected": {QUESTION_ID: label}, "source": source}) + "\n")
    print("%s: %d rows" % (path, len(rows)))


if __name__ == "__main__":
    rows = load_rows()
    texts = {}
    for r in rows:
        texts.setdefault(r[3], set()).add(r[1])
    leaked = [t for t, s in texts.items() if len(s) > 1]
    if leaked:
        raise SystemExit("tasks in both splits: %r" % leaked[:5])
    write([r for r in rows if r[1] == "train"], os.path.join(ROOT, "data", "train.jsonl"))
    write([r for r in rows if r[1] == "eval"], os.path.join(ROOT, "data", "eval.jsonl"))

# Training the tier classifier

The tier classifier reads a coding task and labels it `simple`, `mid` or `complex`. adtok uses the label for the `Task tier:` hint (hybrid profile) and for proxy routing. It is a [Laya](https://github.com/NandhaKishorM/laya) decision model fine-tuned on about 350 labelled tasks. Checkpoints are 1.6 GB and are not in git, so a fresh clone has to train one or turn the classifier off (`routing.classifier = false`). A GPU is much faster; a CPU works.

## Quick path

```powershell
./install.ps1 -Train            # venv, laya + torch, dataset, training into models/tier-full-v1
.venv\Scripts\python scripts\evaluate.py --model models\tier-full-v1
```

`./install.sh --train` does the same on Linux and macOS. The steps below are what the script runs, so you can change them.

## 1. The data

Every row is one task and its label. The question the model answers lives in [adtok/questions.py](../adtok/questions.py):

| label | meaning |
|---|---|
| `simple` | a direct answer or one small edit: explain, rename, typo, one-liner, run a command |
| `mid` | a contained change in a few files: fix a located bug, add a small feature, write tests |
| `complex` | multi-file or open-ended: refactor, design, unknown root cause, migration, performance |

Rows come from two places:

- **[data/prompts.py](../data/prompts.py)**: hand-written `TRAIN` and `EVAL` lists of `(label, text)`. Add your own tasks here. The real prompts you type into Claude Code are the best examples.
- **data/external.json** (optional): imported task sets. `scripts/import_external.py` reads a CSV with columns `task_id, task_title, task_prompt, complexity` and a JSON list with `id, task_description, tier`. It maps `medium` to `mid`, drops `(variant N)` duplicates, applies the hand corrections in its `RELABEL` table and splits 80/20 by a hash of the text, so the split is the same on every run.

```bash
python scripts/import_external.py --csv tasks.csv --json tasks.json   # optional
python scripts/build_dataset.py                                       # writes data/train.jsonl and data/eval.jsonl
```

`build_dataset.py` stops if the same task text is in both splits. Each line it writes is a Laya training item:

```json
{"state": {"task": "Fix the typo 'recieve' in the README"}, "questions": {"tier": {...}}, "expected": {"tier": "simple"}, "source": "own"}
```

Labelling tips:

- Label by the work the agent must do, not by how long the prompt is.
- When unsure between two tiers, take the higher one. Sending a hard task to Haiku costs more than sending an easy task to Sonnet.
- Keep the three classes roughly balanced, and keep the eval split for evaluation only.

## 2. Check before training

```bash
laya-train --data data/train.jsonl --eval-data data/eval.jsonl --dry-run
```

It parses the items and prints counts without loading weights.

## 3. Train

```bash
laya-train --data data/train.jsonl --eval-data data/eval.jsonl --base english --epochs 6 \
           --micro-batch 8 --grad-accum 1 --out models/tier-full-v1
```

Add `--device cuda` for a GPU. With few rows, keep `--grad-accum 1` so the model still gets enough optimizer steps. Other useful flags: `--seed`, `--label-smoothing 0.1`, `--shuffle-options` and `--loss soft-ce`.

What was tried on this dataset (126 held-out tasks):

| run | setup | held-out accuracy |
|---|---|---|
| zero-shot | base `english`, no training | 0.579 |
| `tier-frozen-v1` | `--freeze-encoder`, 10 epochs | 0.587 |
| `tier-frozen-v2` | `--freeze-encoder --loss soft-ce`, 15 epochs | 0.579 |
| **`tier-full-v1`** | full fine-tune, 6 epochs | **0.865** |

Training only the heads did not help. The encoder has to learn what makes a task harder. The run writes `train_report.json` into the output folder with before/after accuracy, calibration and the fitted thresholds. On this data, calibration was fitted on only 35 items and the temperature hit its clamp. Trust the confidence numbers less than the accuracy.

## 4. Evaluate

```bash
python scripts/evaluate.py --model models/tier-full-v1 --device cuda --out eval.json
```

The output includes:

- **accuracy**, overall and per source
- **under_tiered**: tasks labelled lower than they should be. This is the costly mistake; `tier-full-v1` labelled no complex task simple.
- **over_tiered**
- the confusion matrix
- per-item latency (about 34 ms)
- every miss, with its confidence

Read the misses. Most of them point either at a wrong label in the data or at a kind of task the data is missing.

## 5. Try it and use it

```bash
adtok classify "Rename tmp to retry_count in utils/retry.py" "Migrate the auth layer to OAuth2"
adtok serve                               # the hint hook calls http://127.0.0.1:8787/__adtok/classify
```

The model path is `classifier_model` in `adtok.hybrid.toml` / `adtok.example.toml`. The default is `models/tier-full-v1`. `min_confidence` (default 0.6) moves an uncertain answer up one tier. Raise it to send more borderline tasks to the stronger model.

To retrain after adding data, rerun steps 1 to 4 into a new folder (for example `models/tier-full-v2`). Compare it with `evaluate.py` against the same `data/eval.jsonl`, then point `classifier_model` at the better model.

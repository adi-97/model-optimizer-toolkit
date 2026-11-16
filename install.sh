#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export USE_TF=0
classifier=0 train=0 user=0
for arg in "$@"; do
  case "$arg" in
    --classifier) classifier=1 ;;
    --train) classifier=1; train=1 ;;
    --user) user=1 ;;
    *) echo "usage: ./install.sh [--classifier] [--train] [--user]"; exit 2 ;;
  esac
done
if [ ! -d .venv ]; then
  python="${PYTHON:-python3}"
  if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "adtok needs Python 3.11+; $python is $("$python" -V 2>/dev/null || echo missing)."
    echo "point PYTHON at a newer interpreter, e.g. PYTHON=python3.12 ./install.sh"
    exit 1
  fi
  "$python" -m venv .venv
fi
py=.venv/bin/python
[ -x "$py" ] || py=.venv/Scripts/python
"$py" -m pip install -q --upgrade pip
if [ "$classifier" = 1 ]; then
  [ -f ../laya-model/pyproject.toml ] && "$py" -m pip install -q -e ../laya-model
  "$py" -m pip install -q -e ".[classifier]"
else
  "$py" -m pip install -q -e .
fi
if [ "$train" = 1 ] && [ ! -f models/tier-full-v1/model.safetensors ]; then
  "$py" scripts/build_dataset.py
  "$py" -m laya.train_cli --data data/train.jsonl --eval-data data/eval.jsonl --base english --epochs 6 \
    --micro-batch 8 --grad-accum 1 --out models/tier-full-v1
fi
if [ "$user" = 1 ]; then
  if [ -f models/tier-full-v1/model.safetensors ]; then "$py" -m adtok.cli init --user
  else "$py" -m adtok.cli init --user --set routing.classifier=false; fi
fi
echo "adtok is installed: $(pwd)/$(dirname "$py")/adtok"

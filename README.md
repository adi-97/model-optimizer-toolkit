# laya-tier

A task-complexity classifier built on [Laya](../laya-model), and a local proxy that uses it to route coding-agent requests to a cheaper Claude model when the task allows, while tracking every token.

```
coding agent ──► laya-tier proxy (localhost) ──► api.anthropic.com
                   │ classify the task: simple / mid / complex
                   │ pick the model, lock it for the session
                   └ record tokens and cost in SQLite
```

| tier    | model               | effort                        |
|---------|---------------------|-------------------------------|
| simple  | `claude-haiku-5-5`  | `low`                         |
| mid     | `claude-sonnet-5-5` | `medium`                      |
| complex | `claude-opus-5-5`   | unchanged (the agent's own)   |

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv/Scripts/python -m pip install -e ../laya-model -e .
```

The proxy loads the classifier from `models/tier-full-v1` (see [Training the classifier](#training-the-classifier)).

## Run

```bash
laya-tier serve --shadow -v        # classify and log only; requests go through unchanged
laya-tier serve -v                 # route for real
```

Point the agent at it. Claude Code, PowerShell:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
claude
```

Start in `--shadow` mode for a few sessions: nothing is rewritten, and `laya-tier stats` shows what routing would have cost.

```bash
laya-tier stats --since 7d --by tier      # also: model, route, role, source, session, day
laya-tier classify "Fix the typo in the README" "Migrate auth to JWT across web and mobile"
```

`cost_$` is what the tokens cost on the model that served them. `unrouted_$` prices the same tokens at the model the agent asked for (in shadow mode: at the model the proxy would have picked). The saving is an estimate: a different model produces a different number of tokens.

## How routing decides

- **Main conversation: one decision per session.** The first prompt of at least `lock_min_words` words is classified, and the main conversation keeps that model for the rest of the session — tool loops, follow-ups and compaction included. The main thread is recognised by its system prompt and tool set, so a compacted history is still the same thread.
- **Manual switches win.** If the agent starts asking for a different model mid-session (e.g. `/model` in Claude Code), the proxy stops routing that thread and forwards the model you chose.
- **Branches are routed on their own.** A request with a different system prompt or tool set in the same session (subagents, forked skills, background jobs) is a branch: it is classified once from its own task prompt and locked for its lifetime.
- **Utility calls go to the simple tier.** A small branch request with no tools (session titles, one-line summaries; at most `utility_max_chars` characters) is sent to `utility_tier` without classification, because its text often quotes the task itself. Set `utility_tier = ""` to classify them instead.
- **Never upgraded:** requests that ask for a model in `never_upgrade_from` (Haiku by default — the agent's own background calls).
- **Not routed:** requests the target model would reject (forced `tool_choice` on models that 400 on it, `speed: "fast"` where unsupported, server-side `fallbacks`), and unknown models.
- **Safety net:** if the upstream rejects a rewritten request (400/404/422), the original request is sent unchanged and the row is marked `retried`.
- `session_lock = false` switches to per-prompt routing (each new human prompt can change the model).

Why the session lock matters: prompt caches belong to one model, and thinking blocks are bound to the model and conversation that produced them. Switching the main conversation's model re-sends the whole history uncached and drops the earlier reasoning. Switching only at session start and in branches avoids that.

## Token levers

| lever | config | default |
|---|---|---|
| model routing by tier | `[routes.*]` | on |
| effort per tier (only applied when the model is switched, so an unswitched request is forwarded byte for byte) | `routes.*.effort` | simple `low`, mid `medium` |
| context trimming — Anthropic's server-side context editing (`clear_tool_uses_20250919`), added for the listed tiers | `context_editing_tiers` | off |
| budgets | `[budget] daily_usd`, `session_usd`, `action = warn \| downgrade \| block` | none |

The proxy never edits conversation history itself: on current models that breaks the thinking-block check (a 400 for newer accounts) and the prompt cache. Context editing runs on Anthropic's side instead.

`downgrade` routes everything to the simple tier once a budget is spent, which switches the model mid-session; `block` answers 429 with a message naming the budget.

## Configuration

Copy `laya-tier.example.toml`, edit, and pass `--config`. Prices are per million tokens and can be overridden under `[prices."model-id"]` with `input`, `output`, `cache_read`, `cache_write`. The defaults come from Anthropic's published list as of 2026-11-03; cache writes are priced at 1.25x input.

Prompts are not stored unless `store_prompts = true`; rows carry a hash of the prompt.

## Training the classifier

```bash
python scripts/import_external.py --csv <tasks.csv> --json <tasks.json>
python scripts/build_dataset.py
laya-train --data data/train.jsonl --eval-data data/eval.jsonl --base english --epochs 6 \
           --micro-batch 8 --grad-accum 1 --device cpu --out models/tier-full-v1
python scripts/evaluate.py --model models/tier-full-v1 --device cuda
```

`models/tier-full-v1`: 0.865 held-out accuracy (126 items) against 0.579 zero-shot; no complex task was classified simple; 8 of 126 complex tasks were classified mid. Single call ~34 ms on an RTX 3050.

## Tests

```bash
python tests/test_proxy.py
```

Offline: a mock upstream and a stub classifier, no API key and no torch.

## Limits

- Verified against a mock upstream and the real classifier; not yet against the live API. Authentication headers are forwarded unchanged; API-key auth (`x-api-key`) is the expected path.
- Only the Anthropic Messages API (`/v1/messages`) is routed; every other path is passed through.
- Session and thread state is in memory: a proxy restart re-classifies on the next prompt.

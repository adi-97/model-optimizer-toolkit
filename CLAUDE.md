# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

adtok (published as model-optimizer-toolkit) is one package that cuts and measures Claude Code's token cost by stacking layers: model routing (a Laya tier classifier, native subagents, hooks), context (compaction window, handoff), tool-output guards and output style. They are installed from one config (`.claude/adtok.toml`) and checked by a doctor (prefix audit, cache invalidators, conflicts). A transcript ledger, an optional proxy and a benchmark harness measure the result in dollars per accepted task.

## Commands

The project venv is `.venv` (Python 3.11, CUDA torch, `laya` installed editable from `../laya-model`). Set `USE_TF=0` for anything that loads the classifier.

```bash
.venv/Scripts/python tests/test_layers.py     # layer config, install/uninstall, tool and compact hooks, ledger, doctor
.venv/Scripts/python tests/test_proxy.py      # proxy routing, streaming, budgets, hint headers
.venv/Scripts/python tests/test_hybrid.py     # installer, hooks, classify endpoint
.venv/Scripts/python tests/test_harness.py    # suite parsing, workspaces, fake-agent end-to-end runs
.venv/Scripts/ruff check adtok/ tests/ scripts/ bench/ --select=E9,F63,F7,F82,F401,F811 --line-length=120
.venv/Scripts/python -m compileall -q adtok tests scripts bench
```

The test files are plain scripts that print PASS/FAIL and exit non-zero on failure; run one test by calling its function from a `-c` one-liner. They run offline: fixture transcripts, a mock upstream, `classifier_model = "stub:<tier>"` and `tests/fake_claude.py` stand in for the API, the model and Claude Code.

```bash
adtok classify "Fix the typo in the README"              # tier + route with the real classifier
adtok serve [--config adtok.hybrid.toml] [--shadow]  # proxy + classify endpoint on :8787
adtok init <project> [--set tools.test_output=true]       # install layers from .claude/adtok.toml; uninstall reverses it
adtok doctor <project>                                    # prefix tokens, cache invalidators, layer conflicts
adtok ledger [usage|misses|ttl] --project <dir>           # cost from ~/.claude/projects transcripts
adtok bench plan|run|report bench/core/suite.toml        # benchmark; results go outside the repo
adtok stats --since 7d --by route                        # proxy usage ledger
```

Classifier training: `scripts/import_external.py` then `scripts/build_dataset.py`, then `laya-train` (see README), then `scripts/evaluate.py`. Full fine-tuning does not fit the 4 GB GPU; it runs on CPU (about 45 min). `models/` is gitignored; the routing code expects `models/tier-full-v1`.

## Architecture

- `adtok/questions.py`, `classifier.py`: one Laya choice question (`tier`: simple/mid/complex). `TierClassifier.classify` returns tier, confidence and probabilities, and moves up one tier below `min_confidence`.
- `adtok/proxy/`: Starlette app (`server.py`) in front of `api.anthropic.com`. `policy.Router` decides per request; `rewrite` changes only `model`, `output_config.effort` and an incompatible `thinking` setting. Never edit `system`, `tools` or earlier messages: that breaks prompt caching and the preserved-thinking prefix check (400). Traffic is classified from `x-claude-code-*` hint headers when present (main, subagent, fork, compaction, auxiliary), else from system prompt + tool signature. The main conversation is locked per session (no per-prompt switching); `main_routing = false` makes the proxy observe only. `tracker.py` is the SQLite ledger; `pricing.py` holds list prices, including 1-hour cache writes at 2x. Extra endpoints: `/__adtok/health`, `/__adtok/classify`.
- `adtok/layers.py`: the layer toggles (`DEFAULTS`, the `adtok.toml` template) and `plan()`, which maps them to settings keys, env vars and hook registrations.
- `adtok/hybrid/`: `install.py` applies a plan: `templates/agents/*.md` (tracked by hash so upgrades replace them), the `CLAUDE.md` block between `adtok` markers, settings/env/hooks merged into `.claude/settings.local.json` keeping any value the user set, and a manifest that `uninstall` reverses. Re-running `init` uninstalls first. `hooks.py` is the single dispatcher per event: `UserPromptSubmit` tier hint (fails open), `PreModelSwitch` cache guard, `PreToolUse` (allowlisted test-command wrapper, Read `limit`, Grep `head_limit`; copies every input field, skips auto mode, never edits output), `PreCompact` keep-list and `PostCompact` handoff note.
- `adtok/digest.py`: stdin filter behind the test wrapper; above 8,000 characters it prints error lines + tail + the saved log path.
- `adtok/ledger.py`: transcript reader (dedupe by message id + request id, subagent files, 1h/5m writes), `misses` (cache rebuilds with a cause) and `ttl_advice` (re-price real gaps under 5m vs 1h). `adtok/doctor.py`: offline prefix audit and lint rules.
- `adtok/harness/`: suite TOML (`suite.py`), fresh workspace per run from git archive + patch or a copy (`workspace.py`), one proxy per run on a free port, killed by process handle (`runner.py`), hidden acceptance checks, and a report with mean ± sd and a noise test (`report.py`). Arms with `route_traffic = false` are measured from Claude Code's own `modelUsage`; arms that route through the proxy need `ANTHROPIC_API_KEY` unless the suite sets `allow_subscription_proxy = true`.
- `bench/core/`: arms `baseline`, `concise`, `native`, `hybrid`, `ensemble`, `ensemble-tools`, `routed` (arms set `layers` overrides); the `bugfix-structured` task (planted bug in a `laya-model` copy) and the `greenfield-genexp` task (hidden checker in `check.py`).
- `scripts/`: classifier dataset build (`import_external.py`, `build_dataset.py`) and evaluation (`evaluate.py`).

## Conventions

- No comments or docstrings in code. Explanations go in README or commit messages.
- Prices, model IDs and Claude Code behaviour are as of November 2026; `reports/` has the sources.
- Layers may only change what enters the conversation for the first time. Never rewrite earlier messages, and register at most one adtok hook per event. Tool-output layers stay off by default until the benchmark shows a lower cost per accepted task. Re-check them before relying on them.
- Default to measuring without the proxy. Routing a claude.ai subscription through a proxy is not explicitly permitted; use an API key for proxy runs.
- Real benchmark runs spend plan usage or money. Ask before starting one.

<!-- adtok:begin -->
## Model routing

- Exploration that spans more than a couple of files: use the `Explore` agent (Haiku). Do small lookups yourself.
- Independent, well-scoped implementation pieces, especially several that can run in parallel: delegate to `implementer` (Sonnet) with the files, the change and how to verify it.
- A bug with a known failing test or error: `fixer` (Sonnet, high effort).
- Design across modules, plans for large changes, and reviews of finished work: `architect` (Opus, read-only).
- Keep planning, integration, and any step that depends on what you have already read in this conversation here. Do not split a dependent chain of steps across agents.
- A `Task tier:` note may be attached to a prompt by a hook. Treat it as advice, not an order.
- After a delegated change, run its tests yourself. If they fail, retry at a higher tier (`implementer` -> do it yourself; `low`/`medium` -> `high`) instead of repeating the same attempt.
<!-- adtok:end -->

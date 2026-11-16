# adtok: token optimizer for Claude Code

One package that cuts and measures the token cost of Claude Code by stacking several techniques as layers. You get one install, one config file with a switch per layer, one ledger to measure the result and one conflict check. A benchmark shows whether a combination actually lowers dollars per accepted task.

Why these layers: on Opus 5.5 and Sonnet 5.5 a cache read costs 0.05× input. The bill is driven by cache *writes*, output and context growth, not by bytes of tool output. Billed studies found that blanket output compression saves tokens and loses money. So the layers that shrink or protect the cache are on by default, and the tool-output layers stay off until the benchmark justifies them. Research and sources: [token reduction techniques](reports/Claude%20Code%20token%20reduction%20techniques.md) and [model routing hybrid](reports/Claude%20Code%20model%20routing%20hybrid.md).

## Install

```powershell
./install.ps1 -User                # Windows: core layers for every project, no torch, no classifier
./install.ps1 -Train -User         # also installs Laya + torch, trains the tier classifier, enables the hint
```

```bash
./install.sh --user                # Linux/macOS; --classifier installs Laya only, --train also trains
```

Both scripts check for Python 3.11+ before they create `.venv`. Set `PYTHON` to use another interpreter, such as a conda or pyenv one: `PYTHON=~/.pyenv/versions/3.12.4/bin/python ./install.sh`, or `$env:PYTHON = "C:\Python312\python.exe"` before `./install.ps1`. After install, the CLI is `.venv\Scripts\adtok` on Windows and `.venv/bin/adtok` on Linux and macOS.

The core (layers, doctor, ledger, skills, hooks) uses only the Python standard library (3.11+). Without `-User`, the script installs only the package, and you then choose a scope with `adtok init`. By hand:

- `pip install -e .` installs the core.
- `pip install -e ".[proxy]"` adds `adtok serve` without the model.
- `pip install -e ".[classifier]"` adds Laya and torch. For a CUDA build, install torch from the PyTorch index first. To train the model, see the [training guide](docs/training.md).

## Use

```bash
adtok init path/to/project                          # writes .claude/adtok.toml on first run, installs enabled layers
adtok init path/to/project --set tools.test_output=true   # saves the value to adtok.toml, then installs
adtok init path/to/project --unset tools.test_output     # puts the key back to its default
adtok init path/to/project --set routing.classifier=false # tier classifier off: no hint hook, no adtok serve needed
adtok doctor path/to/project                        # prefix size, cache invalidators, layer conflicts
adtok skills path/to/project                        # skill listing cost, body size, uses and last use per skill
adtok ledger --project path/to/project --by model   # tokens and $ from Claude Code's own transcripts
adtok ledger misses --project path/to/project       # each cache rebuild, its cost and its likely cause
adtok ledger ttl --project path/to/project          # your sessions re-priced under 5m vs 1h cache lifetime
adtok uninstall path/to/project                     # removes exactly what init added
adtok init --user                                   # every project: ~/.claude/settings.json, CLAUDE.md, agents/
adtok uninstall --user                              # undo the user-level install
```

`--user` works like RTK's global install. It writes adtok's block into `~/.claude/CLAUDE.md` and its hooks and settings into `~/.claude/settings.json`, and keeps its config in `~/.claude/adtok.toml`. A project-level install still wins inside that project. Before it writes, it copies `settings.json` to `settings.json.adtok-<timestamp>.bak` and keeps the newest five. Every file is written to a temporary file and then swapped in, so an interrupted run never leaves a half-written settings file. Values are checked before anything is touched: a bad `--set` or an unreadable `settings.json` stops `init` with nothing changed.

With the default config, a user-wide install changes these for **every** project and session:

| What | Value | Effect |
|---|---|---|
| `CLAUDE_CODE_SUBAGENT_MODEL` | `sonnet` | subagents without their own `model:` run on Sonnet, not the main model |
| `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL` | `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5` | the `haiku`/`sonnet`/`opus` aliases are pinned to these IDs until you re-run `init` with a newer adtok |
| `CLAUDE_CODE_GATEWAY_HINT_HEADERS` | `1` | requests carry `x-claude-code-*` headers (used by `adtok serve`) |
| `autoCompactWindow` | `250000` | auto-compaction starts at 250K tokens instead of near the model's full window |
| hooks | `UserPromptSubmit`, `PreModelSwitch`, `PreCompact`, `PostCompact` | tier hint, cache guard on `/model`, compaction keep-list and handoff note; each runs the venv's Python |
| agents | `Explore`, `architect`, `fixer`, `implementer` in `~/.claude/agents/` | the routing subagents |

Any of these you had already set is kept and reported. `adtok uninstall --user` removes exactly what was added. The hooks run the venv's Python by absolute path, so if you move or rebuild the venv, re-run `init`; `adtok doctor` warns when a hook's interpreter is missing or cannot import adtok.

`init` keeps existing agents, settings and `CLAUDE.md` content, and reports any value it left alone. Re-run it after editing `adtok.toml`. Values passed with `--set` or `--unset` are written into `adtok.toml`, so later runs keep them. `uninstall` removes a setting only if it still has the value adtok wrote; anything you changed afterwards stays.

## Layers

| layer | `adtok.toml` | default | what it installs |
|---|---|---|---|
| routing | `[routing] enabled, profile, classifier` | on, `hybrid`, on | `Explore` on Haiku (with `autoCompactWindow: 100000`, below Haiku's 5× price step), `implementer`/`fixer` on Sonnet, `architect` on Opus, a `CLAUDE.md` delegation block, model aliases pinned to 5.5. `hybrid` adds a `Task tier:` hint hook (needs `adtok serve`; `classifier = false` drops it and the model is never loaded) and a `PreModelSwitch` guard that asks before a switch re-writes more than `switch_ask_usd` of cache |
| context | `compact_window` | `250000` | `autoCompactWindow`: 1M-context models otherwise compact near 967K tokens, re-reading up to 4× more context per request |
| context | `handoff` | on | `PreCompact` tells the summary what to keep (task, changed files, exact failing tests, decisions); `PostCompact` saves the summary to `.claude/adtok-handoff.md`, so `/clear` plus the note can replace a long session |
| context | `quiet_background` | off | stops goal check-ins and holds cross-session messages, which re-read context while you are idle |
| tools | `test_output` | off | `PreToolUse` wraps allowlisted test/build/lint commands: output over 8,000 characters is cut to matching error lines plus the last 60 lines and a path to the full log; the exit code is kept. Skipped in auto mode, for compound commands and for anything not on the allowlist |
| tools | `read_guard` | off | adds `limit` to Reads of files over `read_max_lines` and `head_limit` to content Greps; never edits output, so Read de-duplication and edit anchors stay intact |
| tools | `output_caps` | off | `bashOutputMaxChars` and `MAX_MCP_OUTPUT_TOKENS` |
| output | `concise` | off | the built-in `Concise` output style (cache-safe to switch) |
| skills | `prune_unused`, `unused_days`, `mode` | off, `30`, `name-only` | `skillOverrides` for your own skills and commands with no use in the last `unused_days` (from transcripts, only once they cover that many days). `name-only` drops the description from every request but keeps the skill callable. Uninstall removes only the entries adtok added |

All tool rewrites go through one `PreToolUse` dispatcher, because when two hooks rewrite the same call the last to finish wins. Rewrites copy every original input field. Nothing ever edits a message already sent, so the prompt cache survives.

## Doctor

`adtok doctor` lists every user-controlled prefix component with its token count, including adtok's own block. It then flags:

- **prefix:** files over 200 lines, `@imports` (they save nothing), MEMORY.md over 200 lines or 25 KB, skill descriptions over 1,536 characters, tool search off with several MCP servers, `alwaysLoad` servers
- **cache:** `opusplan`, skills or commands with their own `model:`, `!`-command output in CLAUDE.md, `FORCE_PROMPT_CACHING_5M`, gateways that may strip cache headers
- **safety:** another hook rewriting tool input (for example RTK), `CLAUDE_CODE_EFFORT_LEVEL` overriding per-agent effort, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`
- **output and context:** effort `max`, an `outputStyle` with no file behind it, Haiku agents without the 100K window, no `autoCompactWindow`

`--strict` exits 1 on any warning.

## Skills

Each skill or command the model can invoke puts its name and description (up to 1,536 characters) into the listing on every request. The body loads only when the skill is invoked. `adtok skills` shows, for each skill:

- **listed:** tokens in the listing, after `disable-model-invocation` and `skillOverrides`
- **body:** body tokens
- **uses and last use:** counted from `Skill` tool calls and `/name` commands in transcripts. `--user` counts across all projects.

Skills marked unused can be pruned with `adtok init --set skills.prune_unused=true`. Skills synced from claude.ai are reported but never pruned.

`doctor` also flags:

- a `SKILL.md` over 500 lines (move reference material into linked files, which load only when read)
- a body over 5,000 tokens (only that much is re-injected after compaction)
- a listing past the 8,000-character fallback budget, where the least-used skills lose their descriptions
- a skill with its own `model:`, which rebuilds the cache

## Ledger

Reads `~/.claude/projects/<project>/*.jsonl` and subagent transcripts. It de-duplicates records per API response by message id and request id, skips synthetic and orphaned records, and prices 1-hour cache writes at 2× and Opus/Sonnet 5.5 cache reads at 0.05×.

- `misses` flags any request whose cache write exceeds half the previous context. It names the cause: model switch, compaction, Claude Code upgrade, idle gap longer than the cache lifetime, or another prefix change.
- `ttl` replays each conversation's real request gaps under both cache lifetimes and recommends `promptCacheTtl` / `subagentPromptCacheTtl`.

The transcript format is undocumented (field names observed on 2.1.29x–2.1.323).

## Proxy (optional)

```bash
adtok serve --config adtok.hybrid.toml -v    # observe only: keeps the agent's models, logs every request
adtok serve --config adtok.example.toml -v   # route: simple -> Haiku (low), mid -> Sonnet (medium), complex -> Opus
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"; claude
adtok stats --since 7d --by route
```

`classifier = false` in the proxy config, or `serve --no-classifier`, skips loading the model. The proxy then only records traffic and sends background calls to Haiku. `/__adtok/classify` returns 503.

The proxy rewrites only `model`, `effort` and an incompatible `thinking` setting.

- **Main conversation:** classified once and locked for the session; a manual `/model` switch wins.
- **Other traffic:** subagents, forks and compaction keep their model; background calls go to Haiku.
- **Errors:** rejected rewrites are retried unchanged.

**Subscriptions:** billing follows the credential. The Usage Policy from 12 November 2026 forbids services that route requests through consumer subscriptions. Use an API key for the proxy. The layers above need no proxy.

## Benchmark

```bash
adtok bench plan bench/core/suite.toml
adtok bench run bench/core/suite.toml --task bugfix-structured --arm ensemble --repeats 1
adtok bench report bench/core/suite.toml
```

Each run gets a fresh workspace, headless Claude Code, per-run and suite budgets, and hidden acceptance checks: the `bugfix-structured` repo tests, and a hidden checker for the `greenfield-genexp` CLI.

The arms are:

- `baseline`
- `concise`: a one-line "be concise" control
- `native` and `hybrid`: routing only
- `ensemble`: the default layers
- `ensemble-tools`: plus `test_output` and `read_guard`
- `routed`: through the proxy; needs `ANTHROPIC_API_KEY`

Arms can override any layer with `layers = { section = { key = value } }`. The report gives dollars per accepted task and mean ± sd. It counts a difference only beyond two standard errors; identical runs varied about 4% in cost, so use three or more repeats.

## Classifier

`./install.ps1 -Train` builds the dataset and trains `models/tier-full-v1`. The [training guide](docs/training.md) covers the data format, adding your own tasks, the training flags, evaluation and plugging a new model in. `tier-full-v1` is a [Laya](https://github.com/NandhaKishorM/laya) model that labels tasks simple, mid or complex in about 34 ms. It scored 0.865 held-out accuracy against 0.579 zero-shot. No complex task was classified simple.

## Tests

```bash
python tests/test_layers.py && python tests/test_proxy.py && python tests/test_hybrid.py && python tests/test_harness.py
```

All tests run offline, with a mock upstream, a stub classifier, fixture transcripts and a fake agent.

## Limits

- No layer has a measured cost result yet; that is what `adtok bench` is for. The tool layers stay off until it shows a saving.
- `updatedInput` is ignored for some tools and versions (Edit, dynamic MCP tools). The test wrapper relies on `set -o pipefail` in Claude Code's bash.
- The proxy routes only `/v1/messages` and keeps session state in memory.

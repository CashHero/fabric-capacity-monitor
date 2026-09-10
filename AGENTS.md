# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.
`CLAUDE.md` is a symlink to this file, so Claude Code picks it up too.

## Commands

```bash
pip install -e ".[dev]"          # or ".[dev,azure]" for DefaultAzureCredential
pytest -q                        # full suite (CI runs this on 3.13)
pytest tests/test_cu.py::test_spark_cu_for_a_real_session   # single test
ruff check .                     # lint; CI fails on any finding
```

Running against real Fabric requires `az login` first (default `--auth azure-cli`):

```bash
fabric-capacity-monitor list-capacities
fabric-capacity-monitor report --days 14 --by-item --html capacity.html
```

Exit codes are part of the contract: `0` healthy, `1` a throttling threshold was crossed,
`2` config/auth failure. Don't add new exit codes without updating README and CHANGELOG.

## Architecture

A one-way pipeline; each stage depends only on the one before it.

```
auth.TokenProvider ──> fabric_api.FabricClient ─┐
                   └─> arm.ArmClient  (optional)┴─> collect.collect() ─> Collection
                                                          │
   rates.Rates (rates.toml + user overrides) ──────────────┤
                                                          v
                                     report.analyse() ─> Analysis ─> text | json_out | html
                                          │
                                     smoothing.build_timeline() ─> list[Window]
```

- **`model.py`** holds the two normalised records every stage speaks: `Operation` (one
  capacity-consuming run) and `Window` (one 30-second slice of the reconstructed
  timeline). Anything new that consumes CU becomes an `Operation`; nothing downstream
  should need to change.
- **`collect.py`** is the only place that knows Fabric response shapes. Renderers must
  never see raw API JSON.
- **`smoothing.py`** reproduces Fabric's billing model, not a plot: background CU is
  spread over 2,880 forward windows via a difference array, and each throttle horizon is
  a *forward* mean (10 min / 60 min / 24 h). The prefix-sum/difference-array structure
  keeps this linear in windows rather than windows × operations — preserve it.
- **`rates.py` / `rates.toml`** — every Microsoft-published constant lives in TOML with a
  doc link and an `as_of` date, deep-merged over by `[rates]` in the user config. Never
  hardcode a rate in Python.
- **`report/analysis.py`** does all aggregation (per-item, per-day, per-workspace,
  week-over-week delta). The three renderers are pure formatters over `Analysis`.

`cli.py` fetches an extra week before the requested window so the per-item "vs 7 days
ago" column has a baseline; `analyse()` filters back down to the requested range.

## Invariants worth protecting

These are the project's reason for existing — tests enforce most of them.

- **Exact vs estimated is never blurred.** Only Spark is `EXACT` (published rate:
  2 vCores = 1 CU, billed for the session's whole lifetime). Everything else is
  `ESTIMATED` and every renderer must mark it (`est` / `~`). Pipeline CU is explicitly a
  *lower bound* — activity counts aren't exposed by any public API.
- **High concurrency is labelled, not guessed.** Fabric reports one session per stage,
  named after the notebook that opened it; joiners leave no record. When
  `Collection.high_concurrency_present`, the breakdown must say "by Spark session
  (attributed to session opener)", not "by item". `subruns.apply_subruns` is the only
  sanctioned way to redistribute, and it divides pro rata so the capacity total is
  preserved exactly; an unknown session or a raising resolver leaves the operation intact.
- **Unmeasurable workloads are listed, not dropped.** OneLake, SQL endpoint, semantic
  models, Eventhouse and pipeline-triggered Dataflow Gen2 go into
  `Collection.unaccounted` with the reason.
- **Read-only, viewer-level.** Every endpoint used must work for a workspace viewer with
  no Power BI licence and no capacity-admin role. Nothing writes to Fabric or Azure.
- **ARM is strictly optional.** No SKU, cost, or pause/resume history without it, but the
  report must still run; `ArmClient` returns `None`/`[]` rather than raising, and
  Cost Management 429s are swallowed.
- **The HTML dashboard is self-contained** — inline CSS and hand-built SVG, no CDN, no
  JS framework, and all interpolated text goes through `_e()`. `test_html_is_self_contained`
  and `test_html_escapes_item_names` guard this.

## Gotchas

- Fabric timestamps are inconsistent (`Z` suffix or none, 7 fractional digits). Always
  parse via `model.parse_fabric_time`, which normalises to aware UTC.
- Per-session vCore fields (`driverCores`, `executorCores`) are documented but not
  populated, so `cu.session_vcores` falls back to the workspace pool. Keep the
  session-field branch — it starts working for free if Microsoft ever fills them in.
- Spark history is ~30 days; `cli.MAX_DAYS` clamps `--days` with a warning.
- README's "Approaches that don't work" section records dead ends (Azure Monitor metrics,
  workspace monitoring KQL, Real-Time Hub capacity events, the Metrics app semantic
  model). Read it before proposing a new data source.

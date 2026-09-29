# fabric-capacity-monitor

Monitor Microsoft Fabric capacity usage from the command line — utilization, throttling
risk, overage carryforward and per-item consumption — **without a Power BI Pro licence**.

Microsoft's Fabric Capacity Metrics app is a Power BI app. On any F SKU below F64, every
viewer of Power BI content needs a Pro or PPU licence, so on an F2/F4/F8 the app that
tells you how your capacity is doing is behind a per-seat paywall. This tool reads the
same underlying reality from free REST APIs and renders it as text, JSON, or a
self-contained HTML dashboard that anyone can open.

```
$ fabric-capacity-monitor report --days 14 --by-item
$ fabric-capacity-monitor report --html capacity.html
```

## Install

```bash
pip install fabric-capacity-monitor          # core
pip install "fabric-capacity-monitor[azure]" # adds DefaultAzureCredential support
```

Requires Python 3.13+.

## Authentication

Read-only. Everything used is available to a **workspace viewer** — no capacity-admin
role, no Power BI licence.

| `--auth` | How |
|---|---|
| `azure-cli` (default) | `az login`, then the tool shells out to `az account get-access-token` |
| `default` | `DefaultAzureCredential` (managed identity, env vars, CLI, …) — needs the `[azure]` extra |
| `env` | `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` |

ARM permissions (`Reader` on the capacity's subscription) are optional but recommended:
without them you lose the SKU, region, resize and paused/resumed history and cost, and the
CU budget has to be supplied via config.

Resizes come from Azure Resource Graph change history, which keeps 14 days. Utilization
is measured against the SKU in effect at each moment. If the activity log shows an older
write to the capacity, the report notes that the SKU before it can't be confirmed.

## How CU is calculated

**Spark is exact.** Fabric bills a Spark session for its whole vCore allocation over the
session's lifetime, and publishes the rate: *two Spark vCores = one capacity unit*.

```
vcores     = driver_cores + max_executors * executor_cores
cu_seconds = (vcores / 2) * running_duration_seconds
```

Session durations come from `GET /v1/workspaces/{id}/spark/livySessions`; the vCore count
comes from `GET /v1/workspaces/{id}/spark/pools`, because the per-session core fields the
API documents are not currently populated.

**Everything else is estimated** from published rates and marked `est` / `~` in the output.
Pipeline orchestration is charged per activity run and no public API exposes activity
counts, so pipeline CU is reported as a lower bound. Dataflow Gen2 refreshes — including
those a pipeline triggers — are priced from each run's wall-clock duration at the CI/CD
query-evaluation rate, as a single query on one tier schedule. Fabric bills every mashup
query on its own schedule (each gets its own expensive first ten minutes) and adds High
Scale staging and Fast Copy compute; none of that is exposed, so a multi-query refresh
comes out low. The figure only runs high when a run idles around a short evaluation.
Rates live in
[`rates.toml`](src/fabric_capacity_monitor/rates.toml), each with a documentation link and
an "as of" date, and any of them can be overridden in your config file.

**Smoothing and throttling** follow Fabric's own model. Background operations are spread
evenly over the 2,880 thirty-second windows after they finish, and throttling is decided
by the mean utilization of a *forward* window:

| Throttle | Forward window | Effect |
|---|---|---|
| Interactive delay | 10 minutes | user-triggered operations are delayed |
| Interactive rejection | 60 minutes | user-triggered operations are rejected |
| Background rejection | 24 hours | scheduled jobs are rejected |

**Outlook.** Smoothing means most of the next day's utilization is already set by runs
that have finished. The report projects the next 24 hours on the current SKU as if nothing
new runs, and shows three things:

- **When utilization drops below the target**, 50% by default (`--target PCT`, or
  `target_utilization` in the config). If you need the capacity to calm down, this tells
  you how long to hold off new runs.
- **Headroom now**: how many CU-seconds of background work could be charged right now
  without starting any throttle. A job is charged when it *ends*, by which point the
  committed load has fallen further, so this figure is conservative.
- **What's holding it up**: runs from the past 24 hours whose background CU is still
  spreading into now, per item, in percentage points of utilization. These set the recovery
  time and headroom above.

The projection only counts what the report can see. Spark sessions still running, and
workloads listed as *not counted*, add more load. So the true recovery time can be later
and the true headroom smaller.

## High-concurrency sessions: read the per-item table carefully

When a workspace has high concurrency enabled for pipeline runs, Fabric opens **one Spark
session per stage** and names it after the notebook that *opened* it. Notebooks that later
join that session produce no session record at all.

- **Capacity totals are unaffected** — the session is the billed unit and is counted once.
- **Per-notebook attribution is not available.** A row showing 470k CU-s against one
  notebook may really be a dozen notebooks sharing its session.

The tool detects this and labels the breakdown *"by Spark session (attributed to session
opener)"* rather than pretending otherwise.

Two ways to get true per-notebook figures:

1. Turn off `notebookPipelineRunEnabled` in the workspace's Spark settings, at the cost of
   a session start per notebook.
2. Supply a **sub-run resolver**. Fabric exposes the Livy session id to running notebook
   code, so if your notebooks already log their own runtime next to it, you have
   everything needed. Implement one method and
   `fabric_capacity_monitor.subruns.apply_subruns` will redistribute each session's CU in
   proportion to the durations you return, preserving the capacity total exactly:

   ```python
   class MyResolver:
       def resolve(self, session_id: str) -> list[SubRun]:
           # -> [SubRun(name="20_silver_transform_accounts", duration_seconds=88.0), ...]
           ...
   ```

   Sessions the resolver doesn't recognise are left attributed to their opener rather
   than guessed at, and a resolver that raises never loses a session's CU.

## What is not covered

- OneLake transactions, SQL analytics endpoint queries, semantic model refreshes and
  Eventhouse uptime. No free API exposes their CU; they are listed explicitly as
  *not counted* rather than silently dropped.
- Eventstreams. A running one bills a flat 0.222 CU while events flow — about 6% of an
  F4 — plus data traffic and the Eventhouse it feeds. The report can't measure that, but it
  names every running Eventstream on the capacity under *not counted*.
- The Metrics app's Storage page.

History is limited to what the Spark API retains — about 30 days. Use `--json` if you want
to accumulate more.

## Approaches that don't work

Recorded so nobody re-derives them:

- **Azure Monitor platform metrics.** There are none for `Microsoft.Fabric/capacities`;
  that namespace only exists for the legacy `Microsoft.PowerBIDedicated/capacities`.
- **Workspace monitoring (the monitoring Eventhouse).** Its KQL database has no Spark,
  notebook, pipeline or capacity tables — workspace monitoring has never covered Spark.
- **Real-Time Hub capacity events** (`Microsoft.Fabric.Capacity.Summary` / `.Operation`).
  These *are* the exact whole-capacity CU feed, and on a large SKU they're the right
  answer. But consuming them needs an Eventstream at 0.222 CU/h flat plus 0.778–2.333 CU/h
  for the processor, and Summary events fire every 30 seconds so it never goes idle — on
  an F4 that is 5–60% of the entire capacity, permanently. Worth it above F64; not below.
- **Querying the Capacity Metrics app's semantic model.** Needs the Pro licence this tool
  exists to avoid.

## Configuration

Optional, at `~/.config/fabric-capacity-monitor/config.toml` or via `--config`:

```toml
default_capacity = "my-capacity"
target_utilization = 50   # the outlook counts down to this percentage (default 50)

[aliases]
prod = "my-prod-capacity"

[rates.spark]
vcores_per_cu = 2.0   # override if Microsoft changes a rate before this package does
```

## Exit codes

`0` healthy · `1` a throttling threshold was crossed · `2` configuration, auth or
Fabric API failure (including the API staying unreachable after retries).
Suitable for CI.

If the capacity was resized during the range, "crossed" means the range's load would cross
a threshold on the *current* SKU. The throttling counts in the report still show what
happened against the SKU in effect at the time.

## Licence

MIT.

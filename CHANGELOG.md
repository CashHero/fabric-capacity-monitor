# Changelog

All notable changes to `fabric-capacity-monitor` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Dataflow Gen2 refreshes are now priced (estimated) from their job-instance run history,
  including refreshes triggered from a pipeline. Each run is priced as one CI/CD query, so
  multi-query refreshes come out low.
- Running Eventstreams are listed under *not counted* with their flat hourly charge, so an
  always-on stream is visible even though its CU can't be measured.

### Fixed

- A capacity resized during the reporting range was measured against its current SKU for
  the whole range. Resizes are now read from Azure Resource Graph, so each window and each
  day is measured against the SKU in effect at the time. The text summary names the
  resize, and the dashboard's SKU card shows it (e.g. `F4 → F8`).
- The utilization timeline now includes background CU carried in from runs that ended
  up to 24 hours before the reporting range. Previously the first day of every report
  started from zero, understating early utilization, peaks and the average.
- The README claimed pipeline-triggered Dataflow Gen2 refreshes register no job
  instances. They do, and are now counted.

## [0.1.0] - 2026-09-10

First release.

- Exact Spark CU from `spark/livySessions` + `spark/pools` (2 vCores = 1 CU).
- Fabric's smoothing model reproduced at 30-second resolution: 24-hour background
  spread, the three forward-window throttling horizons, and overage carryforward.
- Capacity-wide reporting across every workspace assigned to a capacity.
- High-concurrency sessions detected and labelled honestly, with an optional sub-run
  resolver to redistribute a shared session's CU.
- Text, JSON and self-contained HTML output.
- Pipeline CU as a documented lower bound; unmeasurable workloads listed explicitly
  rather than silently dropped.

[Unreleased]: https://github.com/CashHero/fabric-capacity-monitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/CashHero/fabric-capacity-monitor/releases/tag/v0.1.0

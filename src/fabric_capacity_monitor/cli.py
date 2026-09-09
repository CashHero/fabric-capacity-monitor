"""Command-line entry point.

Usage:
    fabric-capacity-monitor list-capacities
    fabric-capacity-monitor report --capacity <name-or-id> --days 14 --by-item
    fabric-capacity-monitor report --html capacity.html --json capacity.json

Reads only. Nothing here writes to Fabric or to Azure.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from . import config as config_module
from .arm import ArmClient
from .auth import AuthError, TokenProvider
from .collect import collect
from .fabric_api import FabricClient, FabricError
from .model import Capacity
from .rates import Rates
from .report import analyse, json_out, text
from .report import html as html_report

MAX_DAYS = 30  # the Livy session API retains roughly this much history


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fabric-capacity-monitor",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--auth",
        choices=("azure-cli", "default", "env"),
        default="azure-cli",
        help="how to obtain Azure tokens (default: azure-cli)",
    )
    parser.add_argument("--config", type=Path, help="path to a TOML config file")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list-capacities", help="list the Fabric capacities you can see")

    report = subparsers.add_parser("report", help="report on one capacity")
    report.add_argument("--capacity", help="capacity name or id (optional if you have exactly one)")
    report.add_argument("--days", type=int, default=14, help="days of history (default 14, max 30)")
    report.add_argument("--by-item", action="store_true", help="add the per-item breakdown")
    report.add_argument("--by-workspace", action="store_true", help="add the per-workspace breakdown")
    report.add_argument("--top", type=int, default=15, help="rows in each breakdown (default 15)")
    report.add_argument("--html", type=Path, help="also write a self-contained HTML dashboard")
    report.add_argument("--json", type=Path, help="also write the normalised records as JSON")
    report.add_argument(
        "--json-windows",
        action="store_true",
        help="include every 30-second window in the JSON (large: ~20k rows per week)",
    )
    report.add_argument("--no-pipelines", action="store_true", help="skip pipeline job history")
    report.add_argument("--include-cost", action="store_true", help="query Azure Cost Management")
    report.add_argument("--quiet", action="store_true", help="suppress the text report on stdout")
    return parser


def _capacities(fabric: FabricClient, arm: ArmClient | None) -> list[Capacity]:
    """Every visible capacity, with its workspaces attached.

    Fabric is the source of truth here: only it knows the capacity GUID that workspaces
    reference. ARM is consulted opportunistically to recover the resource id needed for
    cost and pause/resume history, matched on name — but the tool works fully without it.
    """
    by_capacity: dict[str, list[dict]] = {}
    for workspace in fabric.workspaces():
        capacity_id = (workspace.get("capacityId") or "").lower()
        if capacity_id:
            by_capacity.setdefault(capacity_id, []).append(workspace)

    resource_ids: dict[str, str] = {}
    if arm is not None:
        try:
            for resource in arm.capacities():
                name = (resource.get("name") or "").lower()
                if name:
                    resource_ids[name] = resource.get("id", "")
        except Exception:  # noqa: BLE001 - ARM access is strictly optional
            resource_ids = {}

    found: list[Capacity] = []
    for entry in fabric.capacities():
        capacity_id = (entry.get("id") or "").lower()
        name = entry.get("displayName", "")
        found.append(
            Capacity(
                id=capacity_id,
                name=name,
                sku=entry.get("sku"),
                region=entry.get("region"),
                state=entry.get("state"),
                resource_id=resource_ids.get(name.lower()),
                workspaces=by_capacity.get(capacity_id, []),
            )
        )

    # A capacity that owns workspaces but did not appear in /capacities still deserves
    # a row; without it the user would silently lose a whole capacity's worth of data.
    known = {c.id for c in found}
    for capacity_id, members in by_capacity.items():
        if capacity_id not in known:
            found.append(
                Capacity(id=capacity_id, name=f"(capacity {capacity_id[:8]})", workspaces=members)
            )
    return found


def _match(capacities: list[Capacity], wanted: str | None) -> Capacity | None:
    if wanted is None:
        candidates = [c for c in capacities if c.workspaces]
        return candidates[0] if len(candidates) == 1 else None
    needle = wanted.lower()
    for capacity in capacities:
        if capacity.name.lower() == needle or capacity.id.lower() == needle:
            return capacity
    for capacity in capacities:
        if needle in capacity.name.lower():
            return capacity
    return None


def cmd_list(fabric: FabricClient, arm: ArmClient | None) -> int:
    capacities = _capacities(fabric, arm)
    if not capacities:
        print("No Fabric capacities visible to this identity.")
        return 1
    print(f"{'NAME':<32}{'SKU':<7}{'REGION':<14}{'STATE':<10}{'WORKSPACES':>11}")
    print("-" * 74)
    for capacity in sorted(capacities, key=lambda c: c.name):
        print(
            f"{capacity.name[:30]:<32}{capacity.sku or '?':<7}{capacity.region or '?':<14}"
            f"{capacity.state or '?':<10}{len(capacity.workspaces):>11}"
        )
    return 0


def cmd_report(args: argparse.Namespace, fabric: FabricClient, arm: ArmClient, rates: Rates,
               user_config: dict) -> int:
    days = args.days
    if days > MAX_DAYS:
        print(
            f"warning: --days {days} exceeds the ~{MAX_DAYS}-day Spark API retention; "
            f"using {MAX_DAYS}",
            file=sys.stderr,
        )
        days = MAX_DAYS

    capacities = _capacities(fabric, arm)
    wanted = config_module.resolve_capacity_name(user_config, args.capacity)
    capacity = _match(capacities, wanted)
    if capacity is None:
        print("Could not pick a capacity. Available:\n", file=sys.stderr)
        cmd_list(fabric, arm)
        return 2
    if not capacity.workspaces:
        print(
            f"No workspaces are assigned to '{capacity.name}', or none are visible to you.",
            file=sys.stderr,
        )
        return 2

    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    # Reach back an extra week so the per-item "change vs 7 days ago" column has a
    # baseline to compare against. analyse() still reports only the requested window.
    baseline_start = start - timedelta(days=7)

    collection = collect(
        fabric, capacity, rates, baseline_start, include_pipelines=not args.no_pipelines
    )

    cost_rows = None
    if args.include_cost and capacity.resource_id:
        subscription = capacity.resource_id.split("/")[2]
        cost_rows = arm.cost_by_meter(
            subscription, start.strftime("%Y-%m-%dT00:00:00Z"), end.strftime("%Y-%m-%dT00:00:00Z")
        )
        if cost_rows is None:
            collection.warnings.append(
                "Azure Cost Management did not return data (it throttles aggressively); "
                "cost is omitted."
            )

    events = []
    if capacity.resource_id:
        events = arm.activity_log(capacity.resource_id, start.strftime("%Y-%m-%dT%H:%M:%SZ"))

    analysis = analyse(
        collection, rates, start, end, cost_rows=cost_rows, activity_events=events
    )

    if not args.quiet:
        print(text.render(
            analysis, by_item=args.by_item, by_workspace=args.by_workspace, top=args.top
        ))

    if args.html:
        args.html.write_text(html_report.render(analysis, top=max(args.top, 25)), encoding="utf-8")
        print(f"\nHTML dashboard written to {args.html}", file=sys.stderr)
    if args.json:
        args.json.write_text(
            json_out.render(analysis, include_windows=args.json_windows), encoding="utf-8"
        )
        print(f"JSON written to {args.json}", file=sys.stderr)

    risky = any(
        analysis.breaches[key]
        for key in ("interactive_delay", "interactive_rejection", "background_rejection")
    )
    return 1 if risky else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        user_config = config_module.load(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rates = Rates.load(user_config.get("rates"))
    try:
        tokens = TokenProvider(args.auth)
        fabric = FabricClient(tokens)
        arm = ArmClient(tokens)
        if args.command == "list-capacities":
            return cmd_list(fabric, arm)
        return cmd_report(args, fabric, arm, rates, user_config)
    except AuthError as exc:
        print(f"authentication failed: {exc}", file=sys.stderr)
        return 2
    except FabricError as exc:
        print(f"Fabric API error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

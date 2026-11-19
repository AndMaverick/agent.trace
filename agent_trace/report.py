"""A summary of one trace as plain data and as text."""
from __future__ import annotations

from typing import Any

from . import analysis
from .model import Span


def summary(spans: list[Span], top: int = 10) -> dict[str, Any]:
    stats = analysis.stats_by_name(spans)
    by_self = sorted(stats.values(), key=lambda s: -s.self_total)[:top]
    return {
        "spans": len(spans),
        "errors": sum(1 for s in spans if s.error),
        "open": sum(1 for s in spans if s.end is None),
        "wall_seconds": analysis.wall_time(spans),
        "total_cost": analysis.total_cost(spans),
        "by_name": [
            {"name": s.name, "count": s.count, "total": s.total, "self": s.self_total, "p50": s.p50, "p95": s.p95, "max": s.max,
             "cost": s.cost, "errors": s.errors}
            for s in by_self
        ],
        "blame": analysis.blame(spans),
        "blocking_path": [s.name for s in analysis.blocking_path(spans)],
        "critical_path": [s.name for s in analysis.critical_path(spans)],
    }


def render_summary(spans: list[Span], top: int = 10) -> str:
    data = summary(spans, top)
    lines = [
        f"spans {data['spans']}   errors {data['errors']}   open {data['open']}   wall {data['wall_seconds'] * 1000:.1f}ms   cost ${data['total_cost']:.4f}",
        "",
        f"{'name':<30} {'n':>5} {'self ms':>10} {'p50 ms':>9} {'p95 ms':>9} {'cost':>10}",
    ]
    for r in data["by_name"]:
        lines.append(f"{r['name'][:30]:<30} {r['count']:>5} {r['self'] * 1000:>10.1f} {r['p50'] * 1000:>9.1f} {r['p95'] * 1000:>9.1f} {r['cost']:>10.4f}")
    lines += ["", "where the wall time went (blocking path):"]
    wall = data["wall_seconds"] or 1.0
    for name, seconds in list(data["blame"].items())[:top]:
        lines.append(f"  {name[:30]:<30} {seconds * 1000:>9.1f}ms  {seconds / wall * 100:>5.1f}%")
    return "\n".join(lines)

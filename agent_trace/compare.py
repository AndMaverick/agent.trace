"""Compare two traces by span name: where did time and cost move between a baseline and a candidate."""
from __future__ import annotations

from dataclasses import dataclass

from . import analysis
from .model import Span


@dataclass(frozen=True)
class Delta:
    name: str
    status: str  # "changed", "new" or "gone"
    count_a: int
    count_b: int
    self_a: float
    self_b: float
    cost_a: float
    cost_b: float

    @property
    def self_change(self) -> float:
        return self.self_b - self.self_a

    @property
    def self_pct(self) -> float | None:
        return None if self.self_a == 0 else self.self_change / self.self_a * 100

    @property
    def cost_change(self) -> float:
        return self.cost_b - self.cost_a


def diff(baseline: list[Span], candidate: list[Span]) -> list[Delta]:
    """Per-name changes in self time and cost, largest absolute self-time change first."""
    a, b = analysis.stats_by_name(baseline), analysis.stats_by_name(candidate)
    rows = []
    for name in sorted(set(a) | set(b)):
        sa, sb = a.get(name), b.get(name)
        status = "new" if sa is None else "gone" if sb is None else "changed"
        rows.append(Delta(name, status, sa.count if sa else 0, sb.count if sb else 0, sa.self_total if sa else 0.0,
                          sb.self_total if sb else 0.0, sa.cost if sa else 0.0, sb.cost if sb else 0.0))
    return sorted(rows, key=lambda d: -abs(d.self_change))


def regressions(baseline: list[Span], candidate: list[Span], threshold_pct: float = 20.0, min_seconds: float = 0.0) -> list[Delta]:
    """Names whose self time grew by more than ``threshold_pct`` and by at least ``min_seconds``, plus any new span."""
    out = []
    for d in diff(baseline, candidate):
        if d.status == "new" and d.self_b >= min_seconds or d.status == "changed" and d.self_change >= min_seconds and d.self_pct is not None and d.self_pct > threshold_pct:
            out.append(d)
    return out

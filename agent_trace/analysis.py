"""Read-only analysis over a list of spans: rollups, self time, critical and blocking paths, statistics, rendering."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .model import Span

EPS = 1e-9


def index(spans: list[Span]) -> dict[int | None, list[Span]]:
    out: dict[int | None, list[Span]] = defaultdict(list)
    ids = {s.id for s in spans}
    for s in spans:
        out[s.parent if s.parent in ids else None].append(s)  # a span whose parent was not recorded becomes a root
    return out


def subtree(spans: list[Span], span_id: int | None) -> list[Span]:
    kids = index(spans)
    start = kids[None] if span_id is None else [s for s in spans if s.id == span_id]
    if span_id is not None and not start:
        raise KeyError(span_id)
    out: list[Span] = []
    stack = list(reversed(start))
    while stack:
        s = stack.pop()
        out.append(s)
        stack.extend(reversed(kids.get(s.id, [])))
    return out


def total_cost(spans: list[Span], span_id: int | None = None) -> float:
    """Exact sum of cost over a subtree (or the whole trace), without float drift from long chains of additions."""
    return math.fsum(s.cost for s in (spans if span_id is None else subtree(spans, span_id)))


def _union_length(intervals: list[tuple[float, float]]) -> float:
    total, cursor = 0.0, -math.inf
    for lo, hi in sorted(intervals):
        lo = max(lo, cursor)
        if hi > lo:
            total += hi - lo
            cursor = hi
    return total


def self_time(span: Span, children: list[Span]) -> float:
    """Time in the span itself: its duration minus the time covered by children, counting parallel children once."""
    end = span.end if span.end is not None else span.start
    covered = _union_length([(max(c.start, span.start), min(c.end if c.end is not None else c.start, end)) for c in children])
    return max(0.0, span.duration - covered)


def critical_path(spans: list[Span]) -> list[Span]:
    """The dominant chain: the longest root, then the longest child at each level."""
    kids = index(spans)
    roots = kids[None]
    if not roots:
        return []
    node = max(roots, key=lambda s: s.duration)
    path = [node]
    while kids.get(node.id):
        node = max(kids[node.id], key=lambda s: s.duration)
        path.append(node)
    return path


def _chain(candidates: list[Span], deadline: float) -> list[Span]:
    """Walk backwards from the deadline, each time taking the child that finished last, then continue before it starts."""
    left = sorted(candidates, key=lambda s: (s.end if s.end is not None else s.start, s.duration))
    chain: list[Span] = []
    cursor = deadline
    while left:
        fits = [s for s in left if (s.end if s.end is not None else s.start) <= cursor + EPS]
        if not fits:
            break
        pick = max(fits, key=lambda s: ((s.end if s.end is not None else s.start), s.duration))
        chain.append(pick)
        left.remove(pick)
        cursor = pick.start
        left = [s for s in left if (s.end if s.end is not None else s.start) <= cursor + EPS]
    chain.reverse()
    return chain


def _latest_end(spans: list[Span]) -> float:
    return max((s.end if s.end is not None else s.start) for s in spans)


def blocking_path(spans: list[Span]) -> list[Span]:
    """The spans that set end-to-end latency, in time order.

    Sequential work is all on the path. Where branches ran in parallel, only the one that finished last is, since
    speeding up a branch that was already waiting for another changes nothing. This differs from ``critical_path``,
    which only follows the longest span down the tree.
    """
    kids = index(spans)
    out: list[Span] = []

    def walk(candidates: list[Span], deadline: float) -> None:
        for s in _chain(candidates, deadline):
            out.append(s)
            if kids.get(s.id):
                walk(kids[s.id], s.end if s.end is not None else s.start)

    if kids[None]:
        walk(kids[None], _latest_end(kids[None]))
    return out


def blame(spans: list[Span]) -> dict[str, float]:
    """Seconds of end-to-end latency attributed to each span name, so for a single root they sum to its duration."""
    kids = index(spans)
    acc: dict[str, float] = defaultdict(float)

    # Each span is charged its own duration minus the chained children it was waiting on.
    def attribute(s: Span) -> None:
        end = s.end if s.end is not None else s.start
        chain = _chain(kids.get(s.id, []), end)
        acc[s.name] += max(0.0, s.duration - sum(c.duration for c in chain))
        for c in chain:
            attribute(c)

    roots = kids[None]
    if roots:
        top = _chain(roots, _latest_end(roots))
        for s in top:
            attribute(s)
    return dict(sorted(acc.items(), key=lambda kv: -kv[1]))


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile, q in [0, 100]. Empty input gives 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class NameStats:
    name: str
    count: int = 0
    total: float = 0.0
    self_total: float = 0.0
    cost: float = 0.0
    errors: int = 0
    durations: list[float] = field(default_factory=list, repr=False)

    @property
    def p50(self) -> float:
        return percentile(self.durations, 50)

    @property
    def p95(self) -> float:
        return percentile(self.durations, 95)

    @property
    def max(self) -> float:
        return max(self.durations, default=0.0)


def stats_by_name(spans: list[Span]) -> dict[str, NameStats]:
    kids = index(spans)
    out: dict[str, NameStats] = {}
    for s in spans:
        st = out.setdefault(s.name, NameStats(s.name))
        st.count += 1
        st.total += s.duration
        st.self_total += self_time(s, kids.get(s.id, []))
        st.cost += s.cost
        st.errors += 1 if s.error else 0
        st.durations.append(s.duration)
    return out


def wall_time(spans: list[Span]) -> float:
    roots = index(spans)[None]
    if not roots:
        return 0.0
    return _latest_end(roots) - min(s.start for s in roots)


# ----------------------------------------------------------------------------------------------- rendering
def _walk(spans: list[Span]) -> list[tuple[int, Span]]:
    kids = index(spans)
    out: list[tuple[int, Span]] = []
    stack = [(0, s) for s in reversed(kids[None])]
    while stack:
        depth, s = stack.pop()
        out.append((depth, s))
        stack.extend((depth + 1, c) for c in reversed(kids.get(s.id, [])))
    return out


def render_tree(spans: list[Span]) -> str:
    lines = []
    for depth, span in _walk(spans):
        flag = f"  ERROR {span.error}" if span.error else ""
        cost = f"  ${span.cost:.4f}" if span.cost else ""
        lines.append(f"{'  ' * depth}{span.name}  {span.duration * 1000:.1f}ms{cost}{flag}")
    return "\n".join(lines)


def render_waterfall(spans: list[Span], width: int = 60) -> str:
    """A text waterfall: one row per span, a bar placed and sized by time. ASCII only, so it survives any terminal."""
    rows = _walk(spans)
    if not rows:
        return ""
    t0 = min(s.start for _d, s in rows)
    total = max(_latest_end([s for _d, s in rows]) - t0, EPS)
    label_w = min(32, max(len("  " * d + s.name) for d, s in rows))
    lines = []
    for depth, s in rows:
        label = ("  " * depth + s.name)[:label_w].ljust(label_w)
        lo = int((s.start - t0) / total * width)
        n = max(1, round(s.duration / total * width))
        lo = min(lo, width - 1)
        n = min(n, width - lo)
        bar = " " * lo + ("x" if s.error else "=") * n
        cost = f" ${s.cost:.4f}" if s.cost else ""
        lines.append(f"{label} |{bar.ljust(width)}| {s.duration * 1000:.1f}ms{cost}")
    return "\n".join(lines)


def folded_stacks(spans: list[Span]) -> list[str]:
    """Self time per stack in the folded format flame graph tools read: ``a;b;c <microseconds>``."""
    kids = index(spans)
    by_id = {s.id: s for s in spans}
    totals: dict[str, int] = defaultdict(int)
    for s in spans:
        names = []
        node: Span | None = s
        while node is not None:
            names.append(node.name.replace(";", ":"))
            node = by_id.get(node.parent) if node.parent is not None else None
        micros = round(self_time(s, kids.get(s.id, [])) * 1_000_000)
        if micros > 0:
            totals[";".join(reversed(names))] += micros
    return [f"{stack} {micros}" for stack, micros in sorted(totals.items())]

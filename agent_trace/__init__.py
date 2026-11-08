"""agent.trace: span trees for agent runs, with cost and latency rollups and a critical path."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

__version__ = "0.1.0"


@dataclass
class Span:
    id: int
    parent: int | None
    name: str
    start: float
    end: float | None = None
    cost: float = 0.0
    error: str | None = None
    attrs: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return (self.end if self.end is not None else self.start) - self.start


class Tracer:
    def __init__(self, clock=time.perf_counter) -> None:
        self.clock = clock
        self.spans: list = []
        self._stack: list = []

    @contextmanager
    def span(self, name: str, cost: float = 0.0, **attrs):
        parent = self._stack[-1].id if self._stack else None
        span = Span(len(self.spans) + 1, parent, name, self.clock(), cost=cost, attrs=attrs)
        self.spans.append(span)
        self._stack.append(span)
        try:
            yield span
        except Exception as exc:
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span.end = self.clock()
            self._stack.pop()

    def children(self, span_id: int | None) -> list:
        return [s for s in self.spans if s.parent == span_id]

    def total_cost(self, span_id: int | None = None) -> float:
        """Cost of a span's whole subtree, or of the entire trace when span_id is None."""
        if span_id is None:
            return sum(s.cost for s in self.spans)
        span = next(s for s in self.spans if s.id == span_id)
        return span.cost + sum(self.total_cost(c.id) for c in self.children(span_id))

    def critical_path(self) -> list:
        """Follow the longest-running child at each level, from the longest root."""
        roots = self.children(None)
        if not roots:
            return []
        node = max(roots, key=lambda s: s.duration)
        path = [node]
        while True:
            kids = self.children(node.id)
            if not kids:
                return path
            node = max(kids, key=lambda s: s.duration)
            path.append(node)

    def errors(self) -> list:
        return [s for s in self.spans if s.error]

    def render(self) -> str:
        lines: list = []

        def walk(span_id, depth):
            for span in self.children(span_id):
                flag = f"  ERROR {span.error}" if span.error else ""
                cost = f"  ${span.cost:.4f}" if span.cost else ""
                lines.append(f"{'  ' * depth}{span.name}  {span.duration * 1000:.1f}ms{cost}{flag}")
                walk(span.id, depth + 1)

        walk(None, 0)
        return "\n".join(lines)

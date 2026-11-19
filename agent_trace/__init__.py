"""agent.trace: tracing for agent runs with no vendor and no collector.

Nested spans that follow threads and asyncio tasks, cost and token usage on the span that incurred it, a
blocking-path analysis that accounts for parallel branches, OpenTelemetry and Perfetto export, and trace files whose
hash chain shows any later edit.
"""
from __future__ import annotations

from .analysis import (
    blame,
    blocking_path,
    critical_path,
    folded_stacks,
    percentile,
    self_time,
    stats_by_name,
)
from .compare import Delta, diff, regressions
from .export import (
    TraceIntegrityError,
    from_jsonl,
    head,
    seal,
    to_chrome,
    to_jsonl,
    to_otlp,
)
from .model import Event, Span
from .report import render_summary, summary
from .tracer import Tracer

__version__ = "0.2.0"

__all__ = [
    "Delta",
    "Event",
    "Span",
    "TraceIntegrityError",
    "Tracer",
    "__version__",
    "blame",
    "blocking_path",
    "critical_path",
    "diff",
    "folded_stacks",
    "from_jsonl",
    "head",
    "percentile",
    "regressions",
    "render_summary",
    "seal",
    "self_time",
    "stats_by_name",
    "summary",
    "to_chrome",
    "to_jsonl",
    "to_otlp",
]

"""The tracer: nested spans that follow the call stack, threads and asyncio tasks, with no collector."""
from __future__ import annotations

import functools
import inspect
import threading
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, TypeVar, overload

from . import analysis
from .model import Span

F = TypeVar("F", bound=Callable[..., Any])

# One module-level variable (the documented way to use contextvars). A span records which tracer it belongs to, so two
# tracers never adopt each other's spans as parents.
_CURRENT: ContextVar[Span | None] = ContextVar("agent_trace_current", default=None)


class _SpanContext:
    def __init__(self, tracer: Tracer, name: str, kind: str, cost: float, parent: Span | int | None, attrs: dict[str, Any]) -> None:
        self._tracer, self._args = tracer, (name, kind, cost, parent, attrs)
        self._span: Span | None = None
        self._token: Token[Span | None] | None = None

    def __enter__(self) -> Span:
        name, kind, cost, parent, attrs = self._args
        self._span, recorded = self._tracer._open(name, kind, cost, parent, attrs)
        if recorded:
            self._token = _CURRENT.set(self._span)
        return self._span

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        span = self._span
        assert span is not None
        if exc_type is not None:
            span.error = f"{exc_type.__name__}: {exc}"
        span.end = self._tracer.clock()
        if self._token is not None:
            _CURRENT.reset(self._token)

    async def __aenter__(self) -> Span:
        return self.__enter__()

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.__exit__(exc_type, exc, tb)


class Tracer:
    """Collects spans for one trace.

    * ``clock`` times durations (monotonic by default). ``wall`` gives epoch seconds for export. Pass your own
      for deterministic tests: when only ``clock`` is injected it also serves as the wall clock.
    * ``scrub(key, value)`` runs on every attribute and event attribute as it is set. Use it to drop prompts or
      mask values before they are stored, so nothing sensitive reaches an export.
    * ``max_spans`` bounds memory. Spans past the cap are timed but not stored, ``dropped`` counts them, and their
      children attach to the nearest stored ancestor.
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter, wall: Callable[[], float] | None = None, *,
                 trace_id: str | None = None, service: str = "agent", max_spans: int = 100_000,
                 scrub: Callable[[str, Any], Any] | None = None) -> None:
        if max_spans < 1:
            raise ValueError("max_spans must be at least 1")
        self.clock = clock
        self.wall = wall or (time.time if clock is time.perf_counter else clock)
        self.trace_id = trace_id or uuid.uuid4().hex
        self.service, self.max_spans, self.scrub = service, max_spans, scrub
        self.spans: list[Span] = []
        self.dropped = 0
        self._children: dict[int | None, list[Span]] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- recording
    def _open(self, name: str, kind: str, cost: float, parent: Span | int | None, attrs: dict[str, Any]) -> tuple[Span, bool]:
        if cost < 0:
            raise ValueError("cost cannot be negative")
        if isinstance(parent, Span):
            parent_id: int | None = parent.id or parent.parent
        elif parent is not None:
            parent_id = parent
        else:
            current = _CURRENT.get()
            parent_id = current.id if current is not None and current.tracer is self else None
        with self._lock:
            if len(self.spans) >= self.max_spans:
                self.dropped += 1
                return Span(0, parent_id, name, self.clock(), cost=cost, kind=kind, wall=self.wall(), tracer=self), False
            span = Span(len(self.spans) + 1, parent_id, name, self.clock(), cost=cost, kind=kind, wall=self.wall(), tracer=self)
            self.spans.append(span)
            self._children.setdefault(parent_id, []).append(span)
        span.set(**attrs)
        return span, True

    def span(self, name: str, cost: float = 0.0, *, kind: str = "span", parent: Span | int | None = None, **attrs: Any) -> _SpanContext:
        """A context manager (sync or ``async with``) that records one span. Errors are stored and re-raised."""
        return _SpanContext(self, name, kind, cost, parent, attrs)

    def record(self, name: str, start: float, end: float, cost: float = 0.0, *, kind: str = "span", error: str | None = None,
               parent: Span | int | None = None, **attrs: Any) -> Span:
        """Add a span that already happened, for work timed elsewhere. Times are on this tracer's clock."""
        if end < start:
            raise ValueError("end is before start")
        span, _ = self._open(name, kind, cost, parent, attrs)
        span.start, span.end, span.error = start, end, error
        return span

    @property
    def current(self) -> Span | None:
        span = _CURRENT.get()
        return span if span is not None and span.tracer is self else None

    @overload
    def trace(self, func: F) -> F: ...
    @overload
    def trace(self, func: None = None, *, name: str | None = None, kind: str = "span") -> Callable[[F], F]: ...
    def trace(self, func: Any = None, *, name: str | None = None, kind: str = "span") -> Any:
        """Decorate a function or coroutine function so every call is a span named after it."""
        def wrap(fn: Any) -> Any:
            label = name or fn.__qualname__
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def awrapper(*args: Any, **kwargs: Any) -> Any:
                    async with self.span(label, kind=kind):
                        return await fn(*args, **kwargs)
                return awrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.span(label, kind=kind):
                    return fn(*args, **kwargs)
            return wrapper
        return wrap(func) if func is not None else wrap

    # ----------------------------------------------------------------- reading (delegates to analysis)
    def children(self, span_id: int | None) -> list[Span]:
        with self._lock:
            return list(self._children.get(span_id, []))

    def total_cost(self, span_id: int | None = None) -> float:
        """Cost of a span's whole subtree, or of the entire trace when ``span_id`` is None."""
        return analysis.total_cost(self.spans, span_id)

    def critical_path(self) -> list[Span]:
        """Follow the longest-running child at each level, from the longest root."""
        return analysis.critical_path(self.spans)

    def blocking_path(self) -> list[Span]:
        """The spans that set end-to-end latency, in time order, accounting for parallel branches."""
        return analysis.blocking_path(self.spans)

    def errors(self) -> list[Span]:
        return [s for s in self.spans if s.error]

    def render(self) -> str:
        return analysis.render_tree(self.spans)

    def waterfall(self, width: int = 60) -> str:
        return analysis.render_waterfall(self.spans, width)


"""The span record: what happened, when, at what cost, and with what attached."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tracer import Tracer

# OpenTelemetry GenAI semantic-convention attribute names, so a span exported to a collector reads as an LLM call.
ATTR_MODEL = "gen_ai.request.model"
ATTR_INPUT = "gen_ai.usage.input_tokens"
ATTR_OUTPUT = "gen_ai.usage.output_tokens"
ATTR_CACHED = "gen_ai.usage.cached_input_tokens"

# (input, output, cached input), in currency per million tokens
Price = tuple[float, float, float]


@dataclass
class Event:
    name: str
    at: float  # on the tracer clock
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Span:
    id: int
    parent: int | None
    name: str
    start: float
    end: float | None = None
    cost: float = 0.0
    error: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    kind: str = "span"
    wall: float = 0.0  # epoch seconds at start, for export
    events: list[Event] = field(default_factory=list)
    tracer: Tracer | None = field(default=None, repr=False, compare=False)

    @property
    def duration(self) -> float:
        return (self.end if self.end is not None else self.start) - self.start

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        return "ok" if self.end is not None else "open"

    def set(self, **attrs: Any) -> Span:
        """Attach attributes. The tracer's scrub hook, if any, sees every value first."""
        scrub = self.tracer.scrub if self.tracer is not None else None
        for key, value in attrs.items():
            self.attrs[key] = scrub(key, value) if scrub is not None else value
        return self

    def event(self, name: str, **attrs: Any) -> Span:
        at = self.tracer.clock() if self.tracer is not None else self.start
        scrub = self.tracer.scrub if self.tracer is not None else None
        self.events.append(Event(name, at, {k: (scrub(k, v) if scrub else v) for k, v in attrs.items()}))
        return self

    def add_cost(self, amount: float) -> Span:
        if amount < 0:
            raise ValueError("cost cannot be negative")
        self.cost += amount
        return self

    def record_llm(self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0, price: Price | None = None) -> Span:
        """Record a model call. With ``price`` (per million tokens: input, output, cached input) the cost is added too.

        ``input_tokens`` is the uncached input, ``cached_tokens`` the cache-read input, so the two never double count.
        No prices are built in: they change, and a wrong one is worse than none.
        """
        if min(input_tokens, output_tokens, cached_tokens) < 0:
            raise ValueError("token counts cannot be negative")
        self.kind = "llm"
        self.attrs.update({ATTR_MODEL: model, ATTR_INPUT: input_tokens, ATTR_OUTPUT: output_tokens})
        if cached_tokens:
            self.attrs[ATTR_CACHED] = cached_tokens
        if price is not None:
            m = Decimal(1_000_000)
            total = (Decimal(str(price[0])) * input_tokens + Decimal(str(price[1])) * output_tokens + Decimal(str(price[2])) * cached_tokens) / m
            self.cost += float(total)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            # numbers are normalised to float so an int from a custom clock and its parsed form hash identically
            "id": self.id, "parent": self.parent, "name": self.name, "kind": self.kind, "start": float(self.start),
            "end": None if self.end is None else float(self.end), "wall": float(self.wall), "cost": float(self.cost),
            "error": self.error, "attrs": self.attrs,
            "events": [{"name": e.name, "at": float(e.at), "attrs": e.attrs} for e in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Span:
        return cls(
            id=int(data["id"]), parent=data.get("parent"), name=str(data["name"]), start=float(data["start"]),
            end=None if data.get("end") is None else float(data["end"]), cost=float(data.get("cost", 0.0)), error=data.get("error"),
            attrs=dict(data.get("attrs") or {}), kind=str(data.get("kind", "span")), wall=float(data.get("wall", 0.0)),
            events=[Event(str(e["name"]), float(e["at"]), dict(e.get("attrs") or {})) for e in data.get("events") or []],
        )

"""Getting a trace out: OpenTelemetry JSON, Chrome/Perfetto, flame graph stacks, and a tamper-evident JSONL file."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from . import analysis
from .model import Span

FORMAT = "agent-trace/1"
GENESIS = "0" * 64


class TraceIntegrityError(ValueError):
    """The file's hash chain does not verify: a line was changed, removed, reordered or added."""


# ---------------------------------------------------------------------------------------------- integrity
def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _link(prev: str, payload: str, key: bytes | None) -> str:
    data = (prev + payload).encode("utf-8")
    return hmac.new(key, data, hashlib.sha256).hexdigest() if key else hashlib.sha256(data).hexdigest()


def seal(spans: list[Span], key: bytes | None = None) -> list[str]:
    """A hash chain over the spans: each link covers the previous link and the span's canonical form.

    Without a key anyone can recompute the chain after editing, so store the final hash (the head) somewhere the
    editor cannot reach, such as a ticket, a commit or a signed message. With a ``key`` (HMAC-SHA256) a forger
    needs the key as well.
    """
    chain, prev = [], GENESIS
    for s in spans:
        prev = _link(prev, _canonical(s.to_dict()), key)
        chain.append(prev)
    return chain


def head(spans: list[Span], key: bytes | None = None) -> str:
    chain = seal(spans, key)
    return chain[-1] if chain else GENESIS


# ---------------------------------------------------------------------------------------------- JSONL
def to_jsonl(spans: list[Span], trace_id: str = "", service: str = "agent", key: bytes | None = None) -> str:
    chain = seal(spans, key)
    lines = [_canonical({"format": FORMAT, "trace_id": trace_id, "service": service, "spans": len(spans), "head": chain[-1] if chain else GENESIS})]
    for s, h in zip(spans, chain, strict=True):
        lines.append(_canonical({**s.to_dict(), "hash": h}))
    return "\n".join(lines) + "\n"


def from_jsonl(text: str, key: bytes | None = None, verify: bool = True) -> tuple[list[Span], dict[str, Any]]:
    """Parse a trace file. With ``verify`` (the default) any edit to the file raises ``TraceIntegrityError``."""
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows or rows[0].get("format") != FORMAT:
        raise ValueError(f"not an {FORMAT} file")
    header, body = rows[0], rows[1:]
    spans = [Span.from_dict({k: v for k, v in r.items() if k != "hash"}) for r in body]
    if verify:
        expected = seal(spans, key)
        if len(body) != header.get("spans") or [r.get("hash") for r in body] != expected:
            raise TraceIntegrityError("hash chain does not verify")
        if header.get("head") != (expected[-1] if expected else GENESIS):
            raise TraceIntegrityError("header head does not match the chain")
    return spans, header


# ---------------------------------------------------------------------------------------------- OpenTelemetry
def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": _canonical(value)}


def _otlp_attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _otlp_value(v)} for k, v in attrs.items()]


def _nanos(seconds: float) -> str:
    return str(round(seconds * 1_000_000_000))


def to_otlp(spans: list[Span], trace_id: str, service: str = "agent") -> dict[str, Any]:
    """OTLP/JSON, the body an OpenTelemetry collector's ``/v1/traces`` endpoint accepts."""
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
        raise ValueError("trace_id must be 32 lowercase hex characters")
    out = []
    for s in spans:
        start = s.wall
        end = s.wall + s.duration
        attrs = dict(s.attrs)
        attrs["agent.span.kind"] = s.kind
        if s.cost:
            attrs["agent.cost"] = s.cost
        item: dict[str, Any] = {
            "traceId": trace_id, "spanId": f"{s.id:016x}", "name": s.name, "kind": 1,
            "startTimeUnixNano": _nanos(start), "endTimeUnixNano": _nanos(end), "attributes": _otlp_attrs(attrs),
            "events": [{"timeUnixNano": _nanos(s.wall + (e.at - s.start)), "name": e.name, "attributes": _otlp_attrs(e.attrs)} for e in s.events],
            "status": {"code": 2, "message": s.error} if s.error else {"code": 1},
        }
        if s.parent is not None:
            item["parentSpanId"] = f"{s.parent:016x}"
        out.append(item)
    return {"resourceSpans": [{
        "resource": {"attributes": _otlp_attrs({"service.name": service})},
        "scopeSpans": [{"scope": {"name": "agent-trace"}, "spans": out}],
    }]}


# ---------------------------------------------------------------------------------------------- Chrome / Perfetto
def _end(s: Span) -> float:
    return s.end if s.end is not None else s.start


def to_chrome(spans: list[Span]) -> dict[str, Any]:
    """Trace Event Format, openable at ui.perfetto.dev or chrome://tracing. Parallel spans get their own rows."""
    if not spans:
        return {"traceEvents": [], "displayTimeUnit": "ms"}
    t0 = min(s.start for s in spans)
    lanes: list[list[Span]] = []  # each lane is a stack of open spans, so spans in one lane nest cleanly
    events = []
    for s in sorted(spans, key=lambda x: (x.start, -x.duration)):
        end = s.end if s.end is not None else s.start
        placed = -1
        for i, stack in enumerate(lanes):
            while stack and _end(stack[-1]) <= s.start + analysis.EPS:
                stack.pop()
            # a span shares a lane only with its own parent (it nests under it) or in an empty lane, so siblings
            # that run in parallel are never drawn as if one were the child of the other
            if not stack or (stack[-1].id == s.parent and _end(stack[-1]) >= end - analysis.EPS):
                placed = i
                break
        if placed < 0:
            lanes.append([])
            placed = len(lanes) - 1
        lanes[placed].append(s)
        args = {**s.attrs, "cost": s.cost, "status": s.status}
        if s.error:
            args["error"] = s.error
        events.append({"name": s.name, "cat": s.kind, "ph": "X", "ts": round((s.start - t0) * 1e6, 3), "dur": round(s.duration * 1e6, 3),
                       "pid": 1, "tid": placed + 1, "args": json.loads(_canonical(args))})
    return {"traceEvents": events, "displayTimeUnit": "ms"}

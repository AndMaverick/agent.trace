# agent.trace

[![CI](https://github.com/AndMaverick/agent.trace/actions/workflows/ci.yml/badge.svg)](https://github.com/AndMaverick/agent.trace/actions/workflows/ci.yml)

An agent run that went wrong leaves three questions. Which step made it slow or expensive? What did each call cost? And is the record you are reading the one the run actually produced? Most tracing answers the first, sometimes the second, and assumes the third.

agent.trace is tracing for agent runs with no vendor and no collector. Spans follow threads and asyncio tasks, cost and token usage sit on the span that incurred them, latency is attributed along the path that actually set it, and a trace file carries a hash chain so a later edit shows. Standard library only, typed, Python 3.10 and up.

```
pip install git+https://github.com/AndMaverick/agent.trace
```

## Use

```python
from agent_trace import Tracer

tracer = Tracer(service="support-bot")

with tracer.span("run", kind="agent"):
    with tracer.span("plan") as s:
        s.record_llm("my-model", input_tokens=1200, output_tokens=300, price=(3.0, 15.0, 0.3))
    with tracer.span("search", kind="tool"):
        ...

print(tracer.waterfall())
```

```
run      |==============================| 812.4ms
  plan   |========                      | 230.1ms $0.0081
  search |        ===================== | 580.0ms
```

Async and decorators work the same way, and a failure is stored on the span and raised again, never swallowed:

```python
@tracer.trace(kind="tool")
async def fetch(url): ...

async with tracer.span("fan-out"):
    await asyncio.gather(fetch("a"), fetch("b"))   # both are children of "fan-out", and run in parallel
```

Threads do not inherit context, so pass the parent: `tracer.span("job", parent=run)`.

## Where did the time go

`critical_path()` follows the longest span down the tree. `blocking_path()` answers the sharper question: which spans set the latency from start to finish, given that parallel branches wait for the slowest one. Speeding up a branch that was not last changes nothing, and the blocking path leaves it out.

```python
tracer.blocking_path()      # spans in time order that determine latency
blame(tracer.spans)         # {'search': 0.58, 'plan': 0.23, ...} seconds attributed; for one root they sum to its duration
```

`self_time` counts parallel children once, so a span that waits on four parallel calls is not charged for four times the wait.

## Get it out

| Need | Call |
|---|---|
| OpenTelemetry collector or any OTLP/JSON backend | `to_otlp(spans, trace_id, service)` |
| ui.perfetto.dev or chrome://tracing | `to_chrome(spans)` (parallel spans get their own rows) |
| Flame graph tools | `folded_stacks(spans)` |
| Archive, share, diff | `to_jsonl(...)` and `from_jsonl(...)` |

LLM spans use the OpenTelemetry GenAI attribute names (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, and so on), so a collector reads them as model calls.

## A trace that shows tampering

`to_jsonl` writes a hash chain: each span's hash covers the previous hash and the span. `from_jsonl` verifies it and raises `TraceIntegrityError` if any line is edited, removed, added or reordered.

```
python -m agent_trace verify run.jsonl       # exit 2 on any change
```

Be exact about what this proves. Without a key, anyone who edits a file can recompute the chain, so record the final hash (`head`, printed by `verify`) somewhere the editor cannot reach, such as a ticket, a commit or a signed message. Seal with `key=b"..."` (HMAC-SHA256) and a forger needs the key too.

## Compare two runs

```
python -m agent_trace diff baseline.jsonl candidate.jsonl --threshold 20
```

Self time and cost per span name, largest change first. The exit code is 1 when any span grew past the threshold, so it can gate a pipeline.

## Command line

```
python -m agent_trace summary run.jsonl      # totals, p50 and p95 per name, blame table
python -m agent_trace waterfall run.jsonl
python -m agent_trace convert run.jsonl --to otlp|chrome|folded
```

## Keeping sensitive data out

`Tracer(scrub=fn)` runs `fn(key, value)` on every attribute and event attribute as it is set, so a prompt can be dropped or masked before anything is stored or exported. Pair it with [guard.rails](https://github.com/AndMaverick/guard.rails) to redact personal data. `max_spans` bounds memory, and `dropped` counts what was not stored.

## Honest limits

- Timing is only as good as the clock. The default is monotonic for durations and the system clock for export timestamps, which can jump. Inject both for tests.
- Spans from several processes are separate traces. Joining them is a collector's job, and this library does not propagate context across a network call.
- The hash chain detects edits. It does not prove the events happened, only that the record was not changed afterwards.
- No token prices are built in. Prices change, and a wrong one is worse than none. Pass your own.

## Tests

```
python -m unittest discover -s tests -t . -v
```

**Related repositories:** [mao-framework](https://github.com/AndMaverick/mao-framework) has a smaller module, `mao.trace`, that attaches cost to the span that incurred it. [incident.log](https://github.com/AndMaverick/incident.log) keeps the register of what happened once a trace shows something went wrong.

MIT licensed. Built by [AndMaverick LLC](https://www.andmaverick.com).

---

**Read the thinking:** [Nobody Signs for the Agent](https://blog.andmaverick.com/nobody-signs-for-the-agent?utm_source=github&utm_medium=repo&utm_campaign=agent.trace)

**If this is your problem.** If a system you run can fail in ways that matter, the controls belong in the architecture, not in the postmortem. I am happy to talk through yours. [Begin a conversation](https://calendly.com/andmaverick?utm_source=github&utm_medium=repo&utm_campaign=agent.trace).

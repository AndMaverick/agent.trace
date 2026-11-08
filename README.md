# agent.trace

Tracing for agent runs, without a vendor or a collector. Nested spans with timing, cost attached at the span that incurred it, rollups over any subtree, error capture, and a critical path that shows where the time actually went. Standard library only.

## Use

```python
from agent_trace import Tracer

tracer = Tracer()
with tracer.span("run"):
    with tracer.span("plan", cost=0.01):
        ...
    with tracer.span("act"):
        with tracer.span("search", cost=0.02):
            ...
        with tracer.span("write", cost=0.03):
            ...

print(tracer.render())
tracer.total_cost()                       # 0.06
[s.name for s in tracer.critical_path()]  # the slowest chain, root to leaf
```

## Design

- **Context managers.** A span closes and records its end time even when the body raises. The error is stored on the span and re-raised, so tracing never swallows a failure.
- **Cost where it happens.** Attach spend to the span that incurred it, then roll up over any subtree with `total_cost(span_id)`.
- **Critical path.** Follows the longest child at each level, so the answer to "why was this run slow" is a short list.
- **Injectable clock.** Pass your own clock for deterministic tests. The suite uses one.

## Tests

```
python -m unittest discover -s tests -t . -v
```

MIT licensed. Built by [AndMaverick LLC](https://www.andmaverick.com).

---

**If this is your problem.** If a system you run can fail in ways that matter, the controls belong in the architecture, not in the postmortem. I am happy to talk through yours. [Begin a conversation](https://calendly.com/andmaverick?utm_source=github&utm_medium=repo&utm_campaign=agent.trace).

import asyncio
import threading
import unittest

from agent_trace import Tracer


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class ContextTests(unittest.TestCase):
    def test_asyncio_tasks_share_a_parent_and_run_in_parallel(self):
        clock = Clock()
        tracer = Tracer(clock)

        async def work(name, secs):
            async with tracer.span(name):
                await asyncio.sleep(0)
                clock.tick(secs)

        async def main():
            async with tracer.span("run"):
                await asyncio.gather(work("a", 1), work("b", 2))

        asyncio.run(main())
        by = {s.name: s for s in tracer.spans}
        self.assertEqual(by["a"].parent, by["run"].id)
        self.assertEqual(by["b"].parent, by["run"].id)
        self.assertEqual(tracer.current, None)

    def test_threads_need_an_explicit_parent(self):
        tracer = Tracer()
        with tracer.span("run") as run:
            def job(name):
                with tracer.span(name, parent=run):
                    pass
            threads = [threading.Thread(target=job, args=(f"t{i}",)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(tracer.children(run.id)), 8)
        self.assertEqual(len({s.id for s in tracer.spans}), 9)  # ids are unique under concurrency

    def test_thread_without_parent_starts_its_own_root(self):
        tracer = Tracer()
        with tracer.span("run"):
            t = threading.Thread(target=lambda: tracer.span("orphan").__enter__())
            t.start()
            t.join()
        self.assertEqual([s.parent for s in tracer.spans if s.name == "orphan"], [None])

    def test_two_tracers_do_not_adopt_each_others_spans(self):
        a, b = Tracer(), Tracer()
        with a.span("a"), b.span("b"):
            pass
        self.assertIsNone(b.spans[0].parent)

    def test_decorator_sync_and_async(self):
        tracer = Tracer()

        @tracer.trace
        def add(x, y):
            return x + y

        @tracer.trace(name="fetch", kind="tool")
        async def fetch():
            return add(1, 2)

        self.assertEqual(asyncio.run(fetch()), 3)
        self.assertEqual([(s.name, s.kind) for s in tracer.spans], [("fetch", "tool"), ("ContextTests.test_decorator_sync_and_async.<locals>.add", "span")])
        self.assertEqual(tracer.spans[1].parent, tracer.spans[0].id)

    def test_error_is_stored_and_reraised_in_async(self):
        tracer = Tracer(Clock())

        async def boom():
            async with tracer.span("x"):
                raise RuntimeError("no")

        with self.assertRaises(RuntimeError):
            asyncio.run(boom())
        self.assertEqual(tracer.spans[0].status, "error")

    def test_record_adds_a_finished_span(self):
        tracer = Tracer(Clock())
        with tracer.span("run") as run:
            s = tracer.record("external", 1.0, 3.5, cost=0.2, kind="tool")
        self.assertEqual((s.parent, s.duration), (run.id, 2.5))
        with self.assertRaises(ValueError):
            tracer.record("bad", 2.0, 1.0)

    def test_max_spans_caps_memory_and_reattaches_children(self):
        tracer = Tracer(max_spans=2)
        with tracer.span("a"), tracer.span("b"), tracer.span("dropped"), tracer.span("under-dropped"):
            pass
        self.assertEqual(len(tracer.spans), 2)
        self.assertEqual(tracer.dropped, 2)

    def test_negative_cost_rejected(self):
        with self.assertRaises(ValueError):
            Tracer().span("x", cost=-1).__enter__()

    def test_scrub_hook_runs_on_attrs_and_events(self):
        tracer = Tracer(scrub=lambda k, v: "[removed]" if k == "prompt" else v)
        with tracer.span("llm", prompt="my secret", n=3) as s:
            s.set(prompt="again")
            s.event("e", prompt="x", ok=True)
        self.assertEqual(s.attrs, {"prompt": "[removed]", "n": 3})
        self.assertEqual(s.events[0].attrs, {"prompt": "[removed]", "ok": True})

    def test_deep_nesting(self):
        tracer = Tracer(Clock())
        ctxs = [tracer.span(f"s{i}") for i in range(400)]
        for c in ctxs:
            c.__enter__()
        for c in reversed(ctxs):
            c.__exit__(None, None, None)
        self.assertEqual(len(tracer.render().splitlines()), 400)
        self.assertEqual(len(tracer.blocking_path()), 400)

    def test_bad_max_spans(self):
        with self.assertRaises(ValueError):
            Tracer(max_spans=0)


class LlmTests(unittest.TestCase):
    def test_record_llm_sets_conventional_attributes_and_exact_cost(self):
        tracer = Tracer()
        with tracer.span("call") as s:
            s.record_llm("model-x", 1000, 500, cached_tokens=2000, price=(3.0, 15.0, 0.3))
        self.assertEqual(s.kind, "llm")
        self.assertEqual(s.attrs["gen_ai.request.model"], "model-x")
        self.assertEqual(s.attrs["gen_ai.usage.input_tokens"], 1000)
        self.assertEqual(s.attrs["gen_ai.usage.cached_input_tokens"], 2000)
        self.assertEqual(s.cost, 0.0111)  # (3*1000 + 15*500 + 0.3*2000) / 1e6
        with self.assertRaises(ValueError):
            s.record_llm("m", -1, 0)


if __name__ == "__main__":
    unittest.main()

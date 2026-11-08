import unittest

from agent_trace import Tracer


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


def build():
    clock = Clock()
    tracer = Tracer(clock)
    with tracer.span("run") as _:
        with tracer.span("plan", cost=0.01):
            clock.tick(1)
        with tracer.span("act") as _:
            with tracer.span("search", cost=0.02):
                clock.tick(2)
            with tracer.span("write", cost=0.03):
                clock.tick(5)
    return tracer


class TracerTests(unittest.TestCase):
    def test_parent_child_structure(self):
        tracer = build()
        run = tracer.spans[0]
        self.assertEqual([s.name for s in tracer.children(run.id)], ["plan", "act"])

    def test_durations(self):
        tracer = build()
        by_name = {s.name: s for s in tracer.spans}
        self.assertEqual(by_name["run"].duration, 8)
        self.assertEqual(by_name["write"].duration, 5)

    def test_cost_rollups(self):
        tracer = build()
        by_name = {s.name: s for s in tracer.spans}
        self.assertAlmostEqual(tracer.total_cost(), 0.06)
        self.assertAlmostEqual(tracer.total_cost(by_name["act"].id), 0.05)

    def test_critical_path(self):
        names = [s.name for s in build().critical_path()]
        self.assertEqual(names, ["run", "act", "write"])

    def test_errors_are_recorded_and_reraised(self):
        tracer = Tracer(Clock())
        with self.assertRaises(ValueError):
            with tracer.span("bad"):
                raise ValueError("nope")
        self.assertEqual(tracer.errors()[0].error, "ValueError: nope")
        self.assertIsNotNone(tracer.spans[0].end)

    def test_render_indents_by_depth(self):
        lines = build().render().splitlines()
        self.assertTrue(lines[0].startswith("run"))
        self.assertTrue(lines[1].startswith("  plan"))
        self.assertTrue(lines[-1].startswith("    write"))


if __name__ == "__main__":
    unittest.main()

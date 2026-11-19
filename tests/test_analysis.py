import math
import unittest

from agent_trace import (
    Tracer,
    blame,
    blocking_path,
    folded_stacks,
    percentile,
    self_time,
    stats_by_name,
)
from agent_trace.analysis import render_waterfall


def parallel():
    """run 0..10: plan 0..2, then fanout 2..9 holding a (2..4) and b (2..9) in parallel, then write 9..10."""
    t = Tracer(lambda: 0.0)
    r = t.record("run", 0, 10)
    t.record("plan", 0, 2, parent=r)
    f = t.record("fanout", 2, 9, parent=r)
    t.record("a", 2, 4, parent=f)
    t.record("b", 2, 9, parent=f)
    t.record("write", 9, 10, parent=r)
    return t


class PathTests(unittest.TestCase):
    def test_blocking_path_skips_the_parallel_branch_that_was_not_last(self):
        names = [s.name for s in parallel().blocking_path()]
        self.assertEqual(names, ["run", "plan", "fanout", "b", "write"])
        self.assertNotIn("a", names)
        self.assertIn("b", names)

    def test_blame_sums_to_the_root_duration(self):
        t = parallel()
        b = blame(t.spans)
        self.assertAlmostEqual(sum(b.values()), 10.0)
        self.assertAlmostEqual(b["b"], 7.0)
        self.assertAlmostEqual(b["plan"], 2.0)
        self.assertNotIn("a", b)

    def test_self_time_counts_parallel_children_once(self):
        t = parallel()
        fanout = next(s for s in t.spans if s.name == "fanout")
        self.assertAlmostEqual(self_time(fanout, t.children(fanout.id)), 0.0)  # a and b cover all of 2..9
        run = t.spans[0]
        self.assertAlmostEqual(self_time(run, t.children(run.id)), 0.0)

    def test_gaps_belong_to_the_parent(self):
        t = Tracer(lambda: 0.0)
        r = t.record("run", 0, 10)
        t.record("a", 1, 3, parent=r)
        self.assertAlmostEqual(self_time(r, t.children(r.id)), 8.0)

    def test_empty(self):
        t = Tracer()
        self.assertEqual((t.critical_path(), t.blocking_path(), t.render(), t.waterfall()), ([], [], "", ""))

    def test_orphan_whose_parent_was_dropped_becomes_a_root(self):
        t = Tracer(lambda: 0.0)
        s = t.record("x", 0, 1)
        s.parent = 999
        self.assertEqual([x.name for x in t.blocking_path()], ["x"])


class StatsTests(unittest.TestCase):
    def test_percentiles_nearest_rank(self):
        v = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual((percentile(v, 50), percentile(v, 95), percentile(v, 100), percentile([], 50)), (5, 10, 10, 0.0))

    def test_stats_by_name(self):
        t = Tracer(lambda: 0.0)
        for d in (1, 2, 3):
            t.record("call", 0, d, cost=0.1, error="x" if d == 3 else None)
        st = stats_by_name(t.spans)["call"]
        self.assertEqual((st.count, st.total, st.max, st.errors), (3, 6, 3, 1))
        self.assertTrue(math.isclose(st.cost, 0.3))

    def test_total_cost_has_no_float_drift(self):
        t = Tracer(lambda: 0.0)
        for _ in range(1000):
            t.record("x", 0, 1, cost=0.1)
        self.assertEqual(t.total_cost(), 100.0)

    def test_unknown_span_id(self):
        with self.assertRaises(KeyError):
            Tracer().total_cost(5)


class RenderTests(unittest.TestCase):
    def test_waterfall_places_bars_by_time(self):
        rows = render_waterfall(parallel().spans, 20).splitlines()
        self.assertEqual(len(rows), 6)
        self.assertTrue(rows[0].split("|")[1].strip("= ") == "")  # root bar fills the width
        self.assertIn("====================", rows[0])
        plan = next(r for r in rows if r.startswith("  plan"))
        self.assertTrue(plan.split("|")[1].startswith("===="))

    def test_waterfall_marks_errors(self):
        t = Tracer(lambda: 0.0)
        t.record("bad", 0, 1, error="boom")
        self.assertIn("x", t.waterfall(10))

    def test_folded_stacks_use_self_time(self):
        lines = folded_stacks(parallel().spans)
        self.assertIn("run;fanout;b 7000000", lines)
        self.assertIn("run;plan 2000000", lines)
        self.assertFalse(any(line.startswith("run ") for line in lines))  # run has no self time


class BlockingPathModuleLevel(unittest.TestCase):
    def test_function_matches_method(self):
        t = parallel()
        self.assertEqual(blocking_path(t.spans), t.blocking_path())


if __name__ == "__main__":
    unittest.main()

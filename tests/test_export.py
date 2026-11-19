import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_trace import (
    TraceIntegrityError,
    Tracer,
    diff,
    from_jsonl,
    head,
    regressions,
    render_summary,
    seal,
    summary,
    to_chrome,
    to_jsonl,
    to_otlp,
)
from agent_trace.cli import main
from tests.test_analysis import parallel


def sample():
    t = Tracer(lambda: 0.0, trace_id="a" * 32, service="svc")
    r = t.record("run", 0, 10, kind="agent", user="u1", ok=True, n=3, ratio=0.5)
    s = t.record("search", 1, 4, cost=0.02, parent=r, error="timeout")
    s.event("retry", attempt=2)
    t.record("write", 4, 9, cost=0.03, parent=r)
    return t


class JsonlTests(unittest.TestCase):
    def test_round_trip_preserves_everything(self):
        t = sample()
        spans, header = from_jsonl(to_jsonl(t.spans, t.trace_id, t.service))
        self.assertEqual([s.to_dict() for s in spans], [s.to_dict() for s in t.spans])
        self.assertEqual((header["trace_id"], header["service"], header["spans"]), ("a" * 32, "svc", 3))
        self.assertEqual(header["head"], head(t.spans))

    def test_any_edit_is_detected(self):
        text = to_jsonl(sample().spans)
        lines = text.splitlines()
        tampered = {
            "changed value": text.replace('"cost":0.02', '"cost":0.01'),
            "removed span": "\n".join(lines[:2] + lines[3:]) + "\n",
            "reordered": "\n".join([lines[0], lines[2], lines[1], lines[3]]) + "\n",
            "dropped last": "\n".join(lines[:-1]) + "\n",
            "forged head": text.replace(json.loads(lines[0])["head"], "f" * 64),
        }
        for label, bad in tampered.items():
            with self.assertRaises(TraceIntegrityError, msg=label):
                from_jsonl(bad)
        from_jsonl(tampered["changed value"], verify=False)  # still readable when explicitly unverified

    def test_a_recomputed_chain_needs_the_key_when_keyed(self):
        spans = sample().spans
        text = to_jsonl(spans, key=b"k1")
        from_jsonl(text, key=b"k1")
        with self.assertRaises(TraceIntegrityError):
            from_jsonl(text, key=b"wrong")
        with self.assertRaises(TraceIntegrityError):
            from_jsonl(text)  # an unkeyed verifier cannot validate a keyed chain
        self.assertNotEqual(seal(spans), seal(spans, b"k1"))

    def test_rejects_other_formats(self):
        with self.assertRaises(ValueError):
            from_jsonl('{"hello": 1}\n')
        with self.assertRaises(ValueError):
            from_jsonl("")

    def test_empty_trace_round_trips(self):
        spans, header = from_jsonl(to_jsonl([]))
        self.assertEqual((spans, header["spans"]), ([], 0))


class FormatTests(unittest.TestCase):
    def test_otlp_shape(self):
        t = sample()
        doc = to_otlp(t.spans, t.trace_id, "svc")
        spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 3)
        run, search = spans[0], spans[1]
        self.assertEqual((run["traceId"], run["spanId"], "parentSpanId" in run), ("a" * 32, "0000000000000001", False))
        self.assertEqual(search["parentSpanId"], "0000000000000001")
        self.assertEqual(search["status"], {"code": 2, "message": "timeout"})
        self.assertEqual(run["status"], {"code": 1})
        self.assertEqual(int(search["endTimeUnixNano"]) - int(search["startTimeUnixNano"]), 3_000_000_000)
        values = {a["key"]: a["value"] for a in run["attributes"]}
        self.assertEqual(values["n"], {"intValue": "3"})
        self.assertEqual(values["ok"], {"boolValue": True})
        self.assertEqual(values["ratio"], {"doubleValue": 0.5})
        self.assertEqual(values["agent.span.kind"], {"stringValue": "agent"})
        self.assertEqual(search["events"][0]["name"], "retry")
        res = doc["resourceSpans"][0]["resource"]["attributes"][0]
        self.assertEqual((res["key"], res["value"]), ("service.name", {"stringValue": "svc"}))

    def test_otlp_rejects_a_bad_trace_id(self):
        with self.assertRaises(ValueError):
            to_otlp(sample().spans, "xyz")

    def test_chrome_events_and_lanes(self):
        doc = to_chrome(parallel().spans)
        ev = {e["name"]: e for e in doc["traceEvents"]}
        self.assertEqual(ev["run"]["dur"], 10_000_000.0)
        self.assertEqual(ev["plan"]["ts"], 0.0)
        # a and b overlap without nesting, so they cannot share a lane
        self.assertNotEqual(ev["a"]["tid"], ev["b"]["tid"])
        # but a child nested in its parent can
        self.assertEqual(ev["run"]["tid"], ev["plan"]["tid"])
        json.dumps(doc)
        self.assertEqual(to_chrome([])["traceEvents"], [])


class CompareTests(unittest.TestCase):
    def build(self, slow):
        t = Tracer(lambda: 0.0)
        r = t.record("run", 0, 10 + slow)
        t.record("search", 0, 3 + slow, parent=r, cost=0.01)
        t.record("write", 3 + slow, 10 + slow, parent=r)
        return t

    def test_diff_and_regressions(self):
        a, b = self.build(0), self.build(4)
        rows = {d.name: d for d in diff(a.spans, b.spans)}
        self.assertAlmostEqual(rows["search"].self_change, 4.0)
        self.assertAlmostEqual(rows["search"].self_pct, 4 / 3 * 100)
        self.assertEqual(rows["write"].self_change, 0)
        bad = regressions(a.spans, b.spans, threshold_pct=50)
        self.assertEqual([d.name for d in bad], ["search"])
        self.assertEqual(regressions(a.spans, a.spans), [])

    def test_new_and_gone_spans(self):
        a = self.build(0)
        b = Tracer(lambda: 0.0)
        b.record("run", 0, 10)
        b.record("brand-new", 0, 5)
        rows = {d.name: d.status for d in diff(a.spans, b.spans)}
        self.assertEqual(rows, {"brand-new": "new", "run": "changed", "search": "gone", "write": "gone"})
        self.assertIn("brand-new", [d.name for d in regressions(a.spans, b.spans)])


class SummaryTests(unittest.TestCase):
    def test_summary_data_and_text(self):
        t = sample()
        data = summary(t.spans)
        self.assertEqual((data["spans"], data["errors"], data["open"]), (3, 1, 0))
        self.assertAlmostEqual(data["total_cost"], 0.05)
        self.assertEqual(data["wall_seconds"], 10)
        text = render_summary(t.spans)
        self.assertIn("blocking path", text)
        self.assertIn("search", text)
        json.dumps(data)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        t = sample()
        self.path = os.path.join(self.dir.name, "t.jsonl")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(to_jsonl(t.spans, t.trace_id, t.service))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_commands(self):
        self.assertIn("ok: 3 spans", self.run_cli("verify", self.path)[1])
        self.assertIn("blocking path", self.run_cli("summary", self.path)[1])
        self.assertEqual(json.loads(self.run_cli("summary", "--json", self.path)[1])["spans"], 3)
        self.assertIn("search", self.run_cli("waterfall", "--width", "30", self.path)[1])
        self.assertIn("  search", self.run_cli("tree", self.path)[1])
        otlp = json.loads(self.run_cli("convert", self.path, "--to", "otlp")[1])
        self.assertEqual(otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"], "a" * 32)
        self.assertIn("traceEvents", json.loads(self.run_cli("convert", self.path, "--to", "chrome")[1]))
        self.assertIn("run;search", self.run_cli("convert", self.path, "--to", "folded")[1])

    def test_tampering_exits_2(self):
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read().replace('"cost":0.03', '"cost":0.99')
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        code, _out, err = self.run_cli("verify", self.path)
        self.assertEqual(code, 2)
        self.assertIn("INTEGRITY FAILURE", err)

    def test_missing_file_exits_2(self):
        self.assertEqual(self.run_cli("verify", os.path.join(self.dir.name, "nope"))[0], 2)

    def test_diff_exit_code_signals_regression(self):
        slow = Tracer(lambda: 0.0)
        r = slow.record("run", 0, 20)
        slow.record("search", 0, 15, parent=r)
        path2 = os.path.join(self.dir.name, "slow.jsonl")
        with open(path2, "w", encoding="utf-8") as fh:
            fh.write(to_jsonl(slow.spans))
        self.assertEqual(self.run_cli("diff", self.path, self.path)[0], 0)
        code, out, _ = self.run_cli("diff", self.path, path2)
        self.assertEqual(code, 1)
        self.assertIn("regression", out)


if __name__ == "__main__":
    unittest.main()

"""``python -m agent_trace``: read a trace file (JSONL from ``to_jsonl``) and report on it. Local only."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import analysis, compare, export, report
from .model import Span


def _load(path: str, key: bytes | None, verify: bool = True) -> tuple[list[Span], dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return export.from_jsonl(fh.read(), key, verify)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent_trace", description="Inspect, convert, verify and compare agent traces.")
    ap.add_argument("--key", help="HMAC key the trace was sealed with (verification only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("summary", "waterfall", "tree", "verify"):
        sub.add_parser(name).add_argument("file")
    sub.choices["summary"].add_argument("--json", action="store_true")
    sub.choices["waterfall"].add_argument("--width", type=int, default=60)
    conv = sub.add_parser("convert")
    conv.add_argument("file")
    conv.add_argument("--to", choices=["otlp", "chrome", "folded"], required=True)
    d = sub.add_parser("diff")
    d.add_argument("baseline")
    d.add_argument("candidate")
    d.add_argument("--threshold", type=float, default=20.0, help="percent self-time growth that counts as a regression")
    d.add_argument("--min-ms", type=float, default=0.0)
    args = ap.parse_args(argv)
    key = args.key.encode() if args.key else None
    try:
        if args.cmd == "verify":
            spans, header = _load(args.file, key)
            print(f"ok: {len(spans)} spans, head {header['head']}")
            return 0
        if args.cmd == "diff":
            a, _ = _load(args.baseline, key)
            b, _ = _load(args.candidate, key)
            bad = compare.regressions(a, b, args.threshold, args.min_ms / 1000)
            for row in compare.diff(a, b):
                pct = "new" if row.self_pct is None else f"{row.self_pct:+.0f}%"
                print(f"{row.name[:30]:<30} self {row.self_a * 1000:>9.1f} -> {row.self_b * 1000:>9.1f}ms  {pct:>6}  cost {row.cost_change:+.4f}")
            if bad:
                print(f"\n{len(bad)} regression(s): " + ", ".join(r.name for r in bad))
            return 1 if bad else 0
        spans, header = _load(args.file, key)
    except export.TraceIntegrityError as exc:
        print(f"INTEGRITY FAILURE: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "summary":
        print(json.dumps(report.summary(spans), indent=2) if args.json else report.render_summary(spans))
    elif args.cmd == "waterfall":
        print(analysis.render_waterfall(spans, args.width))
    elif args.cmd == "tree":
        print(analysis.render_tree(spans))
    elif args.to == "otlp":
        print(json.dumps(export.to_otlp(spans, header.get("trace_id") or "0" * 32, header.get("service", "agent")), indent=2))
    elif args.to == "chrome":
        print(json.dumps(export.to_chrome(spans)))
    else:
        print("\n".join(analysis.folded_stacks(spans)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

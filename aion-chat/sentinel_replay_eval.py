"""Offline Sentinel replay/eval CLI.

This command is intentionally dry-run only. It reads local cases, evaluates the
current fixture/builder output shape, and optionally writes a report for manual
review. It does not call providers, Core, databases, or websockets.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from app.sentinel.eval import (
    DEFAULT_SENTINEL_REPLAY_TRACE_DIR,
    attention_snapshot_builder,
    evaluate_cases,
    fixture_snapshot_builder,
)


DEFAULT_CASES_PATH = Path(__file__).resolve().parent / "app" / "sentinel" / "eval_cases.json"
SNAPSHOT_BUILDERS = {
    "attention": attention_snapshot_builder,
    "fixture": fixture_snapshot_builder,
}


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict]:
    case_path = Path(path)
    data = json.loads(case_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"sentinel eval cases must be a JSON list: {case_path}")
    return data


def build_markdown(result: dict) -> str:
    metrics = result["metrics"]
    lines = [
        "# Sentinel Replay Eval",
        "",
        f"schema_version: `{result['schema_version']}`",
        f"runtime_mode: `{result['runtime_mode']}`",
        f"snapshot_builder: `{result.get('snapshot_builder', '')}`",
        f"trace_dir: `{result['trace_dir']}`",
        "",
        "## Summary",
        "",
        f"- total: {metrics['total']}",
        f"- passed: {metrics['passed']}",
        f"- failed: {metrics['failed']}",
        f"- pass_rate: {metrics['pass_rate']}",
        "",
        "## Records",
        "",
    ]
    for record in result["records"]:
        status = "PASS" if record["ok"] else "FAIL"
        lines.append(f"### {status} {record['id']}")
        lines.append("")
        lines.append(f"- category: `{record['category']}`")
        lines.append(f"- trace_id: `{record['trace']['trace_id']}`")
        if record["failures"]:
            lines.append("- failures:")
            lines.extend(f"  - {failure}" for failure in record["failures"])
        else:
            lines.append("- failures: none")
        compact_text = record["actual"].get("compact_text") or ""
        if compact_text:
            lines.append(f"- compact_text: {compact_text}")
        lines.append("")
    return "\n".join(lines)


def _write_text(path: str | Path, content: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline Sentinel replay eval")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--builder", choices=sorted(SNAPSHOT_BUILDERS), default="attention")
    parser.add_argument("--trace-dir", default=DEFAULT_SENTINEL_REPLAY_TRACE_DIR)
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-md", default="")
    parser.add_argument("--no-fail", action="store_true", help="always exit 0 even if cases fail")
    args = parser.parse_args()

    result = evaluate_cases(
        load_cases(args.cases),
        snapshot_builder=SNAPSHOT_BUILDERS[args.builder],
        snapshot_builder_name=args.builder,
        trace_dir=args.trace_dir,
    )
    if args.output_json:
        _write_text(args.output_json, json.dumps(result, ensure_ascii=False, indent=2))
        print(args.output_json)
    if args.output_md:
        _write_text(args.output_md, build_markdown(result))
        print(args.output_md)
    if not args.output_json and not args.output_md:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["metrics"]["failed"] and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

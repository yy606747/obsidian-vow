"""CLI for offline Sentinel Judgment replay checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.sentinel.eval import attention_snapshot_builder
from app.sentinel.judgment_eval import (
    DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR,
    evaluate_judgment_cases,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_ATTENTION_CASES_PATH = ROOT / "app" / "sentinel" / "eval_cases.json"
DEFAULT_JUDGMENT_CASES_PATH = ROOT / "app" / "sentinel" / "judgment_eval_cases.json"


def load_cases(case_path: Path) -> list[dict[str, Any]]:
    data = json.loads(case_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"sentinel judgment eval cases must be a JSON list: {case_path}")
    return data


def build_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Sentinel Judgment Eval",
        "",
        f"- schema_version: `{result['schema_version']}`",
        f"- runtime_mode: `{result['runtime_mode']}`",
        f"- judgment_schema_version: `{result['judgment_schema_version']}`",
        f"- total: `{result['metrics']['total']}`",
        f"- passed: `{result['metrics']['passed']}`",
        f"- failed: `{result['metrics']['failed']}`",
        "",
        "## Cases",
        "",
    ]
    for record in result["records"]:
        status = "PASS" if record["ok"] else "FAIL"
        lines.append(f"### {record['id']} - {status}")
        lines.append("")
        lines.append(f"- category: `{record['category']}`")
        lines.append(f"- attention_case_id: `{record['attention_case_id']}`")
        lines.append(f"- wake_intent: `{record['actual']['wake_intent']}`")
        lines.append(f"- score: `{record['actual']['score']}`")
        if record["failures"]:
            lines.append("- failures:")
            for failure in record["failures"]:
                lines.append(f"  - `{failure}`")
        lines.append("")
    return "\n".join(lines)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline Sentinel Judgment replay eval")
    parser.add_argument("--cases", type=Path, default=DEFAULT_JUDGMENT_CASES_PATH)
    parser.add_argument("--attention-cases", type=Path, default=DEFAULT_ATTENTION_CASES_PATH)
    parser.add_argument("--trace-dir", default=DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after printing report")
    args = parser.parse_args()

    judgment_cases = load_cases(args.cases)
    attention_cases = load_cases(args.attention_cases)
    result = evaluate_judgment_cases(
        judgment_cases,
        attention_cases=attention_cases,
        snapshot_builder=attention_snapshot_builder,
        trace_dir=args.trace_dir,
    )
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.output_json:
        _write_text(args.output_json, output + "\n")
    if args.output_md:
        _write_text(args.output_md, build_markdown(result) + "\n")
    if result["metrics"]["failed"] and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Gold-set evaluation for Memory V2 turn planning.

Batch 2.3e is intentionally offline and DB-free. It validates taxonomy,
intent detection, namespace policy, and abstain behavior against a balanced
hand-written set instead of relying only on historical chat distribution.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

from app.memory_v2.taxonomy import TAXONOMY_SOURCE, classify_query

from .v2_recall import allowed_namespaces, analyze_turn


DEFAULT_CASES_PATH = Path(__file__).with_name("eval_cases.json")
DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[3] / "migration_dumps" / "server_memory"


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict]:
    case_path = Path(path)
    with case_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"gold eval cases must be a JSON list: {case_path}")
    return data


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _check_equal(name: str, actual, expected, failures: list[str]) -> None:
    if actual != expected:
        failures.append(f"{name}: expected {expected!r}, got {actual!r}")


def _check_include(name: str, actual: list, expected: list, failures: list[str]) -> None:
    missing = [item for item in expected if item not in actual]
    if missing:
        failures.append(f"{name}: missing {missing!r}, actual {actual!r}")


def _check_exclude(name: str, actual: list, forbidden: list, failures: list[str]) -> None:
    present = [item for item in forbidden if item in actual]
    if present:
        failures.append(f"{name}: forbidden {present!r}, actual {actual!r}")


def evaluate_case(case: dict) -> dict:
    if not isinstance(case, dict):
        raise ValueError(f"case must be an object: {case!r}")
    case_id = case.get("id") or ""
    text = case.get("text") or ""
    if not case_id or not text:
        raise ValueError(f"case requires id and text: {case!r}")

    classification = classify_query(text)
    keywords = case.get("keywords")
    if keywords is None:
        keywords = classification["keywords"]
    mode = case.get("mode")
    if mode is None:
        mode = "intimate" if classification["query_type"] == "intimate" else "normal"
    namespace = case.get("namespace")
    if namespace is None and classification["query_type"] != "normal":
        namespace = classification["query_type"]

    plan = analyze_turn(text, keywords, mode=mode, namespace=namespace)
    allowed = allowed_namespaces(plan)
    actual = {
        "needs_memory": plan["needs_memory"],
        "query_type": classification["query_type"],
        "is_open_loop_query": classification["is_open_loop_query"],
        "namespace": plan["namespace"],
        "detected_namespaces": plan["detected_namespaces"],
        "preferred_kinds": plan["preferred_kinds"],
        "allowed_namespaces": allowed,
        "keywords": keywords,
        "terms": plan["terms"],
    }

    failures: list[str] = []
    expect = case.get("expect") or {}
    for key in ("needs_memory", "query_type", "is_open_loop_query", "namespace"):
        if key in expect:
            _check_equal(key, actual[key], expect[key], failures)
    for key in ("detected_namespaces", "preferred_kinds", "allowed_namespaces"):
        if key in expect:
            _check_equal(key, actual[key], expect[key], failures)
        include_key = f"{key}_include"
        exclude_key = f"{key}_exclude"
        if include_key in expect:
            _check_include(key, actual[key], _as_list(expect[include_key]), failures)
        if exclude_key in expect:
            _check_exclude(key, actual[key], _as_list(expect[exclude_key]), failures)

    return {
        "id": case_id,
        "category": case.get("category") or "uncategorized",
        "text": text,
        "ok": not failures,
        "failures": failures,
        "expect": expect,
        "actual": actual,
    }


def evaluate_cases(cases: list[dict]) -> dict:
    records = [evaluate_case(case) for case in cases]
    by_category = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    for record in records:
        bucket = by_category[record["category"]]
        bucket["total"] += 1
        if record["ok"]:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1

    query_type_counts = Counter(record["actual"]["query_type"] for record in records)
    namespace_counts = Counter(
        namespace
        for record in records
        for namespace in record["actual"]["detected_namespaces"]
    )
    intent_counts = Counter(
        kind
        for record in records
        for kind in record["actual"]["preferred_kinds"]
    )
    total = len(records)
    passed = sum(1 for record in records if record["ok"])
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "taxonomy_source": TAXONOMY_SOURCE,
        "metrics": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "needs_memory_count": sum(1 for record in records if record["actual"]["needs_memory"]),
            "open_loop_count": sum(1 for record in records if record["actual"]["is_open_loop_query"]),
            "by_category": dict(by_category),
            "query_type_counts": dict(query_type_counts),
            "detected_namespace_counts": dict(namespace_counts),
            "preferred_kind_counts": dict(intent_counts),
        },
        "records": records,
    }


def _preview(text: str, length: int = 72) -> str:
    return " ".join((text or "").split())[:length]


def build_markdown(result: dict) -> str:
    metrics = result["metrics"]
    lines = [
        "# Memory V2 Gold Eval",
        "",
        f"生成时间：{result['generated_at']}",
        f"taxonomy_source：`{result['taxonomy_source']}`",
        "",
        "说明：",
        "",
        "- 这是 Batch 2.3e 的平衡样本评测，不依赖 SQLite，不调用外部模型。",
        "- 它只检查 turn planning：是否需要记忆、query_type、namespace、intent、allowed_namespaces。",
        "- 历史消息回放用于观察真实使用分布；gold eval 用于观察架构基本理智。",
        "",
        "## 总览",
        "",
        "```text",
        f"total: {metrics['total']}",
        f"passed: {metrics['passed']}",
        f"failed: {metrics['failed']}",
        f"pass_rate: {metrics['pass_rate']}",
        f"needs_memory_count: {metrics['needs_memory_count']}",
        f"open_loop_count: {metrics['open_loop_count']}",
        "```",
        "",
        "## 分类通过率",
        "",
        "| category | total | passed | failed |",
        "| --- | ---: | ---: | ---: |",
    ]
    for category, bucket in sorted(metrics["by_category"].items()):
        lines.append(
            f"| {category} | {bucket['total']} | {bucket['passed']} | {bucket['failed']} |"
        )

    lines += [
        "",
        "## Query Type 分布",
        "",
        "```json",
        json.dumps(metrics["query_type_counts"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Detected Namespace 分布",
        "",
        "```json",
        json.dumps(metrics["detected_namespace_counts"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Preferred Kind 分布",
        "",
        "```json",
        json.dumps(metrics["preferred_kind_counts"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 失败样本",
        "",
    ]

    failed = [record for record in result["records"] if not record["ok"]]
    if not failed:
        lines.append("_无失败样本_")
    else:
        lines += [
            "| id | category | query | failures | actual |",
            "| --- | --- | --- | --- | --- |",
        ]
        for record in failed:
            failures = "<br>".join(record["failures"])
            actual = json.dumps(record["actual"], ensure_ascii=False)
            lines.append(
                f"| {record['id']} | {record['category']} | {_preview(record['text'])} "
                f"| {failures} | `{actual}` |"
            )

    lines += [
        "",
        "## 通过样本摘要",
        "",
        "| id | category | query_type | namespaces | kinds | query |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in result["records"]:
        actual = record["actual"]
        lines.append(
            f"| {record['id']} | {record['category']} | {actual['query_type']} "
            f"| {','.join(actual['detected_namespaces']) or '-'} "
            f"| {','.join(actual['preferred_kinds']) or '-'} | {_preview(record['text'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _write_text(path: str | Path, content: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Memory V2 gold-set evaluation")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--output-md", default="")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--no-fail", action="store_true", help="always exit 0 even if cases fail")
    args = parser.parse_args()

    result = evaluate_cases(load_cases(args.cases))
    output_md = args.output_md
    output_json = args.output_json
    if not output_md and not output_json:
        output_md = str(DEFAULT_REPORT_DIR / "MEMORY_B23E_GOLD_EVAL.md")
        output_json = str(DEFAULT_REPORT_DIR / "MEMORY_B23E_GOLD_EVAL.json")
    if output_json:
        _write_text(output_json, json.dumps(result, ensure_ascii=False, indent=2))
        print(output_json)
    if output_md:
        _write_text(output_md, build_markdown(result))
        print(output_md)
    if result["metrics"]["failed"] and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

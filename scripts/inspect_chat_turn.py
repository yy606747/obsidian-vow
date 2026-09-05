#!/usr/bin/env python3
"""只读查看一轮聊天的工程诊断，不输出聊天正文或模型请求快照。"""

import argparse
import json
from pathlib import Path
import sqlite3


def inspect_turn(db_path: Path, turn_id: str | None = None) -> dict:
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if not turn_id:
            latest = db.execute(
                "SELECT turn_id FROM tool_invocation_events WHERE source_chain='main' "
                "AND stage IN ('turn','model_request','diagnostic') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                return {"结果": "没有聊天诊断记录"}
            turn_id = latest["turn_id"]
        rows = db.execute(
            "SELECT stage,conv_id,assistant_message_id,tool_name,outcome,turn_outcome,metadata_json "
            "FROM tool_invocation_events WHERE turn_id=? ORDER BY created_at", (turn_id,),
        ).fetchall()
    if not rows:
        return {"轮次": turn_id, "结果": "未找到记录"}
    result = {"轮次": turn_id, "会话": rows[0]["conv_id"], "消息": None, "结果": "尚未结束",
              "诊断": None, "图片保留": None, "工具": [], "后台": []}
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if row["assistant_message_id"]:
            result["消息"] = row["assistant_message_id"]
        if meta.get("diagnostics"):
            result["诊断"] = meta["diagnostics"]
        if meta.get("image_history") is not None:
            result["图片保留"] = meta["image_history"]
        if row["stage"] == "turn":
            result["结果"] = row["turn_outcome"]
        if row["stage"] == "execution":
            result["工具"].append({"名称": row["tool_name"], "结果": row["outcome"]})
        if row["stage"] == "diagnostic":
            if meta.get("phase") == "prepare":
                result["结果"] = row["outcome"]
                result["诊断"] = {key: meta.get(key) for key in ("timings", "usage", "error_type")}
            elif meta.get("phase") == "background":
                result["后台"].append({"任务": meta.get("task_name"), "结果": row["outcome"],
                                       "耗时毫秒": meta.get("elapsed_ms"), "异常类型": meta.get("error_type")})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="明确指定数据库文件")
    parser.add_argument("--turn-id", help="省略时读取最新聊天轮次")
    args = parser.parse_args()
    try:
        result = inspect_turn(args.db, args.turn_id)
    except sqlite3.Error as exc:
        parser.exit(1, f"无法读取诊断记录：{type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

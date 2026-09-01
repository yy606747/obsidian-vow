"""
历史对话导入工具
用法：python import_history.py 对话文件.html

功能：
1. 解析 Chatbox AI 导出的 HTML 聊天记录
2. 将消息写入 aion-chat 数据库
3. 重置摘要锚点，使 manual_digest 能处理这些历史消息
4. 导入完成后，去网页点「手动总结记忆」即可生成向量记忆
"""

from __future__ import annotations
import sys
import re
import time
import json
import uuid
import sqlite3
from pathlib import Path

# ── 路径配置 ────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "chat.db"
DIGEST_ANCHOR_PATH = DATA_DIR / "digest_anchor.json"


def parse_html(html_path: str) -> list[tuple[str, str]]:
    """解析 Chatbox HTML，返回 [(role, content), ...] 列表"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = content.split('<div class="mb-4">')
    messages = []

    for block in blocks[1:]:
        if 'text-green-500' in block and '>USER: <' in block:
            role = "user"
        elif 'text-blue-500' in block and '>ASSISTANT: <' in block:
            role = "assistant"
        else:
            continue

        m = re.search(r'<div class="break-words [^"]*">(.*?)</div>\s*</div>', block, re.DOTALL)
        if not m:
            continue

        text_html = m.group(1)
        # 去掉 HTML 标签
        text = re.sub(r'<[^>]+>', '', text_html)
        # 处理 HTML 实体
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
                   .replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

        if text:
            messages.append((role, text))

    return messages


def import_to_db(messages: list[tuple[str, str]], title: str = None):
    """将解析好的消息插入数据库"""
    if not DB_PATH.exists():
        print(f"[错误] 数据库不存在: {DB_PATH}")
        print("  请先启动一次 aion-chat 服务，让它初始化数据库后再运行此脚本")
        sys.exit(1)

    if not messages:
        print("[警告] 没有找到任何消息，请检查 HTML 文件格式")
        return

    # 给历史消息分配时间戳：从30天前开始，每条消息间隔约1分钟
    # （实际上只影响显示排序，不影响记忆内容）
    base_ts = time.time() - 30 * 24 * 3600  # 30天前
    interval = 60  # 每条消息间隔60秒

    conv_id = str(uuid.uuid4())
    conv_title = title or Path(html_path).stem
    conv_created = base_ts
    conv_updated = base_ts + len(messages) * interval

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # 插入对话
        conn.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, conv_title, "imported", conv_created, conv_updated)
        )
        print(f"[✓] 创建对话: {conv_title} (id={conv_id[:8]}...)")

        # 插入消息
        for i, (role, text) in enumerate(messages):
            msg_id = str(uuid.uuid4())
            ts = base_ts + i * interval
            conn.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at) VALUES (?,?,?,?,?)",
                (msg_id, conv_id, role, text, ts)
            )

        conn.commit()
        print(f"[✓] 插入 {len(messages)} 条消息（时间从 {time.strftime('%Y-%m-%d', time.localtime(base_ts))} 开始）")

        # 重置摘要锚点到这些历史消息之前
        # 这样 manual_digest 就能检测到并处理它们
        old_anchor = 0.0
        if DIGEST_ANCHOR_PATH.exists():
            try:
                data = json.loads(DIGEST_ANCHOR_PATH.read_text(encoding="utf-8"))
                old_anchor = data.get("anchor_ts", 0.0)
            except Exception:
                pass

        # 只有当历史消息的起始时间早于当前锚点时，才需要重置锚点
        if base_ts < old_anchor:
            new_anchor = base_ts - 1
            DIGEST_ANCHOR_PATH.write_text(
                json.dumps({"anchor_ts": new_anchor}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            print(f"[✓] 已将摘要锚点从 {old_anchor:.0f} 重置到 {new_anchor:.0f}")
            print(f"    （这样「手动总结」才能处理这批历史消息）")
        else:
            print(f"[i] 锚点 ({old_anchor:.0f}) 早于历史消息，无需重置，直接触发「手动总结」即可")

    except Exception as e:
        conn.rollback()
        print(f"[错误] 写入数据库失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python import_history.py <聊天记录.html>")
        print("示例：python import_history.py 开学压力倾诉_2.html")
        sys.exit(1)

    html_path = sys.argv[1]
    if not Path(html_path).exists():
        # 尝试从项目根目录找
        alt = Path(__file__).parent.parent / html_path
        if alt.exists():
            html_path = str(alt)
        else:
            print(f"[错误] 文件不存在: {html_path}")
            sys.exit(1)

    print(f"[→] 解析文件: {html_path}")
    messages = parse_html(html_path)
    print(f"[✓] 解析到 {len(messages)} 条消息 ({sum(1 for r,_ in messages if r=='user')} 用户 / {sum(1 for r,_ in messages if r=='assistant')} AI)")

    import_to_db(messages)

    print()
    print("=" * 50)
    print("✅ 导入完成！下一步：")
    print()
    print("  1. 确保 aion-chat 服务正在运行")
    print("  2. 打开网页 → 顶部菜单 → 「记忆管理」→ 点击「手动总结记忆」")
    print("     （或者直接调用 API: POST http://127.0.0.1:18080/api/memory/digest）")
    print()
    print("  总结完成后，AI 就能在对话中自动回忆这段历史了")
    print("=" * 50)

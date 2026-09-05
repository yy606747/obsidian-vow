"""
文件管理路由：上传、聊天记录文件 CRUD、导出
"""

import json, time, re, mimetypes

from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel

from config import (
    UPLOADS_DIR, CHATS_DIR, ALLOWED_TYPES,
    load_worldbook, load_file_index, save_file_index, sanitize_filename,
)
from database import get_db
from ws import manager

router = APIRouter()


# ── 导出对话到 .md 文件 ───────────────────────────
async def export_conversation(conv_id: str):
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        if not conv:
            return
        cur = await db.execute("SELECT * FROM messages WHERE conv_id=? ORDER BY created_at", (conv_id,))
        msgs = await cur.fetchall()

    lines = [f"# {conv['title']}", f"> 模型: {conv['model']}", f"> ID: {conv_id}", ""]
    wb = load_worldbook()
    u_name = wb.get("user_name", "你")
    a_name = wb.get("ai_name", "AI")
    for m in msgs:
        if m["role"] in ("cam_user", "cam_log", "cam_trigger"):
            continue
        role = u_name if m["role"] == "user" else a_name
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["created_at"]))
        lines.append(f"### {role} · {ts}")
        lines.append("")
        lines.append(m["content"])
        lines.append("")

    idx = load_file_index()
    old_filename = idx.get(conv_id)
    if old_filename:
        old_path = CHATS_DIR / old_filename
        if old_path.exists():
            old_path.unlink()

    safe_title = sanitize_filename(conv['title']) or conv_id
    filename = f"{safe_title}.md"
    filepath = CHATS_DIR / filename

    if filepath.exists() and idx.get(conv_id) != filename:
        counter = 2
        while filepath.exists():
            filename = f"{safe_title} ({counter}).md"
            filepath = CHATS_DIR / filename
            counter += 1

    filepath.write_text("\n".join(lines), encoding='utf-8')
    idx[conv_id] = filename
    save_file_index(idx)


def delete_exported_file(conv_id: str):
    idx = load_file_index()
    filename = idx.pop(conv_id, None)
    if filename:
        filepath = CHATS_DIR / filename
        if filepath.exists():
            filepath.unlink()
        save_file_index(idx)


def parse_chat_file(content: str):
    lines = content.split('\n')
    title = model = conv_id = ""
    messages = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('# '):
            title = line[2:].strip()
        elif line.startswith('> 模型: '):
            model = line[len('> 模型: '):].strip()
        elif line.startswith('> ID: '):
            conv_id = line[len('> ID: '):].strip()
        elif line.startswith('### '):
            break
        i += 1

    wb = load_worldbook()
    a_name = wb.get("ai_name", "AI")
    msg_re = re.compile(r'^### (.+?) · (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$')
    cur_role = cur_time = None
    cur_lines = []
    while i < len(lines):
        m = msg_re.match(lines[i])
        if m:
            if cur_role is not None:
                messages.append({"role": "user" if cur_role != a_name else "assistant",
                                 "content": '\n'.join(cur_lines).strip(),
                                 "created_at": time.mktime(time.strptime(cur_time, "%Y-%m-%d %H:%M:%S"))})
            cur_role, cur_time, cur_lines = m.group(1), m.group(2), []
        else:
            cur_lines.append(lines[i])
        i += 1
    if cur_role is not None:
        messages.append({"role": "user" if cur_role != a_name else "assistant",
                         "content": '\n'.join(cur_lines).strip(),
                         "created_at": time.mktime(time.strptime(cur_time, "%Y-%m-%d %H:%M:%S"))})
    return {"title": title, "model": model, "conv_id": conv_id, "messages": messages}


# ── 上传 ──────────────────────────────────────────
@router.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_TYPES:
        return {"error": f"不支持的文件类型: {file.content_type}"}
    ext = mimetypes.guess_extension(file.content_type) or ".bin"
    if ext == '.jpe': ext = '.jpg'
    fname = f"{int(time.time()*1000)}{ext}"
    fpath = UPLOADS_DIR / fname
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        return {"error": "文件太大，最大 20MB"}
    fpath.write_bytes(content)
    url = f"/uploads/{fname}"
    return {"url": url, "type": file.content_type, "name": file.filename}


# ── 文件列表 / 读取 / 保存 ────────────────────────
class FileContent(BaseModel):
    content: str

@router.get("/api/files")
async def list_files():
    idx = load_file_index()
    result = []
    for cid, fname in idx.items():
        fp = CHATS_DIR / fname
        if fp.exists():
            result.append({"conv_id": cid, "filename": fname, "size": fp.stat().st_size})
    return result

@router.get("/api/files/{conv_id}")
async def read_chat_file(conv_id: str):
    idx = load_file_index()
    fname = idx.get(conv_id)
    if not fname:
        return {"error": "文件不存在"}
    fp = CHATS_DIR / fname
    if not fp.exists():
        return {"error": "文件不存在"}
    return {"content": fp.read_text(encoding='utf-8'), "filename": fname}

@router.put("/api/files/{conv_id}")
async def save_chat_file(conv_id: str, body: FileContent):
    parsed = parse_chat_file(body.content)
    async with get_db() as db:
        if parsed["title"]:
            await db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                             (parsed["title"], time.time(), conv_id))
        if parsed["model"]:
            await db.execute("UPDATE conversations SET model=?, updated_at=? WHERE id=?",
                             (parsed["model"], time.time(), conv_id))
        # 批量撤约（誓约设计 §4.5）：覆盖导入会清掉原消息，来源誓约随之退役；
        # 须在 DELETE 之前（子查询依赖 messages 行）且同一事务提交
        from app.vows.service import vow_service
        revoked = await vow_service.revoke_for_origin_conversation_in_tx(db, conv_id=conv_id)
        await db.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
        for i, m in enumerate(parsed["messages"]):
            msg_id = f"msg_{int(m['created_at']*1000)}_{i}"
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at) VALUES (?,?,?,?,?)",
                (msg_id, conv_id, m["role"], m["content"], m["created_at"])
            )
        await db.commit()
    if revoked:
        await manager.broadcast({"type": "vow_changed", "data": {"action": "origin_deleted"}})

    idx = load_file_index()
    old_fname = idx.get(conv_id)
    if old_fname:
        old_path = CHATS_DIR / old_fname
        if old_path.exists():
            old_path.unlink()
    safe_title = sanitize_filename(parsed["title"]) if parsed["title"] else conv_id
    fname = f"{safe_title}.md"
    fp = CHATS_DIR / fname
    fp.write_text(body.content, encoding='utf-8')
    idx[conv_id] = fname
    save_file_index(idx)

    await manager.broadcast({"type": "conv_updated", "data": {"id": conv_id, "title": parsed["title"], "model": parsed["model"]}})
    await manager.broadcast({"type": "file_synced", "data": {"conv_id": conv_id}})
    return {"ok": True}

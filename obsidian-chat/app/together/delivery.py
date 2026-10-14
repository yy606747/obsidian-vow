"""Durable final replies from an ordinary initiative already in flight.

Only autonomous send paths opt in. Scheduled reminders and owner chat retain
their existing delivery semantics. No model is called to resume delivery.
"""

from dataclasses import asdict
import json
import time

from .coexist import active_in_tx


async def defer_in_tx(db, messages, *, feedback=None, delivery=None, look_at_me=None, now=None):
    now = time.time() if now is None else now
    if not await active_in_tx(db, now=now):
        return False
    payload = {"messages": messages, "feedback": feedback or {},
               "delivery": asdict(delivery) if delivery else None, "look_at_me": look_at_me or {}}
    await db.execute("INSERT OR IGNORE INTO together_deliveries(id,payload,created_at) VALUES (?,?,?)",
                     (messages[-1]["id"], json.dumps(payload, ensure_ascii=False), now))
    return True


async def drain(*, db_factory=None, broadcast=None, now=None, enabled=None, lease_seconds=None):
    from database import get_db
    from app.stickers.autonomous import finalize_in_tx
    from app.stickers.chat import StickerDelivery
    from ws import manager
    now = time.time() if now is None else now
    delivered = []
    async with (db_factory or get_db)() as db:
        await db.execute("BEGIN IMMEDIATE")
        if await active_in_tx(db, now=now, enabled=enabled, lease_seconds=lease_seconds):
            await db.commit()
            return 0
        rows = await (await db.execute("SELECT id,payload FROM together_deliveries WHERE delivered_at IS NULL ORDER BY created_at LIMIT 20")).fetchall()
        for index, (key, encoded) in enumerate(rows):
            payload = json.loads(encoded)
            messages = payload["messages"]
            for offset, original in enumerate(messages):
                message = {**original, "created_at": now+(index*2+offset)*.001}
                cursor = await db.execute("INSERT OR IGNORE INTO messages(id,conv_id,role,content,created_at,attachments) VALUES (?,?,?,?,?,?)",
                    (message["id"], message["conv_id"], message["role"], message["content"], message["created_at"],
                     json.dumps(message.get("attachments", []), ensure_ascii=False)))
                if cursor.rowcount:
                    delivered.append(message)
                    await db.execute("UPDATE conversations SET updated_at=MAX(updated_at,?) WHERE id=?", (message["created_at"], message["conv_id"]))
            await finalize_in_tx(db, feedback=payload["feedback"], message_id=messages[-1]["id"],
                                 delivery=StickerDelivery(**payload["delivery"]) if payload["delivery"] else None)
            if payload.get("look_at_me"):
                from app.look_at_me.repository import repository
                await repository.finalize_in_tx(db, payload["look_at_me"], messages[-1]["id"])
            # Keep only an idempotency tombstone once the real messages exist.
            await db.execute("UPDATE together_deliveries SET delivered_at=?,payload='{}' WHERE id=?", (now, key))
        await db.commit()
    for message in delivered:
        await (broadcast or manager.broadcast)({"type": "msg_created", "data": message})
    if delivered:
        from routes.files import export_conversation
        for conv_id in {m["conv_id"] for m in delivered}:
            try:
                await export_conversation(conv_id)
            except Exception:
                pass
    return len(delivered)

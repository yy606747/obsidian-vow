import asyncio
import re

from .prompts import note_messages


class NotesWriter:
    def __init__(self, repository, models, identity_loader):
        self.repository, self.models, self.identity_loader = repository, models, identity_loader
        self.lock = asyncio.Lock()

    async def update(self, session_id):
        # One notebook, one writer. Read inputs again after acquiring the lock.
        async with self.lock:
            while True:
                feedback = await self.repository.feedback(session_id)
                if not feedback or feedback["note_status"] == "skipped" or feedback["processed_version"] >= feedback["version"]:
                    return
                version = feedback["version"]
                try:
                    session = await self.repository.session(session_id)
                    card = await self.repository.card(session["card_id"])
                    old = await self.repository.notes()
                    identity = await self.identity_loader()
                    messages = note_messages(user_name=identity.user_name, ai_name=identity.ai_name,
                        ai_persona=identity.ai_persona, card=card["raw_text"], rating=feedback["rating"],
                        reason=feedback["reason"], notes=old["content"])
                    raw = (await self.models.once(session["model_key"], messages, scope="notes")).strip()
                    if raw == "<keep/>":
                        content = None
                    else:
                        match = re.fullmatch(r"<notes>([\s\S]*)</notes>", raw)
                        if not match or len(match[1].strip()) > 2000 or "<notes>" in match[1] or "</notes>" in match[1]:
                            raise ValueError("scene_notes_invalid")
                        content = match[1].strip()
                    accepted = await self.repository.apply_notes(session_id, feedback_version=version,
                                                                notes_version=old["version"], content=content)
                    if accepted:
                        return
                    # Feedback changed while the model was running: discard
                    # the old result and process the latest revision.
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self.repository.note_failed(session_id, version, type(exc).__name__)
                    return

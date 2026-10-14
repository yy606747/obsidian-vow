import re

from .prompts import card_messages


def parse_cards(text, candidate_ids):
    chunks = re.split(r"(?m)(?=^##\s*卡\s*\d+\s*[:：])", text.strip())
    cards = []
    for chunk in chunks:
        if not re.match(r"^##\s*卡\s*\d+\s*[:：]", chunk):
            continue
        declarations = re.findall(r"<devices>\s*([^<>]+?)\s*</devices>", chunk)
        if len(declarations) != 1 or chunk.count("<devices") != 1:
            continue
        ids = [item.strip() for item in declarations[0].split(",")]
        if not ids or any(not item for item in ids) or len(ids) != len(set(ids)) or not set(ids).issubset(candidate_ids):
            continue
        cards.append({"raw_text": chunk.strip(), "required_devices": ids})
    return cards


class CardGenerator:
    def __init__(self, repository, models, identity_loader):
        self.repository, self.models, self.identity_loader = repository, models, identity_loader

    async def generate(self, *, conv_id, model_key, devices, source, count=1):
        if not devices:
            raise ValueError("scene_devices_empty")
        identity = await self.identity_loader()
        notes = await self.repository.notes()
        messages = card_messages(**vars(identity), notes=notes["content"], devices=devices, count=count)
        text = await self.models.once(model_key, messages, scope="cards")
        cards = parse_cards(text, {d["id"] for d in devices})[:count]
        if not cards:
            raise ValueError("scene_card_devices_invalid")
        return [await self.repository.add_card(**card, source=source, conv_id=conv_id, model_key=model_key) for card in cards]

"""An optional outlet attached to an existing autonomous Core invocation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from .protocol import extract_intent, IntentStreamFilter
from .prompts import intention_prompt
from .tools import capabilities


@dataclass
class WorkEntry:
    origin: dict
    snapshot: dict
    intent: str = ''
    raw_outputs: list[str] = field(default_factory=list)

    def extract(self, raw):
        visible, intent = extract_intent(raw)
        if intent.strip():
            self.intent = intent
            if not self.raw_outputs or self.raw_outputs[-1] != raw:
                self.raw_outputs.append(raw)
        return visible

    async def commit(self):
        if not self.intent.strip():
            return None
        from .service import heart_work_service
        return await heart_work_service.enqueue_from_turn(self.origin, self.intent,
            {**self.snapshot, 'origin_outputs': self.raw_outputs})

    def metadata(self):
        return {'origin': self.origin, 'snapshot': self.snapshot}


def attach(messages, *, source, turn_id, conv_id, model_key, user_name, ai_name,
           context_messages=(), identity_text='', msg_id=None):
    """Only explicitly supplied identity/history/facts survive into creation.

    The caller passes context before adding its stage-specific tool instructions.
    Attachments are intentionally not retained past their originating turn.
    """
    history = [{'role': m.get('role', 'user'), 'content': str(m.get('content') or '')}
               for m in context_messages]
    snapshot = {'user_name': user_name, 'ai_name': ai_name,
                'messages': [{'role': 'user', 'content': f'你是{ai_name}。\n{identity_text}'}] + deepcopy(history)}
    messages.insert(max(0, len(messages) - 1), {'role': 'user', 'content': intention_prompt(
        ai_name=ai_name, user_name=user_name, capabilities=capabilities())})
    return WorkEntry({'source': source, 'turn_id': turn_id, 'conv_id': conv_id,
                      'model_key': model_key, 'msg_id': msg_id}, snapshot)


def attach_followup(messages, origin_metadata):
    if not origin_metadata:
        return None
    entry = WorkEntry(deepcopy(origin_metadata['origin']), deepcopy(origin_metadata['snapshot']))
    # Results of the current follow-up are factual input supplied by its caller.
    entry.snapshot['messages'].extend({'role': m['role'], 'content': str(m.get('content') or '')} for m in messages[-1:])
    messages.insert(max(0, len(messages) - 1), {'role': 'user', 'content': intention_prompt(
        ai_name=entry.snapshot['ai_name'], user_name=entry.snapshot['user_name'], capabilities=capabilities())})
    return entry

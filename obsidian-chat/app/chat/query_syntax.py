"""Small natural-language query markers; their payload is always inert data."""
from dataclasses import dataclass

from .control_syntax import iter_value_control_markers
from app.heart_works.protocol import executable


@dataclass(frozen=True)
class QueryIntent:
    kind: str
    text: str


def extract_query(text):
    markers = list(iter_value_control_markers(text, names={'回忆', '联网'}))
    intent = None
    # One lookup per turn, even when several complete markers were emitted.
    for marker in markers:
        query = marker.payload.strip()
        if intent is None and marker.complete and query and len(query) <= 600 and executable(text, marker.start):
            intent = QueryIntent('memory' if marker.name == '回忆' else 'web', query)
    for marker in reversed(markers):
        text = text[:marker.start] + text[marker.end:]
    return text, intent


def query_ability(*, user_name, ai_name, memory=False, web=False):
    if not memory and not web:
        return ''
    lines = [f'【{ai_name}的当轮查询】回答{user_name}时，若确实缺少信息，可以直接写一个隐藏标签，里面自由描述要查什么：']
    if memory:
        lines.append('[回忆: 想查的具体旧事、原话或心语作品]')
    if web:
        lines.append('[联网: 需要核实的外部信息]')
    lines.append(f'已知的部分可以先说，也可以只发标签。资料会在本轮返回，你随后继续回答{user_name}，不用等下一条消息。'
                 '每轮最多查询一次；正文不展示标签，不在结果返回前编造答案。上下文已经够用就直接回答。')
    return '\n'.join(lines)

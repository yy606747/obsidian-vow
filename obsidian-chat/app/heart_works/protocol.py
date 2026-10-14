"""Natural text protocol. Positions belong to the document, never to chat."""
from __future__ import annotations

import hashlib
import re

OPEN = '[心语创作]'
CLOSE = '[/心语创作]'
_TOOLS = re.compile(r'\[(IMAGE|MUSIC_SEARCH|MUSIC):|\[WEB_SEARCH_INTENT\]')
_RESERVED = re.compile(r'\[(?:/?心语创作|/?WEB_SEARCH_INTENT|IMAGE:|MUSIC(?:_SEARCH)?:|HEART:|TOY:|REMEMBER:|VOW:|UPDATE_MODEL:|/?WORKING_MODEL_REQUEST|SELF_WAKE|PRESENCE_|SCREEN_CHECK:|MOBILE_SCREEN_CHECK:|POI_SEARCH:|VIEW_IMAGE:|RING:|ALARM:|MONITOR:|SCHEDULE_|CAM_CHECK|查看动态:|做表情:|发图:|偷图\]|封面|正文|记下|完成|待续|放弃:)')


def literal_ranges(text: str) -> list[tuple[int, int]]:
    """Markdown code and quotation are content, not executable instructions."""
    ranges = []
    offset, fence, fence_start = 0, None, 0
    for line in text.splitlines(keepends=True):
        match = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence):
                ranges.append((fence_start, offset + len(line)))
                fence = None
        elif match:
            fence, fence_start = match[1], offset
        elif re.match(r'^\s*>', line):
            ranges.append((offset, offset + len(line)))
        else:
            ranges.extend((offset + m.start(), offset + m.end()) for m in re.finditer(r'(`+).*?\1', line))
        offset += len(line)
    if fence:
        ranges.append((fence_start, len(text)))
    return ranges


def executable(text: str, start: int) -> bool:
    return not any(a <= start < b for a, b in literal_ranges(text))


def extract_intent(raw: str) -> tuple[str, str]:
    """Keep intent byte-for-byte; hide even an interrupted private block."""
    from app.vows.service import find_vow_markers
    from app.chat.control_syntax import iter_value_control_markers
    vows, unfinished = find_vow_markers(raw)
    private = [(a, b) for a, b, _ in vows]
    private.extend((m.start, m.end) for m in iter_value_control_markers(raw, names={'发图', '做表情', '陪我'}))
    if unfinished is not None:
        private.append((unfinished, len(raw)))
    for opening, closing in (('[WORKING_MODEL_REQUEST]', '[/WORKING_MODEL_REQUEST]'),
                             ('[WEB_SEARCH_INTENT]', '[/WEB_SEARCH_INTENT]'),
                             ('[RECALL_INTENT]', '[/RECALL_INTENT]'),
                             ('<think>', '</think>'), ('<analysis>', '</analysis>'),
                             ('<remember>', '</remember>'), ('<understand>', '</understand>')):
        start = 0
        while (start := raw.find(opening, start)) >= 0:
            end = raw.find(closing, start + len(opening))
            end = len(raw) if end < 0 else end + len(closing)
            private.append((start, end))
            start = end
    def eligible(text, start):
        return executable(text, start) and not any(a <= start < b for a, b in private)
    visible, intents, pos = [], [], 0
    for match in re.finditer(re.escape(OPEN), raw):
        if match.start() < pos or not eligible(raw, match.start()):
            continue
        visible.append(raw[pos:match.start()])
        end = raw.find(CLOSE, match.end())
        if end < 0:
            pos = len(raw)
            break
        intents.append(raw[match.end():end])
        pos = end + len(CLOSE)
    visible.append(raw[pos:])
    # One work per origin. Multiple blocks are preserved together as one intention.
    cleaned = ''.join(visible)
    # Orphan closing markers are harmless; leave nested private content to its
    # owning parser so extraction cannot turn it into a different instruction.
    if CLOSE in cleaned and not private:
        for match in reversed(list(re.finditer(re.escape(CLOSE), cleaned))):
            if executable(cleaned, match.start()):
                cleaned = cleaned[:match.start()] + cleaned[match.end():]
    return cleaned, '\n'.join(intents)


class IntentStreamFilter:
    """Hide split markers without delaying ordinary text streaming."""
    def __init__(self):
        self.raw = ''
        self.emitted = 0

    def feed(self, chunk: str) -> str:
        self.raw += chunk
        prefix, _ = extract_intent(self.raw)
        for marker in (OPEN, CLOSE):
            for length in range(len(marker) - 1, 0, -1):
                if prefix.endswith(marker[:length]) and executable(prefix, len(prefix) - length):
                    prefix = prefix[:-length]
                    break
        delta = prefix[self.emitted:]
        self.emitted = len(prefix)
        return delta

    def flush(self) -> str:
        visible, _ = extract_intent(self.raw)
        # Never show a truncated private opening delimiter.
        for marker in (OPEN, CLOSE):
            for length in range(len(marker) - 1, 0, -1):
                if visible.endswith(marker[:length]) and executable(visible, len(visible) - length):
                    visible = visible[:-length]
                    break
        result = visible[self.emitted:]
        self.emitted = len(visible)
        return result


def task_key(tool: str, query: str) -> str:
    return hashlib.sha256((tool + '\0' + query.strip()).encode()).hexdigest()


def blocks(text: str, section: str) -> list[dict]:
    result, pos = [], 0
    for match in _TOOLS.finditer(text):
        if match.start() < pos or not executable(text, match.start()):
            continue
        tool = match[1] or 'WEB_SEARCH_INTENT'
        if match[1]:
            end, depth = match.end(), 1
            while end < len(text) and depth:
                depth += (text[end] == '[') - (text[end] == ']')
                end += 1
            if depth:
                raise ValueError('unfinished_tool_instruction')
            query = text[match.end():end - 1].strip()
        else:
            closing = text.find('[/WEB_SEARCH_INTENT]', match.end())
            if closing < 0:
                raise ValueError('unfinished_tool_instruction')
            query, end = text[match.end():closing].strip(), closing + len('[/WEB_SEARCH_INTENT]')
        if text[pos:match.start()].strip():
            result.append({'type': 'text', 'text': text[pos:match.start()]})
        if not query or (section == 'cover' and tool != 'IMAGE'):
            raise ValueError('invalid_tool_position')
        result.append({'type': 'tool_slot', 'tool': tool, 'query': query,
                       'marker': text[match.start():end], 'request_key': task_key(tool, query),
                       'section': section, 'ordinal': len(result)})
        pos = end
    if text[pos:].strip():
        result.append({'type': 'text', 'text': text[pos:]})
    for block in result:
        if block['type'] == 'text':
            for match in _RESERVED.finditer(block['text']):
                if executable(block['text'], match.start()):
                    raise ValueError('unfinished_or_unsupported_instruction')
    return result


def parse_work(raw: str) -> dict:
    if re.fullmatch(r'\s*\[放弃:([^\]]*)\]\s*', raw):
        return {'status': 'abandoned', 'reason': raw.strip()[4:-1]}
    markers = [m for m in re.finditer(r'(?m)^\[(封面|正文|记下|完成|待续)\][ \t]*\r?$', raw)
               if executable(raw, m.start())]
    names = [m[1] for m in markers]
    if names not in (['封面', '正文', '完成'], ['封面', '正文', '待续'],
                     ['封面', '正文', '记下', '完成'], ['封面', '正文', '记下', '待续']):
        raise ValueError('incomplete_work_sections')
    if raw[:markers[0].start()].strip() or raw[markers[-1].end():].strip():
        raise ValueError('text_outside_work')
    sections = {m[1]: raw[m.end():markers[i + 1].start()].strip()
                for i, m in enumerate(markers[:-1])}
    cover, body = blocks(sections['封面'], 'cover'), blocks(sections['正文'], 'body')
    if not cover or not body:
        raise ValueError('empty_cover_or_body')
    return {'status': 'complete' if names[-1] == '完成' else 'continue',
            'cover': cover, 'body': body, 'event_note_raw': sections.get('记下', '')}


def event_text(*, ai_name: str, user_name: str, note: str, body: list[dict], cover: list[dict], published_at: float) -> str:
    """Only actual final content/assets contribute to the fallback event."""
    from datetime import datetime
    facts = []
    for block in [*cover, *body]:
        if block['type'] == 'image':
            facts.append('图片（创作描述）：' + block.get('description', '')[:160])
        elif block['type'] == 'music':
            facts.append('歌曲：' + block.get('title', '') + ' — ' + block.get('artist', ''))
    for block in body:
        if block['type'] == 'text':
            for match in re.finditer(r'(?<!!)\[([^\]\n]+)\]\((https?://[^\s)]+)\)', block['text']):
                if executable(block['text'], match.start()):
                    facts.append('网页：' + match[1][:120] + '（' + match[2] + '）')
    if not note.strip():
        excerpt = ' '.join(b.get('text', '') for b in body if b['type'] == 'text').strip()
        note = '正文节选：' + excerpt[:400] if excerpt else '留下了一份素材作品。'
    return (f'{ai_name}于{datetime.fromtimestamp(published_at):%Y-%m-%d %H:%M}给{user_name}留下了一份已保存的心语。\n'
            + note.strip()[:1800] + ('\n实际采用的素材：' + '；'.join(dict.fromkeys(facts)) if facts else ''))

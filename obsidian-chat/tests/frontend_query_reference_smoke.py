"""Mobile composer, work navigation and query progress against actual scripts."""
import json
import mimetypes
from pathlib import Path
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WORK_ID = 'hw_' + 'a' * 32
work = {'id': WORK_ID, 'cover': [{'type': 'text', 'text': '一封雨夜的信'}],
        'body': [{'type': 'text', 'text': '这是实际正文。'}], 'published_at': 100}
sent, errors, console_errors = [], [], []


def route_request(route):
    path = urlparse(route.request.url).path
    if path in ('/chat', '/heart-whispers'):
        route.fulfill(path=ROOT / ('static/chat.html' if path == '/chat' else 'static/heart-whispers.html'),
                      content_type='text/html; charset=utf-8')
    elif path.startswith('/static/'):
        asset = ROOT / path.lstrip('/')
        if asset.is_file():
            route.fulfill(path=asset, content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream')
        else:
            route.fulfill(status=404)
    elif path.endswith('/send'):
        sent.append(route.request.post_data_json)
        events = [{'type': 'start', 'id': 'reply', 'user_message_id': 'user', 'conv_id': 'c1'},
                  {'type': 'chunk', 'content': '接着聊这份信。'}]
        route.fulfill(content_type='text/event-stream', body=''.join('data: '+json.dumps(event)+'\n\n' for event in events))
    elif path.startswith('/api/'):
        data = {}
        if path == '/api/worldbook': data = {'ai_name': '阿澈', 'user_name': '小栀'}
        elif path == '/api/models': data = [{'key': 'fixture', 'audio_input': False}]
        elif path == '/api/chat/daily':
            data = {'enabled': False, 'count_initialized': True, 'inherit_count': 30, 'revision': 0,
                    'timezone': 'Asia/Shanghai', 'next_rollover_at': time.time() + 86400}
        elif path == '/api/conversations': data = [{'id': 'c1', 'title': '聊天', 'model': 'fixture'}]
        elif path == '/api/conversations/c1/messages': data = []
        elif path == '/api/avatar/info': data = {'ai': 0, 'user': 0}
        elif path == '/api/tts/voices': data = {'voices': []}
        elif path == '/api/heart-whispers': data = {'items': [{'kind': 'work', 'created_at': 100, **work}], 'total': 1, 'page': 1, 'page_size': 20}
        elif path == f'/api/heart-whispers/works/{WORK_ID}': data = work
        elif path == '/api/schedule/missed-recent': data = {'count': 0, 'items': []}
        elif path.endswith('/stream-status'): data = {'streaming': False}
        route.fulfill(json=data)
    else:
        route.fulfill(status=204)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True,
                                  has_touch=True, service_workers='block')
    context.add_init_script("localStorage.setItem('obsidian_tts_enabled', 'false')")
    context.route('**/*', route_request)
    context.route_web_socket('**/ws', lambda socket: socket.on_message(
        lambda data: socket.send('{"type":"pong"}') if json.loads(data).get('type') == 'ping' else None))
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: console_errors.append(message.text) if message.type in ('error', 'warning') else None)
    page.goto('http://continuity.test/chat')
    try:
        page.wait_for_function('chatInitialized && currentConvId === "c1"', timeout=10000)
    except Exception:
        print({'page_errors': errors, 'console_errors': console_errors[-10:]}, flush=True)
        raise
    page.locator('#input').fill('已经写好的草稿')
    page.evaluate(f"openSubPage('/heart-whispers?work={WORK_ID}')")
    reader = page.frame_locator('#subPageFrame')
    reader.get_by_text('这是实际正文。').wait_for()
    reader.get_by_role('button', name='聊聊这份').click()
    page.locator('#previewArea .heart-work-reference').wait_for()
    assert page.locator('#input').input_value() == '已经写好的草稿'
    assert sent == []
    assert page.locator('#previewArea .heart-work-reference').inner_text() == '心语 · 聊聊这份\n一封雨夜的信'
    page.screenshot(path='/tmp/continuity-heart-reference-mobile.png', full_page=True)
    page.reload()
    page.wait_for_function('chatInitialized && pendingAttachments.length === 1')
    assert page.locator('#input').input_value() == '已经写好的草稿'
    page.evaluate('send()')
    page.wait_for_function('!sending && currentMessages.some(m => m.id === "reply")')
    assert len(sent) == 1 and sent[0]['attachments'] == [{'type': 'heart_work', 'id': WORK_ID, 'name': '一封雨夜的信'}]
    page.evaluate('''async () => {
      window.queryState = createSendStreamState();
      await handleSendStreamEvent({type:'start',id:'query-test',conv_id:'c1'}, queryState);
      await handleSendStreamEvent({type:'chunk',content:'已经说出的第一段。'}, queryState);
      await handleSendStreamEvent({type:'query',kind:'memory',status:'searching'}, queryState);
    }''')
    assert page.locator('#m_query-test').inner_text().count('已经说出的第一段。') == 1
    assert page.locator('#query_query-test').inner_text() == '正在翻回忆…'
    page.screenshot(path='/tmp/continuity-query-mobile.png', full_page=True)
    page.evaluate("handleSendStreamEvent({type:'chunk',content:'\\n\\n查到了，继续。'}, queryState)")
    assert page.locator('#query_query-test').count() == 0
    assert '已经说出的第一段。' in page.locator('#m_query-test').inner_text()
    page.wait_for_function("document.getElementById('m_query-test').innerText.includes('查到了，继续。')")
    assert not errors, errors
    browser.close()
    print('Mobile heart reference, draft recovery, send and query progress passed.')

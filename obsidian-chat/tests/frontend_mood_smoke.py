"""Check emotion rings in the real chat page using an entirely mocked server.

Run: .venv/bin/python obsidian-chat/tests/frontend_mood_smoke.py
"""
import json
import mimetypes
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('/tmp/obsidian-mood-smoke')


def main():
    OUTPUT.mkdir(exist_ok=True)
    examples = {
        'happy': ('好耶！', '255, 105, 180'),
        'sad': ('有点难过。', '100, 149, 237'),
        'angry': ('气死我了！', '239, 68, 68'),
        'shy': ('有点害羞。', '255, 182, 193'),
        'tender': ('抱抱，晚安。', '186, 85, 211'),
    }
    now = time.time()

    def message(mid, content, **extra):
        return dict(id=mid, conv_id='c1', role='assistant', content=content,
                    created_at=now + len(messages), attachments=[], **extra)

    messages = []
    for mood, (content, _) in examples.items():
        messages.append(message(mood, content))
    messages.append(dict(message('user', '好耶！'), role='user'))
    messages.append(message('hidden-command', '[发图:开心的猫]'))
    messages.append(message('error', '抱歉，请求失败', transient_error=True, persisted=False))
    messages.append(message('neutral', '窗边的灯亮着。'))
    sockets, errors, checks, writes = [], [], [], []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=390, height=844), is_mobile=True,
                                      service_workers='block')
        context.add_init_script("localStorage.setItem('obsidian_tts_enabled', 'false')")

        def socket_route(socket):
            sockets.append(socket)
            socket.on_message(lambda text: socket.send('{"type":"pong"}')
                              if json.loads(text).get('type') == 'ping' else None)

        context.route_web_socket('**/ws', socket_route)

        def route_request(route):
            parsed = urlparse(route.request.url)
            name = parsed.path
            if name == '/chat':
                route.fulfill(path=ROOT / 'static/chat.html', content_type='text/html; charset=utf-8')
            elif name.startswith('/static/'):
                file = ROOT / name.lstrip('/')
                if file.is_file():
                    route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                else:
                    route.fulfill(status=404, body='missing local asset')
            elif name in ('/api/avatar/ai', '/api/avatar/user'):
                icon = 'AIIcon.png' if name.endswith('/ai') else 'UserIcon.png'
                route.fulfill(path=ROOT.parent / 'public/optimized' / icon, content_type='image/png')
            elif name in ('/api/conversations/c1/send', '/api/conversations/c1/regenerate'):
                writes.append(name)
                if name.endswith('/regenerate'):
                    old_id = parse_qs(parsed.query).get('replaced_message_id', [''])[0]
                    messages[:] = [m for m in messages if m['id'] != old_id]
                    reply = message('regenerated', '有点害羞。')
                else:
                    messages.append(dict(message('sent-user', route.request.post_data_json['content']), role='user'))
                    reply = message('sent', '抱抱，晚安。')
                messages.append(reply)
                events = [dict(type='start', id=reply['id'], user_message_id='sent-user' if name.endswith('/send') else None),
                          dict(type='chunk', content=reply['content']),
                          dict(type='message', message=reply)]
                route.fulfill(content_type='text/event-stream', body=''.join(
                    'data: ' + json.dumps(event) + '\n\n' for event in events))
            elif name.startswith('/api/'):
                data = {}
                if name == '/api/worldbook': data = dict(ai_name='予安', user_name='南枝')
                elif name == '/api/models': data = [dict(key='fixture', audio_input=False)]
                elif name == '/api/chat/daily':
                    data = dict(enabled=False, count_initialized=True, inherit_count=30, revision=0,
                                timezone='Asia/Shanghai', next_rollover_at=now+86400)
                elif name == '/api/conversations': data = [dict(id='c1', title='今晚', model='fixture'),
                                                          dict(id='empty', title='新对话', model='fixture')]
                elif name == '/api/conversations/c1/messages': data = messages
                elif name == '/api/conversations/empty/messages': data = []
                elif name == '/api/avatar/info': data = dict(ai=0, user=0)
                elif name == '/api/tts/voices': data = dict(voices=[])
                elif name == '/api/heart-whispers': data = dict(items=[], total=0)
                elif name == '/api/schedule/missed-recent': data = dict(count=0, items=[])
                elif name.endswith('/stream-status'): data = dict(streaming=False)
                route.fulfill(json=data)
            else:
                route.fulfill(status=204, body='')

        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def visit():
            page.goto('http://mood.test/chat', wait_until='load')
            page.wait_for_function('chatInitialized && currentMessages.length > 0')
            page.wait_for_selector('#m_happy .msg-avatar.mood-happy')

        def ring(mid):
            return page.locator('#m_' + mid + ' .msg-avatar-frame')

        def assert_mood(mid, mood):
            expected = mood if mood != 'neutral' else None
            assert ring(mid).get_attribute('data-mood') == expected, (mid, mood)
            style = ring(mid).locator('img').evaluate('el => { const s = getComputedStyle(el); '
                'return { shadow:s.boxShadow, radius:s.borderRadius, filter:s.filter, transform:s.transform }; }')
            assert style['radius'] == '50%', style
            assert style['filter'] == 'none' and style['transform'] == 'none', style
            if mood != 'neutral': assert examples[mood][1] in style['shadow'], style

        def push(mid, content, **extra):
            incoming = message(mid, content, **extra)
            messages.append(incoming)
            sockets[-1].send(json.dumps(dict(type='msg_created', data=incoming)))
            page.wait_for_selector('#m_' + mid)

        def wait_pulse(mid):
            page.wait_for_selector('#m_' + mid + ' .mood-pulse')
            assert page.locator('.mood-pulse').count() == 1

        def no_pulse():
            page.wait_for_function("document.querySelectorAll('.mood-pulse').length === 0")

        visit()
        for mood in examples: assert_mood(mood, mood)
        for mid in ('hidden-command', 'error', 'neutral'): assert_mood(mid, 'neutral')
        assert page.locator('#m_user .msg-avatar-frame, #m_user [class*="mood-"]').count() == 0
        checks.append('history_restores_all_five_colors_without_decorating_user_errors_or_hidden_commands')

        page.reload(wait_until='load')
        page.wait_for_selector('#m_happy .msg-avatar.mood-happy')
        for mood in examples: assert_mood(mood, mood)
        checks.append('full_reload_restores_history')

        push('live', '好耶！')
        wait_pulse('live')
        assert_mood('live', 'happy')
        animation = page.evaluate("""() => {
            const a = document.getAnimations().find(a => a.animationName === 'moodGlow');
            return {duration:a.effect.getTiming().duration, iterations:a.effect.getTiming().iterations,
                    frames:a.effect.getKeyframes()};
        }""")
        assert animation['duration'] == 2600 and animation['iterations'] == 2, animation
        assert all(set(f) <= {'opacity', 'offset', 'computedOffset', 'easing', 'composite'}
                   for f in animation['frames']), animation
        page.wait_for_timeout(250)
        page.evaluate('renderMessages()')
        wait_pulse('live')
        assert ring('live').evaluate("el => parseFloat(getComputedStyle(el, '::after').animationDelay)") < -.2
        no_pulse()
        assert_mood('live', 'happy')
        page.evaluate('renderMessages()')
        assert page.locator('.mood-pulse').count() == 0
        checks.append('only_latest_reply_breathes_twice_using_opacity_then_stays_colored_without_restarting')

        messages[-1]['content'] = '窗边的灯亮着。'
        sockets[-1].send(json.dumps(dict(type='msg_updated', data=messages[-1])))
        page.wait_for_function("document.querySelector('#m_live .msg-avatar-frame').dataset.mood === undefined")
        assert_mood('live', 'neutral')
        checks.append('neutral_edit_clears_stale_color')

        page.locator('#input').fill('今晚过得怎么样？')
        page.locator('#sendBtn').click()
        page.wait_for_function("!sending && currentMessages.some(m => m.id === 'sent')")
        assert_mood('sent', 'tender')
        wait_pulse('sent')
        page.evaluate("regenerateMsg('sent')")
        page.wait_for_function("!sending && currentMessages.some(m => m.id === 'regenerated')")
        assert page.locator('#m_sent').count() == 0
        assert_mood('regenerated', 'shy')
        wait_pulse('regenerated')
        checks.append('send_and_regenerate_streams_restore_matching_color')

        page.evaluate('onChatBackground()')
        no_pulse()
        assert_mood('regenerated', 'shy')
        page.evaluate('onChatForeground()')
        page.wait_for_function('!chatSyncPending')
        assert_mood('regenerated', 'shy')
        assert page.locator('.mood-pulse').count() == 0
        checks.append('native_background_stops_motion_and_resume_retains_color')

        page.emulate_media(reduced_motion='reduce')
        push('reduced', '气死我了！')
        assert_mood('reduced', 'angry')
        assert page.locator('.mood-pulse').count() == 0
        page.emulate_media(reduced_motion='no-preference')
        push('offscreen', '抱抱，晚安。')
        wait_pulse('offscreen')
        page.evaluate("document.getElementById('messages').scrollTop = 0")
        no_pulse()
        assert_mood('offscreen', 'tender')
        checks.append('reduced_motion_and_offscreen_stop_animation_but_keep_color')

        # Incoming replies now preserve history reading position. Return to the
        # bottom before exercising the separate covered-page animation case.
        page.evaluate('scrollBottom({force:true})')
        page.wait_for_timeout(100)
        push('covered', '有点难过。')
        wait_pulse('covered')
        page.evaluate("document.getElementById('subPageOverlay').classList.add('show')")
        no_pulse()
        page.evaluate("document.getElementById('subPageOverlay').classList.remove('show')")
        push('hidden', '好耶！')
        wait_pulse('hidden')
        page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:true}); "
                      "document.dispatchEvent(new Event('visibilitychange'))")
        no_pulse()
        page.evaluate("delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))")
        push('pagehide', '有点害羞。')
        wait_pulse('pagehide')
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
        no_pulse()
        checks.append('covered_page_hidden_document_and_pagehide_stop_motion')

        for width in (375, 768, 1440):
            page.set_viewport_size(dict(width=width, height=1000))
            page.evaluate("document.getElementById('messages').scrollTop = 0")
            page.wait_for_timeout(100)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert_mood('happy', 'happy')
            page.screenshot(path=str(OUTPUT / f'chat-{width}.png'), animations='disabled')
        checks.append('mobile_tablet_desktop_layouts')

        page.evaluate("selectConv('empty')")
        page.wait_for_function('currentMessages.length === 0')
        assert page.locator('.msg-avatar-frame').count() == 0
        assert page.evaluate('Object.keys(_msgMoods).length') == 0
        assert page.evaluate('_moodTextCache.size') == 0
        checks.append('empty_conversation_clears_mood_state')

        assert writes == ['/api/conversations/c1/send', '/api/conversations/c1/regenerate'], writes
        assert not errors, errors
        result = dict(passed=checks, page_errors=errors, real_model_calls=0, screenshots=str(OUTPUT))
        (OUTPUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()

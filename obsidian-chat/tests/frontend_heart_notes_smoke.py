"""Exercise contextual heart notes in the actual chat page, with no real server writes.

Run: .venv/bin/python obsidian-chat/tests/frontend_heart_notes_smoke.py
"""
import json
import mimetypes
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('/tmp/obsidian-heart-notes')


def main():
    OUTPUT.mkdir(exist_ok=True)
    now = time.time()
    messages, whispers, sockets, errors, checks, writes, history_requests = [], [], [], [], [], [], []

    def message(mid, content='灯还亮着，我陪你坐一会儿。', role='assistant'):
        return dict(id=mid, conv_id='c1', role=role, content=content,
                    created_at=now + len(messages), attachments=[])

    def note(mid, content, nid=None):
        record = dict(id=nid or 'hw_' + mid, msg_id=mid, content=content, created_at=now + len(whispers))
        whispers.append(record)
        return record

    messages.extend([message('hello', '今天终于可以早点休息了。', 'user'),
                     message('history', '那就把灯调暗一点，抱抱。'),
                     message('last', '窗外在下雨。我们听一会儿吧。')])
    history_text = '南枝说想早点休息的时候，予安也跟着松了一口气。\n这一刻不用急着说些什么，安静待在一起就很好。'
    note('history', history_text)
    note('wrong-conversation', '这条属于另一段对话。')

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
            if name in ('/chat', '/heart-whispers'):
                route.fulfill(path=ROOT / 'static' / (name[1:] + '.html'), content_type='text/html; charset=utf-8')
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
                    reply = message('regenerated', '有点害羞。那就再靠近一点。')
                else:
                    messages.append(message('sent-user', route.request.post_data_json['content'], 'user'))
                    reply = message('sent', '抱抱，晚安。')
                record = note(reply['id'], '予安想让南枝今晚睡得安稳一点。')
                # The server can broadcast before the persisted reply reaches either transport.
                sockets[-1].send(json.dumps(dict(type='heart_whisper', data=record)))
                messages.append(reply)
                events = [dict(type='start', id=reply['id'], user_message_id='sent-user' if name.endswith('/send') else None),
                          dict(type='chunk', content=reply['content']), dict(type='heart_whisper', **record),
                          dict(type='message', message=reply)]
                route.fulfill(content_type='text/event-stream', body=''.join(
                    'data: ' + json.dumps(event) + '\n\n' for event in events))
            elif name.startswith('/api/heart-whispers/') and route.request.method == 'DELETE':
                writes.append(name)
                whispers[:] = [n for n in whispers if n['id'] != name.rsplit('/', 1)[-1]]
                route.fulfill(json=dict(ok=True))
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
                elif name == '/api/heart-whispers':
                    history_requests.append(route.request.url)
                    data = dict(items=list(reversed(whispers)), total=len(whispers), page=1, page_size=100)
                elif name == '/api/schedule/missed-recent': data = dict(count=0, items=[])
                elif name.endswith('/stream-status'): data = dict(streaming=False)
                route.fulfill(json=data)
            else:
                route.fulfill(status=204, body='')

        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        page.clock.install()
        page.goto('http://heart.test/chat', wait_until='load')
        page.wait_for_function('chatInitialized && currentMessages.length > 0')
        page.evaluate('ChatHeartNotes.init()')
        page.wait_for_selector('#m_history .heart-whisper-hint')

        def advance(ms):
            page.clock.fast_forward(ms)
            page.wait_for_timeout(50)

        def close():
            page.locator('.heart-note-close').click()
            assert page.locator('.heart-note').count() == 0

        def push(mid, content):
            page.mouse.move(0, 0)  # A cursor resting over the note intentionally pauses its dismissal.
            record = note(mid, content)
            sockets[-1].send(json.dumps(dict(type='heart_whisper', data=record)))
            incoming = message(mid)
            messages.append(incoming)
            sockets[-1].send(json.dumps(dict(type='msg_created', data=incoming)))
            page.wait_for_selector('#m_' + mid)
            page.wait_for_selector('#m_' + mid + ' .heart-whisper-hint')
            page.evaluate("document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight")
            page.wait_for_timeout(50)
            return record

        def preview(mid):
            advance(2100)
            page.wait_for_selector('.heart-note-preview')
            assert page.locator('.heart-note').count() == 1
            assert page.locator('.heart-note').get_attribute('data-msg-id') == mid

        advance(12000)
        assert page.locator('.heart-note').count() == 0
        assert page.locator('.heart-whisper-hint').count() == 1
        page.locator('#m_history .heart-whisper-hint').click()
        assert page.locator('.heart-note-content').inner_text() == history_text
        advance(16000)
        assert page.locator('.heart-note-open').count() == 1
        page.evaluate('renderMessages()')
        assert page.locator('.heart-note-content').inner_text() == history_text
        page.screenshot(path=str(OUTPUT / 'history-mobile.png'), animations='disabled')
        page.keyboard.press('Escape')
        assert page.locator('.heart-note').count() == 0
        assert page.locator('#m_history .heart-whisper-hint').evaluate('el => el === document.activeElement')
        checks.append('history_is_quiet_expands_in_full_and_stays_open_across_time_and_rerender')

        long_text = '南枝刚刚说的那句话，予安其实听了很久。雨声落在窗边，灯光也慢慢软下来。还有一些没来得及说出口的话，可以先留在这里，等南枝想看的时候再打开。'
        record = push('live', long_text)
        page.locator('#input').dispatch_event('compositionstart')
        advance(5000)
        assert page.locator('.heart-note').count() == 0
        page.locator('#input').dispatch_event('compositionend')
        page.locator('#input').fill('还在写')
        advance(1000)
        page.locator('#input').fill('还在写一点')
        advance(1000)
        assert page.locator('.heart-note').count() == 0
        preview('live')
        excerpt = page.locator('.heart-note-excerpt')
        assert excerpt.inner_text() == long_text
        assert excerpt.evaluate('el => el.clientHeight <= parseFloat(getComputedStyle(el).lineHeight) * 2 + 1')
        page.screenshot(path=str(OUTPUT / 'preview-mobile.png'), animations='disabled')
        sockets[-1].send(json.dumps(dict(type='heart_whisper', data=record)))
        page.evaluate('renderMessages()')
        assert page.locator('.heart-note').count() == 1
        page.locator('.heart-note-preview').click()
        assert page.locator('.heart-note-content').inner_text() == long_text
        close()
        advance(10000)
        assert page.locator('.heart-note').count() == 0
        checks.append('new_note_waits_for_composition_and_idle_previews_two_lines_and_deduplicates_realtime')

        push('timed', '没有急着说出口的一小句。')
        preview('timed')
        advance(3000)
        page.evaluate('renderMessages()')
        advance(3000)
        assert page.locator('.heart-note').count() == 1
        advance(2500)
        assert page.locator('.heart-note').count() == 0
        page.locator('#m_timed .heart-whisper-hint').click()
        assert page.locator('.heart-note-content').inner_text() == '没有急着说出口的一小句。'
        close()
        checks.append('preview_collapses_after_eight_seconds_without_reset_on_rerender_and_remains_reopenable')

        # Drive actual streaming handlers with a held stream, then actual send/regenerate fetches.
        page.evaluate("""async () => {
          window.heartTestStream = createSendStreamState(); sending = true;
          await handleSendStreamEvent({type:'start', id:'held-stream'}, heartTestStream);
          await handleSendStreamEvent({type:'chunk', content:'先写下一半。'}, heartTestStream);
        }""")
        stream_note = note('held-stream', '这一句等写完再给南枝看。')
        sockets[-1].send(json.dumps(dict(type='heart_whisper', data=stream_note)))
        advance(5000)
        assert page.locator('.heart-note').count() == 0
        reply = message('held-stream')
        messages.append(reply)
        page.evaluate("""async reply => {
          await handleSendStreamEvent({type:'message', message:reply}, heartTestStream);
          sending = false; streamingAiId = null; ChatHeartNotes.sync();
        }""", reply)
        preview('held-stream')
        close()
        page.locator('#input').fill('晚安。')
        page.locator('#sendBtn').click()
        page.wait_for_function("!sending && currentMessages.some(m => m.id === 'sent')")
        preview('sent')
        page.locator('.heart-note-preview').click()
        assert page.locator('.heart-note-entry').count() == 1
        assert page.locator('#m_sent .msg-avatar-frame').get_attribute('data-mood') == 'tender'
        close()
        page.evaluate("regenerateMsg('sent')")
        page.wait_for_function("!sending && currentMessages.some(m => m.id === 'regenerated')")
        preview('regenerated')
        assert page.locator('#m_sent').count() == 0
        assert page.locator('#m_regenerated .msg-avatar-frame').get_attribute('data-mood') == 'shy'
        close()
        checks.append('streaming_waits_until_reply_finishes_and_send_regenerate_ws_sse_preserve_note_and_mood')

        for mechanism, suspend, resume in [
            ('native', 'onChatBackground()', 'onChatForeground()'),
            ('covered', "document.getElementById('subPageOverlay').classList.add('show')",
             "document.getElementById('subPageOverlay').classList.remove('show')"),
            ('hidden', "Object.defineProperty(document, 'hidden', {configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange'))",
             "delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))"),
            ('pagehide', "window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))",
             "window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))"),
        ]:
            push(mechanism, '这一小段读到一半，还会留在原处。')
            preview(mechanism)
            advance(2000)
            page.evaluate(suspend)
            count = len(history_requests)
            advance(70000)
            assert len(history_requests) == count, (mechanism, 'background history polling')
            assert page.locator('.heart-note').count() == 1
            assert page.locator('.heart-note-enter').count() == 0
            page.evaluate(resume)
            page.wait_for_function('!chatSyncPending')
            advance(2000)
            assert page.locator('.heart-note').count() == 1, mechanism
            advance(4500)
            assert page.locator('.heart-note').count() == 0, mechanism
        checks.append('all_background_and_cover_paths_pause_preview_budget_and_do_not_poll')

        unsafe_text = '一小句\n<img src=x onerror="window.heartInjected=true">\n' + '很长的一句心语。' * 30
        page.emulate_media(reduced_motion='reduce')
        push('safe', unsafe_text)
        preview('safe')
        assert page.locator('.heart-note-enter').count() == 0
        page.locator('.heart-note-preview').click()
        second = note('safe', '还有一句，可以一起收在这里。', 'hw_safe_second')
        sockets[-1].send(json.dumps(dict(type='heart_whisper', data=second)))
        page.wait_for_function("document.querySelectorAll('.heart-note-entry').length === 2")
        assert page.locator('.heart-note-content').first.inner_text() == unsafe_text
        assert page.locator('.heart-note img, .heart-note script').count() == 0
        assert page.evaluate('window.heartInjected !== true')
        for width in (375, 768, 1440):
            page.set_viewport_size(dict(width=width, height=1000))
            page.locator('.heart-note').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.heart-note').evaluate('el => el.scrollWidth <= el.clientWidth')
            old_top = page.locator('.heart-note').bounding_box()['y']
            page.evaluate('renderMessages()')
            page.wait_for_timeout(100)
            new_top = page.locator('.heart-note').bounding_box()['y']
            assert abs(new_top - old_top) <= 2, (width, old_top, new_top)
            page.screenshot(path=str(OUTPUT / f'expanded-{width}.png'), animations='disabled')
        close()
        checks.append('multiple_notes_long_literal_text_reduced_motion_and_reading_anchor_work_at_three_widths')

        page.reload(wait_until='load')
        page.wait_for_function('chatInitialized && currentMessages.length > 0')
        page.evaluate('ChatHeartNotes.init()')
        assert page.locator('#m_safe .heart-whisper-hint').count() == 1
        advance(12000)
        assert page.locator('.heart-note').count() == 0
        page.locator('#m_live .heart-whisper-hint').click()
        page.evaluate("openSubPage('/heart-whispers')")
        frame = page.frame_locator('#subPageFrame')
        frame.locator("button[onclick=\"deleteWhisper('hw_live')\"]").click()
        page.wait_for_function("!document.querySelector('#m_live .heart-whisper-hint')")
        assert page.locator('#m_live .heart-whisper-hint').count() == 0
        assert page.locator('.heart-note').count() == 0
        page.evaluate('closeSubPage()')
        page.locator('#m_safe .heart-whisper-hint').click()
        page.evaluate("selectConv('empty')")
        page.wait_for_function('currentMessages.length === 0')
        assert page.locator('.heart-note, .heart-whisper-hint').count() == 0
        checks.append('reload_is_quiet_archive_delete_clears_inline_note_and_conversation_switch_clears_paper')

        assert writes == ['/api/conversations/c1/send', '/api/conversations/c1/regenerate', '/api/heart-whispers/hw_live'], writes
        assert not errors, errors
        result = dict(passed=checks, page_errors=errors, real_model_calls=0, screenshots=str(OUTPUT))
        (OUTPUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()

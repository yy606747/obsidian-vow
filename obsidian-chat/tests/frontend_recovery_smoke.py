"""Exercise the complete chat page with a mocked server and Android bridge.

No running backend, real conversations, model requests or device actions are used.
Run: .venv/bin/python obsidian-chat/tests/frontend_recovery_smoke.py
"""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('/tmp/obsidian-recovery-smoke')


def main() -> None:
    messages = [dict(id='old', conv_id='c1', role='assistant', content='后台前的消息', created_at=100, attachments=[])]
    fail_messages = False
    page_errors: list[str] = []
    sockets = []
    api_requests: list[str] = []
    daily_state = dict(enabled=False, count_initialized=True, inherit_count=30, timezone='Asia/Shanghai',
                       day_key='2026-09-08', revision=0, active_conv_id='c1', redirects={},
                       next_rollover_at=time.time()+86400, latest_assistant_at=400)
    daily_messages: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 412, 'height': 915}, is_mobile=True, device_scale_factor=1)
        context.add_init_script("""
          window.notificationAcks = [];
          window.ObsidianNotifications = {
            syncStarted() {}, pendingConversationId() { return ''; },
            synced(at) { window.notificationAcks.push(at); }
          };
        """)
        page = context.new_page()
        page.on('pageerror', lambda error: page_errors.append(str(error)))

        def socket_route(socket):
            sockets.append(socket)
            socket.on_message(lambda text: socket.send('{"type":"pong"}') if json.loads(text).get('type') == 'ping' else None)

        page.route_web_socket('**/ws', socket_route)

        def route_request(route):
            url = urlparse(route.request.url)
            name = url.path
            if name == '/chat':
                route.fulfill(path=ROOT / 'static/chat.html', content_type='text/html; charset=utf-8')
                return
            if name.startswith('/static/'):
                file = ROOT / name.lstrip('/')
                if file.is_file():
                    route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                else:
                    route.fulfill(status=404, body='missing fixture asset')
                return
            if name in ('/api/avatar/ai', '/api/avatar/user'):
                route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" rx="20" fill="#9b734c"/></svg>')
                return
            if name.startswith('/api/'):
                api_requests.append(name)
                data = {}
                if name == '/api/models':
                    data = [{'key': 'gemini-3-flash', 'audio_input': False},
                            {'key': 'test-model', 'audio_input': True}]
                elif name == '/api/upload':
                    data = {'url': '/uploads/daily-voice.wav', 'type': 'audio/wav', 'name': 'daily-voice.wav'}
                elif name == '/api/voice/remote-asr':
                    data = {'text': '跨日录音验证'}
                elif name == '/api/chat/daily':
                    if route.request.method == 'PUT':
                        daily_state.update(route.request.post_data_json)
                    data = daily_state
                elif name == '/api/worldbook':
                    data = {'ai_name': 'Companion', 'user_name': 'Owner'}
                elif name == '/api/conversations':
                    data = [dict(id='c1', title='恢复验证', model='test-model', message_count=len(messages))]
                    if daily_state['enabled']:
                        data.insert(0, dict(id='c2', title='2026-09-09', model='test-model', message_count=len(daily_messages)))
                elif name in ('/api/conversations/c1/messages', '/api/conversations/c2/messages'):
                    if fail_messages:
                        route.fulfill(status=503, json={'error': 'offline fixture'})
                        return
                    data = list(daily_messages if '/c2/' in name else messages)
                elif name == '/api/tts/voices':
                    data = {'voices': []}
                elif name == '/api/avatar/info':
                    data = {'ai': 0, 'user': 0}
                elif 'heart-whisper' in name:
                    data = []
                route.fulfill(json=data)
                return
            route.fulfill(status=204, body='')

        page.route('**/*', route_request)
        page.goto('http://recovery.test/chat', wait_until='load')
        page.wait_for_function('chatInitialized && notificationAcks.includes(100)')
        assert page.locator('#m_old').is_visible()
        assert page.locator('#modelSelect').input_value() == 'test-model'
        page.locator('#input').fill('恢复后仍保留的草稿')

        page.evaluate('onChatBackground()')
        ack_count = page.evaluate('notificationAcks.length')
        messages.append(dict(id='latest', conv_id='c1', role='assistant', content='后台期间新到的消息，切回来就应该看见。', created_at=200, attachments=[]))
        page.evaluate('onChatForeground()')
        page.wait_for_function('notificationAcks.at(-1) === 200')
        assert page.locator('#m_latest').is_visible()
        assert page.locator('#input').input_value() == '恢复后仍保留的草稿'
        assert page.evaluate('notificationAcks.length') > ack_count

        page.evaluate('onChatBackground()')
        fail_messages = True
        ack_count = page.evaluate('notificationAcks.length')
        page.evaluate('onChatForeground()')
        page.wait_for_timeout(100)
        assert page.evaluate('notificationAcks.length') == ack_count
        assert page.locator('#m_latest').is_visible()
        fail_messages = False
        messages.append(dict(id='after_retry', conv_id='c1', role='assistant', content='重新连接后补齐的消息。', created_at=300, attachments=[]))
        page.evaluate('refreshChat()')
        page.wait_for_function('notificationAcks.at(-1) === 300')

        # 前台实时消息也必须显示并确认，且没有因为重复入口建立多条连接。
        newest = dict(id='live', conv_id='c1', role='assistant', content='前台实时到达的消息。', created_at=400, attachments=[])
        messages.append(newest)
        sockets[-1].send(json.dumps({'type': 'msg_created', 'data': newest}))
        page.wait_for_function('notificationAcks.at(-1) === 400')
        assert page.locator('#m_live').is_visible()
        assert len(sockets) == 1, f'duplicate sockets: {len(sockets)}'

        page.reload(wait_until='load')
        page.wait_for_function('chatInitialized && notificationAcks.includes(400)')
        assert page.locator('#input').input_value() == '恢复后仍保留的草稿'
        assert not page_errors, page_errors
        OUTPUT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUTPUT / 'chat-recovery.png'), animations='disabled')

        # Keep the actual send-stream handler and input UI alive across a simulated server midnight.
        page.evaluate("""async () => {
          const voice = await ensureVoiceMessageLoaded();
          Object.assign(voice.recorder, { active: true, useNative: true, ownsNative: false,
            sourceConvId: currentConvId, sourceModelKey: 'test-model', sampleRate: 16000,
            nativeChunks: [new Uint8Array(16000)], startedAt: performance.now() });
          window.dailyTestStream = createSendStreamState();
          sending = true;
          pendingAttachments = [{ url: '/api/avatar/user', type: 'image/png', name: '未发送的图片.png' }];
          renderPreview();
          await handleSendStreamEvent({ type: 'start', id: 'daily-stream' }, dailyTestStream);
          await handleSendStreamEvent({ type: 'chunk', content: '凌晨前的半句，' }, dailyTestStream);
        }""")
        daily_state.update(enabled=True, revision=1, day_key='2026-09-09', active_conv_id='c2', redirects={'c1': 'c2'})
        fail_messages = True
        ack_count = page.evaluate('notificationAcks.length')
        sockets[-1].send(json.dumps({'type': 'daily_chat_rollover', 'data': {
            'conv_id': 'c2', 'previous_conv_id': 'c1', 'state': daily_state,
            'conversation': dict(id='c2', title='2026-09-09', model='test-model'),
        }}))
        page.wait_for_function("currentConvId === 'c2' && chatSyncPending")
        assert page.locator('#modelSelect').input_value() == 'test-model'
        assert page.locator('#m_old').count() == 0
        assert page.locator('#m_live').count() == 0
        assert page.locator('#m_daily-stream').is_visible()
        assert '凌晨前的半句' in page.locator('#m_daily-stream').inner_text()
        assert page.locator('#input').input_value() == '恢复后仍保留的草稿'
        assert page.evaluate('pendingAttachments.length') == 1
        assert page.evaluate('sending')
        assert page.evaluate("ChatApp.getModule('voiceMessage').recorder.sourceConvId") == 'c2'
        assert page.evaluate("ChatApp.getModule('voiceMessage').recorder.nativeChunks[0].length") == 16000
        assert page.evaluate('notificationAcks.length') == ack_count
        page.locator('#input').fill('跨日后继续写的草稿')
        fail_messages = False
        page.evaluate('refreshChat()')
        page.wait_for_function('!chatSyncPending')
        assert page.locator('#m_old').count() == 0
        assert '跨日背景' not in page.locator('#messages').inner_text()
        page.evaluate("handleSendStreamEvent({type:'chunk', content:'在新一天接着说完。'}, dailyTestStream)")
        final_reply = dict(id='daily-stream', conv_id='c2', role='assistant', content='凌晨前的半句，在新一天接着说完。', created_at=500, attachments=[])
        daily_messages.append(final_reply)
        sockets[-1].send(json.dumps({'type': 'msg_created', 'data': final_reply}))
        page.wait_for_function('notificationAcks.at(-1) === 500')
        assert page.locator('#m_daily-stream').count() == 1
        assert final_reply['content'] in page.locator('#m_daily-stream').inner_text()
        assert page.locator('#input').input_value() == '跨日后继续写的草稿'
        assert page.evaluate('pendingAttachments[0].name') == '未发送的图片.png'
        page.evaluate('sending = false')
        page.evaluate("""async () => {
          const originalSend = send;
          send = async () => { window.dailyVoiceDelivery = { conv_id: currentConvId, attachments: pendingAttachments.slice() }; };
          try { await ChatApp.getModule('voiceMessage').recorder.stop(false); }
          finally { send = originalSend; }
        }""")
        assert page.evaluate('dailyVoiceDelivery.conv_id') == 'c2'
        assert page.evaluate("dailyVoiceDelivery.attachments.find(item => item.type === 'voice').duration_ms") == 500
        assert api_requests.count('/api/upload') == 1
        page.reload(wait_until='load')
        page.wait_for_function("chatInitialized && currentConvId === 'c2'")
        assert page.locator('#modelSelect').input_value() == 'test-model'
        assert page.locator('#m_old').count() == 0
        assert page.locator('#m_daily-stream').count() == 1
        assert page.locator('#input').input_value() == '跨日后继续写的草稿'
        assert page.evaluate('pendingAttachments.length') == 2
        assert not page_errors, page_errors
        page.screenshot(path=str(OUTPUT / 'chat-daily-rollover.png'), animations='disabled')
        result = {
            'passed': ['cold_start', 'background_resume', 'read_after_render', 'offline_keeps_notifications',
                       'retry_recovers', 'foreground_push', 'single_socket', 'draft_survives_reload',
                       'midnight_stream_continues', 'midnight_failed_fetch_keeps_notifications',
                       'midnight_text_and_attachment_draft', 'midnight_reply_persists_once',
                       'midnight_drafts_survive_reload', 'midnight_recording_encoded_and_delivered_to_new_window',
                       'midnight_model_is_not_first_option', 'midnight_history_is_prompt_only'],
            'page_errors': page_errors,
            'message_fetches': api_requests.count('/api/conversations/c1/messages'),
            'screenshot': str(OUTPUT / 'chat-recovery.png'),
        }
        (OUTPUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()

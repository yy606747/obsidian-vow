"""Exercise Markdown, source preservation, streaming and layouts in the real chat page.

All backend and WebSocket traffic is mocked. Run with .venv/bin/python.
"""
import json
import mimetypes
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('/tmp/obsidian-markdown')


def main():
    OUTPUT.mkdir(exist_ok=True)
    now = time.time()
    messages, sockets, errors, checks, writes = [], [], [], [], []
    rich = '# 今天我最大\n\n晚饭之后，**留一点时间给我们**。\n\n- 散步\n  - 看看月亮\n  - 慢慢走\n- 回家喝杯热茶\n\n> 今晚不用赶时间。'
    code_text = 'const greeting = "晚安";\n\nconsole.log(greeting);\n'
    technical = '## 留在这里的小记录\n\n```javascript\n' + code_text + '```\n\n| 时刻 | 地点 | 心情 | 备注 |\n| :--- | :---: | ---: | --- |\n| 傍晚 | 窗边 | 放松 | 今天提前收工 |\n| 夜里 | 家里 | 开心 | 热茶和闲聊 |'

    def message(mid, content, role='assistant', **extra):
        return dict(id=mid, conv_id='c1', role=role, content=content,
                    created_at=now + len(messages), attachments=[], **extra)

    messages.extend([message('plain', '晚安，**抱抱**。\n*靠近一点*，这是 `一句悄悄话`。', 'user'),
                     message('rich-user', rich, 'user'), message('rich-ai', rich),
                     message('technical', technical),
                     message('links', '[MUSIC](https://example.test/music) 和 [S](https://example.test/s)。'),
                     message('indent', '    first_line()\n\n    second_line()')])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=390, height=844), is_mobile=True, service_workers='block')
        context.add_init_script("""
          localStorage.setItem('obsidian_tts_enabled','false');
          window.copiedText = '';
          Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
            writeText: async text => { window.copiedText = text; }
          }});
        """)

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
                if file.is_file(): route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                else: route.fulfill(status=404, body='missing local asset')
            elif name in ('/api/avatar/ai', '/api/avatar/user'):
                icon = 'AIIcon.png' if name.endswith('/ai') else 'UserIcon.png'
                route.fulfill(path=ROOT.parent / 'public/optimized' / icon, content_type='image/png')
            elif name.startswith('/api/conversations/c1/') and name.rsplit('/', 1)[-1] in ('send', 'regenerate', 'dom-initiative', 'whisper-initiative'):
                writes.append(dict(path=name, body=route.request.post_data_json))
                kind = name.rsplit('/', 1)[-1]
                if kind == 'send': messages.append(message('sent-user', route.request.post_data_json['content'], 'user'))
                if kind == 'regenerate':
                    replaced = parse_qs(parsed.query).get('replaced_message_id', [''])[0]
                    messages[:] = [m for m in messages if m['id'] != replaced]
                reply = message(kind, '# 抱抱\n\n'+technical)
                messages.append(reply)
                events = [dict(type='start', id=kind, user_message_id='sent-user' if kind == 'send' else None),
                          dict(type='chunk', content=reply['content'][:20]), dict(type='chunk', content=reply['content'][20:]),
                          dict(type='message', message=reply)]
                route.fulfill(content_type='text/event-stream', body=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
            elif name.startswith('/api/messages/') and route.request.method == 'PUT':
                writes.append(dict(path=name, body=route.request.post_data_json))
                for m in messages:
                    if m['id'] == name.rsplit('/', 1)[-1]: m['content'] = route.request.post_data_json['content']
                route.fulfill(json=dict(ok=True))
            elif name.startswith('/api/'):
                data = {}
                if name == '/api/worldbook': data = dict(ai_name='予安', user_name='南枝')
                elif name == '/api/models': data = [dict(key='fixture', audio_input=False)]
                elif name == '/api/chat/daily': data = dict(enabled=False, count_initialized=True, inherit_count=30, revision=0, timezone='Asia/Shanghai', next_rollover_at=now+86400)
                elif name == '/api/conversations': data = [dict(id='c1', title='今晚', model='fixture'), dict(id='empty', title='新对话', model='fixture')]
                elif name == '/api/conversations/c1/messages': data = messages
                elif name == '/api/conversations/empty/messages': data = []
                elif name == '/api/avatar/info': data = dict(ai=0, user=0)
                elif name == '/api/tts/voices': data = dict(voices=[])
                elif name == '/api/heart-whispers': data = dict(items=[], total=0)
                elif name == '/api/schedule/missed-recent': data = dict(count=0, items=[])
                elif name.endswith('/stream-status'): data = dict(streaming=False)
                route.fulfill(json=data)
            else: route.fulfill(status=204, body='')

        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.clock.install()
        page.goto('http://markdown.test/chat', wait_until='load')
        page.wait_for_function('chatInitialized && currentMessages.length > 0')
        assert page.locator('#m_plain .msg-bubble strong').inner_text() == '抱抱'
        assert page.locator('#m_plain .msg-bubble em').inner_text() == '靠近一点'
        assert page.locator('#m_plain .md-document').count() == 0
        for mid in ('rich-user', 'rich-ai'):
            assert page.locator('#m_'+mid+' h1').inner_text() == '今天我最大'
            assert page.locator('#m_'+mid+' .msg-bubble').count() == 0
            assert page.locator('#m_'+mid+' li').count() == 4
            assert page.locator('#m_'+mid+' blockquote').inner_text() == '今晚不用赶时间。'
        assert page.locator('#m_rich-user .md-document').inner_html() == page.locator('#m_rich-ai .md-document').inner_html()
        assert page.locator('#m_technical pre code').text_content() == code_text
        assert page.locator('#m_technical table').count() == 1
        assert page.locator('#m_technical th').nth(1).get_attribute('align') == 'center'
        assert page.locator('#m_links a').count() == 2
        assert 'first_line()\n\nsecond_line()' in page.locator('#m_indent pre code').text_content()
        checks.append('both_sides_share_markdown_rules_preserve_blocks_and_keep_plain_messages_in_bubbles')

        page.evaluate("copyMsg('rich-user')")
        assert page.evaluate('copiedText') == rich
        page.evaluate("editMsg('rich-user')")
        assert page.locator('#edit_rich-user').input_value() == rich
        edited = '    first_line()\n\n    second_line()\n'
        page.locator('#edit_rich-user').fill(edited)
        page.evaluate("saveEdit('rich-user')")
        assert page.locator('#m_rich-user pre code').text_content() == 'first_line()\n\nsecond_line()\n'
        page.locator('#m_technical [data-md-copy]').click()
        assert page.evaluate('copiedText') == code_text
        page.locator('#input').fill(edited)
        page.locator('#sendBtn').click()
        page.wait_for_function("!sending && currentMessages.some(m => m.id === 'send')")
        assert writes[-1]['body']['content'] == edited
        assert page.locator('#m_sent-user pre code').count() == 1
        assert page.locator('#m_send h1').inner_text() == '抱抱'
        page.evaluate("regenerateMsg('send')")
        page.wait_for_function("!sending && currentMessages.some(m => m.id === 'regenerate')")
        assert page.locator('#m_send').count() == 0
        assert page.locator('#m_regenerate table').count() == 1
        page.reload(wait_until='load')
        page.wait_for_function('chatInitialized && !chatSyncPending')
        assert page.evaluate("currentMessages.find(m => m.id === 'sent-user').content") == edited
        assert page.locator('#m_sent-user pre code').count() == 1
        checks.append('copy_edit_send_regenerate_and_reload_retain_raw_markdown_and_leading_indentation')

        malicious = '# 安全显示\n\n<script>window.mdInjected=1</script>\n\n<img src=x onerror="window.mdInjected=2">\n\n[点我](javascript:alert(1))\n\n```html\n<div onclick="boom()">字面代码</div>\n```'
        for role in ('user', 'assistant'):
            m = message('literal-'+role, malicious, role)
            messages.append(m)
            sockets[-1].send(json.dumps(dict(type='msg_created', data=m)))
            page.wait_for_selector('#m_literal-'+role+' h1')
            content = page.locator('#m_literal-'+role+' .msg-content')
            assert content.locator('script, img, iframe, [onclick], [onerror], a[href^="javascript:"]').count() == 0
            assert '<div onclick="boom()">字面代码</div>' in content.locator('code').inner_text()
        assert page.evaluate('window.mdInjected === undefined')
        checks.append('raw_html_and_code_are_literal_and_unsafe_urls_do_not_become_active_elements')

        live = message('live', '...')
        # In-progress replies are local stream state, not persisted history returned by GET.
        page.evaluate("m => { currentMessages.push(m); streamingAiId=m.id; renderMessages(); }", live)
        page.evaluate("""() => {
          window.mdPaints = 0;
          window.mdObserver = new MutationObserver(() => mdPaints++);
          mdObserver.observe(document.querySelector('#m_live [data-msg-content]'), {childList:true});
          window.mdTicks = 0;
          window.mdInterval = setInterval(() => {
            mdTicks++;
            updateStreamingAssistantMessage('live', '# 边写边读\\n\\n' + '一句话。'.repeat(mdTicks));
            if (mdTicks === 50) clearInterval(mdInterval);
          }, 20);
        }""")
        page.clock.run_for(1100)
        assert page.evaluate('mdTicks') == 50
        paints = page.evaluate('mdPaints')
        assert 5 <= paints <= 12, paints
        assert page.locator('#m_live h1').inner_text() == '边写边读'
        assert page.evaluate("currentMessages.find(m => m.id==='live').content.startsWith('# 边写边读')")
        for background, resume in [
            ('onChatBackground()', 'onChatForeground()'),
            ("document.getElementById('subPageOverlay').classList.add('show')", "document.getElementById('subPageOverlay').classList.remove('show')"),
            ("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))", "delete document.hidden;document.dispatchEvent(new Event('visibilitychange'))"),
        ]:
            page.evaluate(background)
            page.wait_for_timeout(30)
            before = page.locator('#m_live .msg-content').inner_html()
            live['content'] = '# 恢复后继续\n\n```js\nconst unfinished = 1;\n'
            page.evaluate("text => updateStreamingAssistantMessage('live',text)", live['content'])
            page.clock.fast_forward(5000)
            assert page.locator('#m_live .msg-content').inner_html() == before
            page.evaluate(resume)
            page.wait_for_function('!chatSyncPending')
            page.wait_for_selector('#m_live pre code')
            assert 'const unfinished = 1;' in page.locator('#m_live pre code').text_content()
        live['content'] += '\nconsole.log(unfinished);\n```'
        page.evaluate("text => {updateStreamingAssistantMessage('live',text);ChatMarkdown.finish('live');streamingAiId=null;}", live['content'])
        assert 'console.log(unfinished);' in page.locator('#m_live pre code').text_content()
        messages.append(live)
        page.evaluate('mdObserver.disconnect()')
        checks.append('fifty_chunks_coalesce_into_at_most_twelve_paints_background_pauses_and_completion_flushes')

        page.evaluate("""async () => {
          await ChatApp.getModule('aiDomLoader').ensureLoaded();
          await ChatApp.getModule('whisperToyLoader').ensureLoaded();
          window.ControlRuntime = null;
          aiDomInitiativeStart = () => {};
          aiDomMode = true;
          await aiDomFireInitiative();
          aiDomMode = false;
          whisperMode = true; toyConnected = true;
          await whisperFireInit();
          whisperMode = false; toyConnected = false;
        }""")
        for mid in ('dom-initiative', 'whisper-initiative'):
            assert page.locator('#m_'+mid+' h1').inner_text() == '抱抱'
            assert page.locator('#m_'+mid+' pre code').text_content() == code_text
        checks.append('both_lazy_initiative_streams_use_the_same_renderer_and_complete_final_blocks')

        speech = page.evaluate("""() => {
          ttsEnabled = true; ttsVoiceId = 'fixture'; ttsQueue = [];
          const previous = playNextTTS; playNextTTS = () => {};
          ttsSpeak('# **晚安**\\n\\n- 好好休息\\n- [明天见](https://example.test)');
          const result = ttsQueue[0].text;
          ttsQueue = []; ttsEnabled = false; playNextTTS = previous;
          return result;
        }""")
        assert speech == '晚安\n好好休息\n明天见', speech
        checks.append('speech_extracts_readable_text_without_markdown_markers_or_link_targets')

        # Show the same short poster and reply at three widths, without real conversation text.
        poster = [message('poster-user', '# 今天我最大\n\n**今晚的安排：**\n\n1. 提前下班\n2. 好好吃饭\n3. 要一个很长的抱抱', 'user'),
                  message('poster-ai', '收到，**今天都听你的。**\n\n*把手递过来*，我们慢慢走。'),
                  message('poster-code', technical)]
        page.evaluate('items => { currentMessages = items; renderMessages(); }', poster)
        for width in (375, 768, 1440):
            page.set_viewport_size(dict(width=width, height=1000))
            page.evaluate("document.getElementById('messages').scrollTop=0")
            page.wait_for_timeout(100)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.md-document').evaluate_all('items => items.every(el => el.scrollWidth <= el.clientWidth + 1)')
            assert page.locator('#m_poster-user h1').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') >= 28
            page.screenshot(path=str(OUTPUT / f'chat-{width}.png'), animations='disabled')
        checks.append('poster_typography_and_internal_table_scrolling_fit_mobile_tablet_and_desktop')

        # Measure on a fresh page with the native performance clock, not the virtual stream clock.
        benchmark = context.new_page()
        benchmark.on('pageerror', lambda error: errors.append(str(error)))
        benchmark.set_viewport_size(dict(width=375, height=1000))
        benchmark.goto('http://markdown.test/chat', wait_until='load')
        benchmark.wait_for_function('chatInitialized && !chatSyncPending')
        benchmark.evaluate('items => { currentMessages = items; renderMessages(); }', poster)
        session = context.new_cdp_session(benchmark)
        session.send('Emulation.setCPUThrottlingRate', dict(rate=4))
        performance = benchmark.evaluate("""() => {
          const target = document.querySelector('#m_poster-code [data-msg-content]');
          const samples = {short:[], document:[], cached:[]};
          const doc = '# 一份小记录\\n\\n' + '- 保留一点自己的时间。\\n'.repeat(35)
            + '\\n```js\\n' + 'console.log("你好");\\n'.repeat(25) + '```';
          for (const kind of Object.keys(samples)) for (let i=0;i<40;i++) {
            const source = kind==='short' ? '**晚安**，明天见。 '+i : doc+(kind==='cached'?'':i);
            const start=performance.now(); const result=ChatMarkdown.prepare(source);
            target.innerHTML=result.html; void target.offsetHeight;
            samples[kind].push(performance.now()-start);
          }
          return Object.fromEntries(Object.entries(samples).map(([key, values]) => {
            values.sort((a,b)=>a-b); return [key, {p50_ms:values[20],p95_ms:values[38],max_ms:values[39]}];
          }));
        }""")
        session.send('Emulation.setCPUThrottlingRate', dict(rate=1))
        benchmark.close()
        assert performance['short']['p95_ms'] < 100, performance
        page.evaluate("selectConv('empty')")
        page.wait_for_function('currentMessages.length === 0')
        assert page.locator('.md-document, .msg-content').count() == 0
        assert not errors, errors
        result = dict(passed=checks, page_errors=errors, stream_chunks=50, stream_paints=paints,
                      performance_cpu_throttle=4, performance=performance,
                      real_model_calls=0, screenshots=str(OUTPUT))
        (OUTPUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()

"""Real pages, mocked traffic: lazy math, streaming, literal source and mobile layouts."""
import json
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('/tmp/obsidian-math')


def main():
    OUTPUT.mkdir(exist_ok=True)
    requests, errors, checks = [], [], []
    now = time.time()

    def message(mid, content, role='assistant'):
        return dict(id=mid, conv_id='c1', role=role, content=content,
                    created_at=now, attachments=[])

    literal = r'今天花了 $20，明天 $30；转义金额 \$40。' + '\n\n`$x^2$`\n\n```tex\n\\[x^2\\]\n$$a_b$$\n```'
    messages = [message('plain', literal)]
    cover = [{'type': 'text', 'text': r'给你留一道小题：$a^2+b^2=c^2$。'}]
    body = [{'type': 'text', 'text': '# 今晚的公式\n\n' + r'\[\begin{pmatrix}1&2\\3&4\end{pmatrix}\]' + '\n\n答案藏在 $x^2$ 里。'}]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=390, height=844), is_mobile=True, service_workers='block')
        context.add_init_script("""
          localStorage.setItem('obsidian_tts_enabled','false');
          Object.defineProperty(navigator,'clipboard',{configurable:true,value:{
            writeText:async text=>{window.copiedText=text;}
          }});
        """)
        context.route_web_socket('**/ws', lambda socket: socket.on_message(
            lambda text: socket.send('{"type":"pong"}') if json.loads(text).get('type') == 'ping' else None))

        def route_request(route):
            path = urlparse(route.request.url).path
            requests.append(path)
            if path in ('/chat', '/heart-whispers'):
                route.fulfill(path=ROOT / ('static' + path + '.html'), content_type='text/html; charset=utf-8')
            elif path.startswith('/static/'):
                file = ROOT / path.lstrip('/')
                if file.is_file():
                    route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                else:
                    route.fulfill(status=404, body='missing local asset')
            elif path in ('/api/avatar/ai', '/api/avatar/user'):
                route.fulfill(path=ROOT.parent / 'public/optimized' / ('AIIcon.png' if path.endswith('/ai') else 'UserIcon.png'), content_type='image/png')
            elif path.startswith('/api/'):
                data = {}
                if path == '/api/worldbook': data = dict(ai_name='予安', user_name='南枝')
                elif path == '/api/models': data = [dict(key='fixture', audio_input=False)]
                elif path == '/api/chat/daily': data = dict(enabled=False, count_initialized=True, inherit_count=30, revision=0, timezone='Asia/Shanghai', next_rollover_at=now+86400)
                elif path == '/api/conversations': data = [dict(id='c1', title='今晚', model='fixture')]
                elif path == '/api/conversations/c1/messages': data = messages
                elif path == '/api/avatar/info': data = dict(ai=0, user=0)
                elif path == '/api/tts/voices': data = dict(voices=[])
                elif path == '/api/heart-whispers': data = dict(items=[dict(id='math_work', kind='work', cover=cover, created_at=now)], total=1)
                elif path == '/api/heart-whispers/works/math_work': data = dict(id='math_work', cover=cover, body=body, published_at=now)
                elif path == '/api/schedule/missed-recent': data = dict(count=0, items=[])
                elif path.endswith('/stream-status'): data = dict(streaming=False)
                route.fulfill(json=data)
            else:
                route.fulfill(status=204, body='')

        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://math.test/chat', wait_until='load')
        page.wait_for_function('chatInitialized && !chatSyncPending')
        page.wait_for_timeout(150)
        assert not any('/vendor/katex/' in path for path in requests), requests
        assert page.locator('#m_plain .md-math').count() == 0
        assert page.locator('#m_plain code').first.inner_text() == '$x^2$'
        assert r'\[x^2\]' in page.locator('#m_plain pre').inner_text()
        checks.append('ordinary_text_currency_escapes_and_code_do_not_load_or_invoke_math')

        rich = '行内 $E=mc^2$，另一个 ' + r'\(\frac{a}{b}\)' + '。\n\n$$\n' + r'\sum_{n=1}^{\infty}\frac{1}{n^2}=\frac{\pi^2}{6}' + '\n$$\n\n' + r'\[\begin{aligned}a&=b+c\\d&=e+f\end{aligned}\]'

        def show(items):
            page.evaluate('items => {currentMessages=items;renderMessages();}', items)

        def settled(target=page):
            target.wait_for_function("document.querySelector('.md-math-ready') && !document.querySelector('.md-math[data-math-pending]')")

        show([message('formula', rich), message('user', rich, 'user')])
        settled()
        assert page.locator('#m_formula .katex').count() == 4
        assert page.locator('#m_user .katex').count() == 4
        assert page.locator('#m_formula math').count() == 4
        assert page.locator('#m_formula .md-math-display').count() == 2
        assert requests.count('/static/vendor/katex/katex.min.js') == 1
        assert requests.count('/static/vendor/katex/katex.min.css') == 1
        assert any(path.endswith('.woff2') for path in requests)
        page.evaluate("copyMsg('formula')")
        assert page.evaluate('copiedText') == rich
        page.evaluate("editMsg('user')")
        assert page.locator('#edit_user').input_value() == rich
        page.evaluate("cancelEdit('user')")
        assert page.evaluate('text => ChatMarkdown.plainText(text)', rich) == '行内 公式，另一个 公式。\n\n公式\n\n公式'
        checks.append('four_delimiters_render_on_both_sides_with_accessible_math_and_original_copy_edit_source')

        page.evaluate("""() => {
          window.mathRenders=0;const render=katex.renderToString;
          katex.renderToString=(...args)=>{mathRenders++;return render(...args);};
          window.savedRow=document.getElementById('m_formula');
          renderMessages();
        }""")
        assert page.evaluate("savedRow===document.getElementById('m_formula')")
        page.evaluate('text=>{document.createElement("div").innerHTML=ChatMarkdown.renderDocument(text);}', rich + '\n\n新的一句。')
        assert page.evaluate('mathRenders') == 0
        checks.append('unchanged_rows_and_already_rendered_formulas_are_reused')

        live = message('live', '慢慢写。')
        show([live])
        page.evaluate("streamingAiId='live'")
        unfinished = '来算一算：\n\n' + r'\[\frac{1}{1+x}'
        page.evaluate("text=>updateStreamingAssistantMessage('live',text)", unfinished)
        page.wait_for_function("document.querySelector('#m_live .md-math-source')")
        assert r'\[\frac{1}{1+x}' in page.locator('#m_live .msg-content').inner_text()
        assert page.locator('#m_live .katex').count() == 0
        complete = unfinished + r'\]'
        page.evaluate("text=>{updateStreamingAssistantMessage('live',text);ChatMarkdown.finish('live');streamingAiId=null;}", complete)
        settled()
        assert page.locator('#m_live .katex').count() == 1
        # A pending stream stays untouched while its overlay is open, then catches up.
        page.evaluate("document.getElementById('subPageOverlay').classList.add('show')")
        before = page.locator('#m_live .msg-content').inner_html()
        page.evaluate("text=>updateStreamingAssistantMessage('live',text)", '$$x_{fresh}=3$$')
        page.wait_for_timeout(200)
        assert page.locator('#m_live .msg-content').inner_html() == before
        page.evaluate("document.getElementById('subPageOverlay').classList.remove('show')")
        page.wait_for_function("document.querySelector('#m_live .md-math-ready')?.dataset.mathSource==='x_{fresh}=3'")
        page.evaluate("ChatMarkdown.finish('live');streamingAiId=null")
        checks.append('unfinished_stream_keeps_source_completion_renders_and_background_work_resumes')

        bad = r'$\definitelyUnsupported{x}$' + '\n\n' + r'\[\href{javascript:alert(1)}{click}\]' + '\n\n' + r'\[\includegraphics{https://evil.test/pixel}\]' + '\n\n' + r'\[\htmlClass{injected}{x}\]' + '\n\n' + r'$\text{<img src=x onerror=alert(1)>}$'
        show([message('invalid', bad), message('valid', '$x^2$')])
        settled()
        assert page.locator('#m_invalid .md-math-source').first.inner_text() == r'$\definitelyUnsupported{x}$'
        assert page.locator('#m_invalid .msg-content').locator('script, img, a, [onerror], .injected').count() == 0
        assert '/pixel' not in requests
        assert page.locator('#m_valid .katex').count() == 1
        checks.append('invalid_formula_falls_back_locally_and_tex_cannot_create_trusted_html_or_fetch_images')

        # Keep the visible message anchored when an earlier formula grows after lazy rendering.
        history = [message(f'h{i}', '之前的一句话。\n\n另一句话。') for i in range(45)]
        show(history)
        page.evaluate("document.getElementById('messages').scrollTop=document.getElementById('m_h20').offsetTop")
        anchor_before = page.locator('#m_h20').bounding_box()['y']
        page.evaluate("""source => {
          currentMessages[0].content=source;
          renderMessages({scroll:'preserve'});
        }""", r'$$\begin{pmatrix}1&2\\3&4\end{pmatrix}$$')
        settled()
        assert abs(page.locator('#m_h20').bounding_box()['y'] - anchor_before) <= 2
        checks.append('async_math_preserves_the_reading_position_in_history')

        long_formula = r'\[\sum_{i=1}^{n}x_i = ' + '+'.join(r'\frac{a_{%d}}{b_{%d}}' % (i, i) for i in range(18)) + r'\]'
        poster = [message('poster-user', '今天想学一点数学，陪我算算？', 'user'),
                  message('poster', '当然。先从一个小小的等式开始：$E=mc^2$。\n\n' + r'\[\int_0^1 x^2\,\mathrm{d}x=\frac{1}{3}\]' + '\n\n长一点的式子，可以左右滑动：\n\n' + long_formula)]
        show(poster)
        settled()
        page.evaluate('document.fonts.ready')
        for width in (375, 768, 1440):
            page.set_viewport_size(dict(width=width, height=1000))
            page.evaluate("document.getElementById('messages').scrollTop=0")
            page.wait_for_timeout(100)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert page.locator('.md-document').evaluate_all('items=>items.every(el=>el.scrollWidth<=el.clientWidth+1)')
            formula = page.locator('#m_poster .md-math-display').last
            assert formula.evaluate('el=>el.scrollWidth>el.clientWidth+100')
            formula.evaluate('el=>el.scrollLeft=0')
            assert formula.evaluate('el=>el.querySelector(".katex-html").getBoundingClientRect().left>=el.getBoundingClientRect().left-1')
            formula.evaluate('el=>el.scrollLeft=120')
            assert formula.evaluate('el=>el.scrollLeft') >= 100
            page.screenshot(path=str(OUTPUT / f'chat-{width}.png'), animations='disabled')
        checks.append('mobile_tablet_desktop_keep_long_math_inside_horizontally_scrollable_rows')

        heart = context.new_page()
        heart.on('pageerror', lambda error: errors.append(str(error)))
        heart.goto('http://math.test/heart-whispers', wait_until='load')
        settled(heart)
        assert heart.locator('.hw-card .katex').count() == 1
        heart.locator('[data-id="math_work"] .hw-open').click()
        heart.get_by_role('heading', name='今晚的公式').wait_for()
        settled(heart)
        assert heart.locator('.hw-reader .katex').count() == 2
        assert heart.evaluate('document.documentElement.scrollWidth<=innerWidth')
        heart.screenshot(path=str(OUTPUT / 'heart-mobile.png'), animations='disabled')
        checks.append('long_heart_whisper_covers_and_reader_share_math_rendering')

        failed = context.new_page()
        failed.on('pageerror', lambda error: errors.append(str(error)))
        failed.route('**/vendor/katex/**', lambda route: route.abort())
        failed.goto('http://math.test/chat', wait_until='load')
        failed.wait_for_function('chatInitialized && !chatSyncPending')
        failed.evaluate('item=>{currentMessages=[item];renderMessages();}', message('fallback', r'普通文字 $x^2$ 仍然能读。'))
        failed.wait_for_timeout(200)
        assert failed.locator('#m_fallback .msg-content').inner_text() == '普通文字 $x^2$ 仍然能读。'
        assert failed.locator('.katex').count() == 0
        assert failed.locator('#input').is_enabled()
        checks.append('resource_load_failure_leaves_readable_source_and_usable_chat')
        assert not errors, errors
        result = dict(passed=checks, page_errors=errors, real_model_calls=0, screenshots=str(OUTPUT))
        (OUTPUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()

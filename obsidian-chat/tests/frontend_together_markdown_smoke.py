"""Exercise the actual offline Android transcript with a fake native UI bridge.

Run with .venv/bin/python; no backend, model calls, or real session writes.
"""
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / 'ObsidianApp/app/src/main/assets/together'
WEB = ROOT / 'obsidian-chat/static'
ORIGIN = 'https://appassets.androidplatform.net'


def message(mid, text, role='assistant', **extra):
    return dict(id=mid, content=text, role=role, **(dict(status='complete') | extra))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('/tmp/together-markdown-preview'))
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    errors, requests, checks = [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=256, height=126), device_scale_factor=3)
        context.add_init_script('''
          window.nativeCalls = [];
          window.TogetherNative = Object.fromEntries(['copy', 'retry', 'following', 'openLink']
            .map(method => [method, (...args) => nativeCalls.push([method, ...args])]));
        ''')

        def route_request(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != 'appassets.androidplatform.net':
                requests.append(route.request.url)
                route.abort()
                return
            path = parsed.path.removeprefix('/assets/together/')
            if parsed.path == '/preview.html':
                route.fulfill(path=output / 'preview.html', content_type='text/html; charset=utf-8')
                return
            if path.startswith('shared/'):
                name = Path(path).name
                file = WEB / ('js/chat/markdown.js' if name == 'markdown.js' else 'vendor/' + name)
            else:
                file = ASSETS / path
            if file.is_file():
                route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
            else:
                route.fulfill(status=404, body='missing local asset')

        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(ORIGIN + '/assets/together/index.html', wait_until='load')
        page.wait_for_function('window.TogetherTranscript')

        def update(**patch):
            page.evaluate('patch => TogetherTranscript.update(patch)', patch)
            page.wait_for_timeout(40)

        def position():
            return page.evaluate('''() => {
              const s=document.querySelector('#transcript'), top=s.getBoundingClientRect().top;
              const r=[...document.querySelectorAll('.message')].find(r=>r.getBoundingClientRect().bottom>top);
              return {key:r?.dataset.key, offset:r?.getBoundingClientRect().top-top, y:s.scrollTop};
            }''')

        def bottom():
            page.evaluate('TogetherTranscript.bottom()')
            page.wait_for_timeout(40)

        plain = '好啊，**慢慢走**。\n\n想吃什么？\n我们路上慢慢挑。'
        messages = [message('owner', '下课了，陪我走一会儿吧。', 'user'), message('reply', plain)]
        update(aiName='予安', userName='南枝', messages=messages)
        reply = page.locator('[data-key="message:reply"]')
        assert reply.locator('.msg-bubble').count() == 2
        assert reply.locator('strong').inner_text() == '慢慢走'
        assert reply.locator('br').count() == 1
        assert reply.get_attribute('aria-label').startswith('予安：')
        assert page.locator('.owner').get_attribute('aria-label').startswith('南枝：')
        assert page.evaluate('TogetherTranscript.plainText("**想你**。\\n\\n*抱一下*")') == '想你。\n抱一下'
        reply.locator('.msg-bubble').last.dispatch_event('contextmenu')
        assert page.evaluate('nativeCalls.filter(c=>c[0]==="copy").at(-1)[1]') == plain
        checks.append('paragraph_bubbles_single_newline_runtime_names_plain_peek_and_copy_whole_source')
        page.screenshot(path=str(output / 'paragraphs.png'), omit_background=True)

        rich = '### 路上的小事\n\n> 今天不赶时间。\n\n- 看看晚霞\n- *吹吹风*\n\n[那家小店](https://example.test/cafe)'
        code = '今晚：散步 → 买热茶 → 沿着河边慢慢走回家\n'
        technical = '```text\n' + code + '```\n\n| 时刻 | 地点 | 记一下 |\n| --- | --- | --- |\n| 傍晚 | 街角的咖啡店 | 隔着玻璃看见一只猫 |'
        update(messages=[message('rich', rich), message('technical', technical)])
        assert page.locator('blockquote').inner_text() == '今天不赶时间。'
        assert page.locator('li').count() == 2
        assert page.locator('em').inner_text() == '吹吹风'
        assert page.locator('h3').evaluate('el=>parseFloat(getComputedStyle(el).fontSize)') < 15
        page.locator('a').click()
        assert page.evaluate('nativeCalls.filter(c=>c[0]==="openLink").at(-1)') == ['openLink', 'https://example.test/cafe']
        page.locator('[data-md-copy]').click()
        assert page.evaluate('nativeCalls.filter(c=>c[0]==="copy").at(-1)[1]') == code
        assert page.locator('.md-table-wrap').evaluate('el=>el.scrollWidth>el.clientWidth')
        assert page.locator('pre').evaluate('el=>el.scrollWidth>el.clientWidth')
        assert page.locator('#transcript').evaluate('el=>el.scrollWidth===el.clientWidth')
        checks.append('compact_rich_blocks_local_code_copy_external_link_bridge_horizontal_overflow')

        # A failed picture and an offline draft retain their exact retry identities.
        update(messages=[message('failed', '**晚霞**', status='failed', capture_id='frame')],
               outbox=[dict(client_id='draft', text='写好了', error='未发送', capture_id='draft-image')])
        assert page.locator('.picture-tag').count() == 2
        for identity in ('failed', 'outbox:draft'):
            page.locator(f'[data-retry="{identity}"]').click()
            assert page.evaluate('nativeCalls.filter(c=>c[0]==="retry").at(-1)[1]') == identity
        checks.append('image_labels_and_server_outbox_retries_preserved')

        unsafe = '<script>window.bad=true</script>\n\n[x](javascript:alert(1)) ![原图](https://external.test/photo.jpg)'
        update(messages=[message('unsafe', unsafe)], outbox=[])
        assert not page.evaluate('window.bad')
        assert page.locator('.message script, .message img, a[href^="javascript:"]').count() == 0
        assert '原图' in page.locator('.message').inner_text()
        assert not requests
        checks.append('markdown_cannot_execute_html_or_load_external_images')

        update(messages=[], stream='嗯，**')
        for text in ['嗯，**我听着呢**。', '嗯，**我听着呢**。\n\n不用急，', '嗯，**我听着呢**。\n\n不用急，慢慢说。']:
            update(stream=text)
        html = page.locator('[data-key="stream"]').inner_html()
        before = page.locator('#rows').bounding_box()['height']
        update(stream='', messages=[message('finished', text)])
        assert page.locator('.message').count() == 1
        assert page.locator('.message').inner_html() == html
        assert abs(page.locator('#rows').bounding_box()['height'] - before) < 1
        checks.append('streaming_and_finished_reply_share_identical_layout')

        history = [message(str(i), f'第 {i} 段。\n\n慢慢走，想到什么就聊什么。') for i in range(30)]
        update(messages=history)
        page.locator('#transcript').evaluate('el=>el.scrollTop=el.scrollHeight/2')
        page.wait_for_timeout(40)
        old = position()
        page.evaluate('window.oldRow=document.querySelector(\'[data-key="message:15"]\')')
        update(messages=history[2:] + [message('new', plain)], stream='正在写下一句…')
        after = position()
        assert after['key'] == old['key'] and abs(after['offset'] - old['offset']) < 1, (old, after)
        assert page.evaluate('oldRow===document.querySelector(\'[data-key="message:15"]\')')
        # Reparenting/resizing into the full room must not send a reader to the end.
        page.set_viewport_size(dict(width=360, height=300))
        page.wait_for_timeout(80)
        resized = position()
        assert resized['key'] == old['key'] and abs(resized['offset'] - old['offset']) < 1, (old, resized)
        page.locator('#transcript').evaluate('el=>el.style.display="none"')
        update(stream='折叠期间继续写。\n\n还有下一段。')
        page.locator('#transcript').evaluate('el=>el.style.display=""')
        page.wait_for_timeout(80)
        restored = position()
        assert restored['key'] == resized['key'] and abs(restored['offset'] - resized['offset']) < 1
        bottom()
        update(stream='现在接着聊。\n\n读到最新消息时，自动跟随到这里。')
        assert page.locator('#transcript').evaluate('el=>el.scrollHeight-el.scrollTop-el.clientHeight') < 1
        checks.append('scrolled_reader_keeps_anchor_on_updates_pruning_resize_hide_restore_then_can_follow')

        update(messages=[message('rich', rich)], stream='')
        page.set_viewport_size(dict(width=256, height=126))
        bottom()
        page.screenshot(path=str(output / 'rich.png'), omit_background=True)
        if (output / 'preview.html').exists():
            page.set_viewport_size(dict(width=750, height=615))
            page.goto(ORIGIN + '/preview.html', wait_until='load')
            page.wait_for_function('[...document.querySelectorAll("iframe")].every(f=>f.dataset.ready)')
            page.screenshot(path=str(output / 'preview.png'), full_page=True)
            page.locator('#stream').click()
            page.wait_for_timeout(3000)
            assert page.frames[1].locator('.message').last.locator('.msg-bubble').count() == 2
        assert not errors, errors
        assert not requests, requests
        browser.close()
    result = dict(checks=checks, javascript_errors=errors, external_requests=requests)
    (output / 'checks.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

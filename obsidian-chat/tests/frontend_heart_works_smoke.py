"""Actual archive page: separate long/short lists, saved reading and explicit audio."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def assert_image_description(page, scope, description):
    image = page.locator(scope).locator('img')
    assert image.count() == 1, scope
    image.dispatch_event('error')
    assert not page.evaluate('Boolean(window.__heartImageInjected)'), scope
    assert image.get_attribute('alt') == description, scope
    assert image.get_attribute('onerror') is None, scope
    assert image.get_attribute('data-injected') is None, scope


def main():
    requests, errors, sockets = [], [], []
    cover = [{'type': 'text', 'text': '今晚给你看点刺激的'}]
    body = [{'type': 'text', 'text': '# 戒色倡议书\n先好好睡觉。'},
            {'type': 'image', 'url': '/api/heart-whispers/works/hw_demo/assets/picture', 'description': '一张草图'},
            {'type': 'music', 'url': '/api/music/stream/123', 'title': '雨夜', 'artist': '某位歌手'}]
    items = [{'id': 'hw_demo', 'kind': 'work', 'cover': cover, 'created_at': 100},
             {'id': 'hw_old', 'kind': 'legacy', 'content': '原来的小心语', 'created_at': 99},
             {'id': 'hw_older', 'kind': 'work', 'cover': [{'type': 'text', 'text': '较早留下的长心语'}], 'created_at': 50}]
    items.extend({'id': f'short_{index}', 'kind': 'legacy', 'content': f'日常小心语 {index}', 'created_at': 98 - index}
                 for index in range(20))
    held, hold = [], {'kind': None}

    def listing(url):
        query = parse_qs(urlparse(url).query)
        kind = query.get('kind', ['legacy'])[0]
        records = sorted((item for item in items if item['kind'] == kind), key=lambda item: item['created_at'], reverse=True)
        page, size = int(query.get('page', ['1'])[0]), int(query.get('page_size', ['20'])[0])
        return {'items': records[(page - 1) * size:page * size], 'total': len(records), 'page': page, 'page_size': size}

    def push(event_type, item):
        items.append(item)
        sockets[-1].send(json.dumps({'type': event_type, 'data': item}))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 390, 'height': 844}, service_workers='block')
        context.route_web_socket('**/ws', lambda socket: sockets.append(socket))
        def route_request(route):
            parsed = urlparse(route.request.url)
            path = parsed.path
            requests.append(path)
            if path == '/heart-whispers':
                route.fulfill(path=str(ROOT / 'static/heart-whispers.html'), content_type='text/html')
            elif path == '/home':
                route.fulfill(path=str(ROOT / 'static/home.html'), content_type='text/html')
            elif path == '/api/heart-whispers/works-status':
                route.fulfill(json={'latest': 102})
            elif path == '/api/heart-whispers':
                if hold['kind'] == parse_qs(parsed.query).get('kind', ['legacy'])[0]:
                    held.append(route)
                    hold['kind'] = None
                else:
                    route.fulfill(json=listing(route.request.url))
            elif path.startswith('/api/heart-whispers/') and route.request.method == 'DELETE':
                items[:] = [item for item in items if item['id'] != path.rsplit('/', 1)[-1]]
                route.fulfill(json={'ok': True})
            elif path == '/api/heart-whispers/works/hw_demo':
                route.fulfill(json={'id': 'hw_demo', 'cover': cover, 'body': body, 'published_at': 100})
            elif path == '/api/heart-whispers/works/hw_demo/assets/picture':
                route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="180"><rect width="300" height="180" fill="#ccbbaa"/></svg>')
            elif path.startswith('/static/') and (ROOT / path.lstrip('/')).is_file():
                route.fulfill(path=str(ROOT / path.lstrip('/')), content_type=mimetypes.guess_type(path)[0] or 'application/octet-stream')
            elif path.startswith('/api/'):
                route.fulfill(json={'user_name': '小栀', 'ai_name': '阿澈'} if path == '/api/worldbook' else {})
            else:
                route.fulfill(status=204)
        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        page.goto('http://heart.test/heart-whispers')
        page.get_by_text('今晚给你看点刺激的').wait_for()
        assert page.get_by_role('tab', name='长心语').get_attribute('aria-selected') == 'true'
        assert page.get_by_text('较早留下的长心语').is_visible()
        assert not page.get_by_text('原来的小心语').count()
        assert page.locator('.hw-stats').inner_text() == '共 2 篇长心语'
        assert not page.get_by_text('戒色倡议书').count()
        assert not page.locator('audio').count() and not any('/assets/' in request for request in requests)
        page.screenshot(path='/tmp/heart-works-long-mobile.png', full_page=True)
        page.get_by_role('tab', name='短心语').click()
        page.get_by_text('原来的小心语').wait_for()
        assert not page.locator('.hw-open').count()
        assert page.locator('.hw-card').count() == 20
        page.get_by_role('button', name='加载更多', exact=True).click()
        page.wait_for_function("document.querySelectorAll('.hw-card').length === 21")
        assert page.locator('.hw-stats').inner_text() == '共 21 条短心语'
        assert not page.get_by_role('button', name='加载更多', exact=True).count()
        page.locator('[data-id="hw_old"] .hw-del').click()
        page.wait_for_function("!document.querySelector('[data-id=hw_old]')")
        assert page.locator('.hw-stats').inner_text() == '共 20 条短心语'
        page.screenshot(path='/tmp/heart-works-short-mobile.png', full_page=True)
        assert sockets
        push('heart_work_published', {'id': 'hw_other', 'kind': 'work', 'cover': [{'type': 'text', 'text': '另一份长心语'}], 'created_at': 101})
        push('heart_whisper', {'id': 'hw_live', 'kind': 'legacy', 'content': '刚刚留下的短心语', 'created_at': 102})
        page.get_by_text('刚刚留下的短心语').wait_for()
        assert not page.get_by_text('另一份长心语').count()
        assert page.evaluate("localStorage.getItem('heartWorksSeenAt')") == '100'
        page.get_by_role('tab', name='短心语').press('ArrowLeft')
        page.get_by_text('另一份长心语').wait_for()
        assert page.get_by_role('tab', name='长心语').evaluate('tab => tab === document.activeElement')
        assert not page.get_by_text('刚刚留下的短心语').count()
        # A late response from the previous category cannot overwrite the selected list.
        hold['kind'] = 'legacy'
        page.get_by_role('tab', name='短心语').click()
        page.get_by_text('正在加载短心语…').wait_for()
        page.get_by_role('tab', name='长心语').click()
        page.get_by_text('另一份长心语').wait_for()
        assert len(held) == 1
        delayed = held.pop()
        with page.expect_response(delayed.request.url):
            delayed.fulfill(json=listing(delayed.request.url))
        assert page.get_by_text('另一份长心语').is_visible()
        assert not page.get_by_text('刚刚留下的短心语').count()
        page.locator('[data-id="hw_demo"] .hw-open').click()
        page.get_by_role('heading', name='戒色倡议书').wait_for()
        assert page.locator('audio').get_attribute('preload') == 'none'
        assert page.locator('audio').evaluate('(audio) => audio.paused && !audio.autoplay')
        assert '/api/music/stream/123' not in requests
        assert page.locator('.hw-work-image').count() == 1
        push('heart_whisper', {'id': 'hw_read_short', 'kind': 'legacy', 'content': '阅读时的短心语', 'created_at': 103})
        push('heart_work_published', {'id': 'hw_read_long', 'kind': 'work', 'cover': [{'type': 'text', 'text': '阅读时的新长心语'}], 'created_at': 104})
        page.wait_for_function("allItems.some(item => item.id === 'hw_read_long')")
        assert page.get_by_role('heading', name='戒色倡议书').is_visible()
        page.screenshot(path='/tmp/heart-works-reading-mobile.png', full_page=True)
        page.get_by_role('button', name='返回心语').click()
        assert not page.locator('audio').count()
        assert page.get_by_text('今晚给你看点刺激的').is_visible()
        assert page.get_by_text('阅读时的新长心语').is_visible()
        assert not page.get_by_text('阅读时的短心语').count()
        # An image-only cover stays image-only at desktop width too.
        page.evaluate("""() => {
          allItems.push({id:'hw_cover', kind:'work', created_at:102,
            cover:[{type:'image', url:'/api/heart-whispers/works/hw_demo/assets/picture', description:'封面草图'}]});
          renderList();
        }""")
        page.set_viewport_size({'width': 1280, 'height': 900})
        image_cover = page.locator('[data-id="hw_cover"] .hw-open')
        assert image_cover.inner_text() == '' and image_cover.locator('img').count() == 1
        assert not page.get_by_text('戒色倡议书').count()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path='/tmp/heart-works-covers-desktop.png', full_page=True)
        # Descriptions stay literal data in both the cover and the saved body.
        for description in (
            '草图" onerror="window.__heartImageInjected = true" data-injected="',
            '写着 "晚安" 与 <星星> & \'月亮\' 的草图',
            '草图"><img src=x onerror="window.__heartImageInjected = true">',
        ):
            body[1]['description'] = description
            page.evaluate("""description => {
              allItems.find(item => item.id === 'hw_cover').cover[0].description = description;
              renderList();
            }""", description)
            assert_image_description(page, '[data-id="hw_cover"] .hw-open', description)
            page.locator('[data-id="hw_demo"] .hw-open').click()
            page.get_by_role('heading', name='戒色倡议书').wait_for()
            assert_image_description(page, '.hw-reader', description)
            page.get_by_role('button', name='返回心语').click()
        home = context.new_page()
        home.on('pageerror', lambda error: errors.append(str(error)))
        home.goto('http://heart.test/home')
        home.locator('[data-heart-archive].heart-work-unread').wait_for()
        page.evaluate("localStorage.setItem('heartWorksSeenAt', '102')")
        home.wait_for_function("!document.querySelector('[data-heart-archive]').classList.contains('heart-work-unread')")
        assert not home.locator('audio').count()
        assert all('/send' not in request and '/generate' not in request for request in requests)
        assert not errors, errors
        browser.close()
    print('heart works browser checks passed: separate tabs/pagination/delete/realtime/tab races/keyboard/cover/read/manual audio/unread/image attribute safety; /tmp/heart-works-long-mobile.png; /tmp/heart-works-short-mobile.png; /tmp/heart-works-reading-mobile.png; /tmp/heart-works-covers-desktop.png')


if __name__ == '__main__':
    main()

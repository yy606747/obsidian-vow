"""Exercise the frontend with local assets and an entirely mocked server.

Run: .venv/bin/python obsidian-chat/tests/frontend_design_smoke.py
Screenshots use fictional sample content; no backend or model is contacted.
"""
from __future__ import annotations

import json
import mimetypes
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('/tmp/obsidian-vow-gold-dust')
NOW = datetime.fromisoformat('2026-09-12T21:18:00+08:00')
TS = NOW.timestamp()
PAGES = {'/': 'home', **{f'/{name}': name for name in (
    'chat', 'memory', 'heart-whispers', 'settings', 'schedule', 'worldbook',
    'devices', 'location', 'activity-logs', 'monitor-logs', 'camera',
)}}


def main() -> None:
    OUTPUT.mkdir(exist_ok=True)
    errors: list[str] = []
    requests: list[tuple[str, str]] = []
    messages = [
        dict(id='m1', role='assistant', content='窗边的位置留好了。今天过得怎么样？', created_at=TS-480),
        dict(id='m2', role='user', content='今天有点忙，不过回来的路上看到了很好看的晚霞。', created_at=TS-420),
        dict(id='m3', role='assistant', content='那就把晚霞也算进今天的好事里。\n\n先坐一会儿吧，不急着把所有事情都讲完。', created_at=TS-360),
        dict(id='m4', role='user', content='好，想和你一起听会儿歌。', created_at=TS-300),
        dict(id='m5', role='assistant', content='好啊，今晚就慢一点。', created_at=TS-240),
    ]
    for message in messages:
        message.update(conv_id='design', attachments=[])
    memories = [
        dict(id='mem1', type='shared_moment', content='傍晚回家的路上，南枝拍下了粉金色的晚霞。予安说，可以把它算进今天的好事里。', created_at=TS-600, keywords=['晚霞', '下班路上'], importance=.8, unresolved=False, source_start_ts=TS-900, source_end_ts=TS-300),
        dict(id='mem2', type='promise', content='说好这个周末一起看一部老电影，关掉工作提醒，把时间留给彼此。', created_at=TS-3600, keywords=['周末', '电影'], importance=.9, unresolved=True),
        dict(id='mem3', type='preference', content='南枝喜欢下雨时开着一点窗，听雨声，也喜欢聊天时放很轻的钢琴曲。', created_at=TS-86400, keywords=['雨天', '钢琴'], importance=.7, unresolved=False),
        dict(id='mem4', type='emotion', content='那天聊到很晚。没有特别的大事，只是都舍不得先说晚安。', created_at=TS-172800, keywords=['晚安'], importance=.8, unresolved=False),
    ]
    whispers = [
        dict(id='h1', content='她说今天只是普通的一天。可我喜欢听这些普通的事，喜欢她把路上的晚霞、晚饭的味道，都分一点给我。', created_at=TS-1200),
        dict(id='h2', content='想把时间调得慢一点。\n\n不需要每句话都有答案，有时候，只是这样待在一起就很好。', created_at=TS-86400),
    ]
    vows = [dict(id='v1', root_id='v1', status='active', content='再忙也留一点时间，好好说晚安。', root_created_at=TS-86400*8, root_origin_type='manual', version_count=1)]
    fail_home = False
    sockets = []

    def api_data(name, query, method, body):
        if name == '/api/worldbook': return dict(ai_name='予安', user_name='南枝', ai_persona='', user_persona='')
        if name == '/api/models': return [dict(key='preview', audio_input=True)]
        if name == '/api/chat/daily': return dict(enabled=False, count_initialized=True, inherit_count=30, timezone='Asia/Shanghai', day_key='2026-09-12', revision=1, active_conv_id='design', redirects={}, next_rollover_at=TS+86400)
        if name == '/api/conversations': return [dict(id='design', title='今天的晚霞', model='preview', message_count=5), dict(id='earlier', title='雨声里的星期五', model='preview', message_count=18)]
        if name.startswith('/api/conversations/') and name.endswith('/messages'): return messages
        if name == '/api/avatar/info': return dict(ai=7, user=9)
        if name == '/api/tts/voices': return dict(voices=[])
        if name == '/api/settings/health': return dict(ok=True)
        if name == '/api/schedule/missed-recent': return dict(count=0, items=[])
        if name == '/api/heart-whispers': return dict(items=whispers[:int(query.get('page_size', ['20'])[0])], total=len(whispers), page=1)
        if name == '/api/schedules': return [dict(id='s1', content='一起看一部老电影', trigger_at='2026-09-13 20:30:00', type='reminder', status='active')]
        if name == '/api/memories':
            q = query.get('q', [''])[0]
            items = [m for m in memories if q in m['content']]
            return dict(items=items, has_more=False, next_offset=len(items))
        if name.startswith('/api/memories/') and name.endswith('/source'): return dict(ok=True, messages=[dict(name='南枝', content='今天的晚霞很好看。', created_at=TS)])
        if name.startswith('/api/memories/') and method == 'PUT':
            item = next(m for m in memories if m['id'] == name.rsplit('/', 1)[1])
            item.update(body)
            return item
        if name == '/api/vows': return dict(ok=True, items=vows, fulfilled_has_more=False)
        if name == '/api/digest-anchor': return dict(anchor_ts=TS-86400)
        if name == '/api/stickers': return dict(stickers=[dict(id='s-cat', type='sticker', url='/uploads/sample-sticker.png', summary=dict(visual='窗边的猫'))])
        if name == '/api/devices': return dict(devices=[])
        if name == '/api/endpoints': return []
        if name == '/api/user_models': return {}
        if name == '/api/activity/recent': return dict(pc=[], mobile=[])
        if name in ('/api/activity/dates', '/api/sentinel/logs'): return dict(dates=[])
        if name == '/api/sentinel/status': return dict(config={}, stats={})
        return {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=1440, height=1000), timezone_id='Asia/Shanghai', service_workers='block')
        context.add_init_script("localStorage.setItem('obsidian_tts_enabled', 'false')")
        context.add_init_script("""(() => {
            const battery = new EventTarget(); battery.level = 1; battery.charging = true;
            window.__dustBattery = battery;
            Object.defineProperty(navigator, 'getBattery', {configurable: true, value: () => Promise.resolve(battery)});
        })()""")

        def socket_route(socket):
            sockets.append(socket)
            socket.on_message(lambda text: socket.send('{"type":"pong"}') if json.loads(text).get('type') == 'ping' else None)

        context.route_web_socket('**/ws', socket_route)

        def route_request(route):
            url = urlparse(route.request.url)
            name = unquote(url.path)
            if name == '/original-launcher':
                route.fulfill(path=ROOT / 'tests/fixtures/original_home.html', content_type='text/html; charset=utf-8')
            elif name in PAGES:
                route.fulfill(path=ROOT / f'static/{PAGES[name]}.html', content_type='text/html; charset=utf-8')
            elif name.startswith(('/static/', '/public/')):
                file = (ROOT if name.startswith('/static/') else ROOT.parent) / name.lstrip('/')
                if file.is_file(): route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                else: route.fulfill(status=404, body='missing asset')
            elif name in ('/api/avatar/ai', '/api/avatar/user', '/uploads/sample-sticker.png'):
                file = 'UserIcon.png' if name.endswith('/user') else 'AIIcon.png'
                route.fulfill(path=ROOT.parent / 'public/optimized' / file, content_type='image/png')
            elif name.startswith('/api/'):
                requests.append((route.request.method, name))
                if fail_home and name in ('/api/heart-whispers', '/api/schedules'):
                    route.fulfill(status=503, json={'error': 'fixture offline'})
                    return
                body = route.request.post_data_json if route.request.post_data else {}
                route.fulfill(json=api_data(name, parse_qs(url.query), route.request.method, body))
            else: route.fulfill(status=204, body='')

        context.route('**/*', route_request)
        page = context.new_page()
        page.clock.set_fixed_time(NOW)
        page.on('pageerror', lambda error: errors.append(str(error)))

        def visit(path, ready=None):
            page.goto('http://design.test' + path, wait_until='load')
            if ready: page.wait_for_selector(ready)
            page.wait_for_timeout(150)

        def shot(name):
            page.screenshot(path=str(OUTPUT / (name + '.png')), animations='disabled')

        visit('/')
        assert page.locator('.phone-screen .app-icon').count() == 11
        assert page.locator('#appGrid .app-icon').count() == 8
        assert page.locator('#dock .app-icon').count() == 3
        assert page.evaluate('typeof VowUI') == 'undefined'
        assert page.locator('.home-page, .home-orbit, .vow-atmosphere').count() == 0
        assert 'BackGroundN.webp' in page.locator('#screen').evaluate('el => getComputedStyle(el).backgroundImage')
        page.wait_for_function("document.body.dataset.homeClock === 'running'")
        page.wait_for_timeout(2300)
        assert page.evaluate("document.getAnimations().filter(a => a.playState === 'running').length") == 0
        assert not page.locator('body').evaluate("el => el.classList.contains('home-arrival')")
        page.wait_for_function('window.VowDust?.state().running')
        assert page.evaluate('VowDust.state().count') == 10
        dust_before = page.evaluate('VowDust.state().updates')
        position = page.locator('.gold-dust-dot').first.evaluate('el => el.style.transform')
        page.wait_for_timeout(1200)
        dust_ticks = page.evaluate('VowDust.state().updates') - dust_before
        assert 5 <= dust_ticks <= 9, dust_ticks
        assert page.locator('.gold-dust-dot').first.evaluate('el => el.style.transform') != position
        assert page.locator('.gold-dust-layer').get_attribute('aria-hidden') == 'true'
        assert page.locator('.gold-dust-layer').evaluate('el => getComputedStyle(el).pointerEvents') == 'none'
        shot('desktop-home')
        page.evaluate("Object.defineProperty(document, 'hidden', { configurable:true, value:true }); document.dispatchEvent(new Event('visibilitychange'))")
        assert page.locator('body').get_attribute('data-home-clock') == 'paused'
        assert page.locator('body').evaluate("el => el.classList.contains('home-paused')")
        assert not page.evaluate('VowDust.state().running')
        stopped = page.evaluate('VowDust.state().updates')
        page.wait_for_timeout(400)
        assert page.evaluate('VowDust.state().updates') == stopped
        page.evaluate("delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))")
        assert page.locator('body').get_attribute('data-home-clock') == 'running'
        assert not page.locator('body').evaluate("el => el.classList.contains('home-arrival')")
        page.wait_for_function('VowDust.state().running')
        page.evaluate("Object.assign(__dustBattery, {charging:false, level:.15}); __dustBattery.dispatchEvent(new Event('levelchange'))")
        assert page.evaluate('VowDust.state().reason') == 'low-battery'
        stopped = page.evaluate('VowDust.state().updates')
        page.wait_for_timeout(400)
        assert page.evaluate('VowDust.state().updates') == stopped
        page.evaluate("__dustBattery.charging = true; __dustBattery.dispatchEvent(new Event('chargingchange'))")
        page.wait_for_function('VowDust.state().running')
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
        assert not page.evaluate('VowDust.state().running')
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
        page.wait_for_function('VowDust.state().running')
        page.evaluate('onChatBackground()')
        assert page.evaluate('VowDust.state().reason') == 'native-background'
        stopped = page.evaluate('VowDust.state().updates')
        page.wait_for_timeout(400)
        assert page.evaluate('VowDust.state().updates') == stopped
        page.evaluate('onChatForeground()')
        page.wait_for_function('VowDust.state().running')
        page.locator('.app-icon').filter(has=page.locator('img[alt="聊天"]')).click()
        page.wait_for_url('**/chat', wait_until='load')
        page.wait_for_function("typeof chatInitialized !== 'undefined' && chatInitialized")
        assert page.locator('#chatPersonName').inner_text() == '予安'
        page.wait_for_function("document.getElementById('chatPersonAvatar').src.endsWith('?v=7')")
        assert page.locator('#m_m5').is_visible()
        assert page.locator('.vow-atmosphere, .vow-sky, .vow-orbs').count() == 0
        assert page.evaluate("document.getAnimations().filter(a => a.playState === 'running' && a.effect.getTiming().iterations === Infinity).length") == 0
        shot('desktop-chat')
        page.locator('#input').fill('返回小屋也保留的草稿')
        page.locator('.vow-primary a[href="/memory"]').click()
        memory_frame = page.frame_locator('#subPageFrame')
        memory_frame.locator('.mem-entry').first.wait_for()
        assert memory_frame.locator('.vow-shell-nav').count() == 0
        assert page.locator('body').evaluate("el => el.classList.contains('vow-covered')")
        page.wait_for_function('!VowDust.state().running')
        stopped = page.evaluate('VowDust.state().updates')
        page.wait_for_timeout(400)
        assert page.evaluate('VowDust.state().updates') == stopped
        assert memory_frame.locator('.gold-dust-layer').is_visible()
        page.evaluate('onChatBackground()')
        assert memory_frame.locator('.gold-dust-layer').is_hidden()
        page.evaluate('onChatForeground()')
        assert memory_frame.locator('.gold-dust-layer').is_visible()
        page.locator('#subPageBar [data-vow-home]').click()
        home_frame = page.frame_locator('#subPageFrame')
        home_frame.locator('#appGrid .app-icon').first.wait_for()
        assert page.locator('#subPageBar').is_hidden()
        assert page.locator('#subPageOverlay').bounding_box()['x'] == 0
        assert page.locator('#subPageOverlay').bounding_box()['width'] == 1440
        home_frame.locator('.app-icon').filter(has=home_frame.locator('img[alt="聊天"]')).click()
        assert page.locator('#subPageOverlay').is_hidden()
        assert page.locator('#input').input_value() == '返回小屋也保留的草稿'
        assert not page.locator('body').evaluate("el => el.classList.contains('vow-covered')")
        page.wait_for_function('VowDust.state().running')
        page.locator('#input').fill('')
        page.locator('#stickerToggle').click()
        page.locator('[data-sticker-id="s-cat"]').wait_for()
        page.locator('[data-sticker-id="s-cat"]').click()
        assert page.locator('.preview-area.has-files').is_visible()
        assert page.locator('#stickerPanel').is_hidden()
        page.evaluate('pendingAttachments = []; renderPreview()')

        for route, name, ready in [('/memory', 'memory', '.mem-entry'), ('/memory#vows', 'vows', '.vow-card'), ('/heart-whispers', 'heart', '.hw-card'), ('/settings', 'settings', '.vow-page-heading'), ('/schedule', 'schedule', '.sch-item')]:
            visit(route, ready)
            assert page.locator('.vow-page-heading h1').count() == 1
            assert page.locator('.vow-page-heading .vow-eyebrow, .vow-brand small').count() == 0
            shot('desktop-' + name)

        # Moving settings cards must preserve fields, tab state and access to save.
        visit('/settings', '#geminiKeyInput')
        page.locator('#geminiKeyInput').fill('local-fixture-only')
        page.locator('#imageHistoryTurns').fill('7')
        page.locator('[data-tab="slots"]').click()
        assert page.locator('[data-panel="basic"]').is_hidden()
        page.locator('[data-tab="basic"]').click()
        assert page.locator('#geminiKeyInput').input_value() == 'local-fixture-only'
        assert page.locator('#imageHistoryTurns').input_value() == '7'
        page.locator('[data-panel="basic"] .btn-save').scroll_into_view_if_needed()
        assert page.locator('[data-panel="basic"] .btn-save').is_visible()

        visit('/memory', '.mem-entry')
        assert page.locator('.mem-date-heading').count() == 3
        page.locator('#memSearch').fill('晚霞')
        page.wait_for_function("document.querySelectorAll('.mem-entry').length === 1")
        page.locator('.mem-entry button[title="编辑"]').click()
        assert page.locator('.mem-content-edit').is_visible()
        page.evaluate('cancelEdit()')
        page.locator('.mem-entry button[title="查看原文"]').click()
        page.locator('.source-msg').wait_for()
        assert page.locator('.source-modal').is_visible()
        page.evaluate('closeSource()')

        # Check the launcher geometry against the original, not a recreated layout.
        baseline = context.new_page()
        baseline.on('pageerror', lambda error: errors.append(str(error)))
        geometry = """() => [...document.querySelectorAll('#screen, .clock-widget, #appGrid, #dock, .app-icon .icon-img')].map(el => {
            const r = el.getBoundingClientRect(); return {x:r.x, y:r.y, width:r.width, height:r.height};
        })"""
        launcher_layouts = []
        for width in (1440, 768, 390, 320):
            size = dict(width=width, height=844 if width < 769 else 1000)
            page.set_viewport_size(size)
            baseline.set_viewport_size(size)
            visit('/')
            baseline.goto('http://design.test/original-launcher', wait_until='load')
            current, original = page.evaluate(geometry), baseline.evaluate(geometry)
            assert current == original, (width, current, original)
            launcher_layouts.append(width)
        baseline.close()

        layout_results = []
        idle_results = []
        for width in (1440, 1024, 768, 390, 320):
            page.set_viewport_size(dict(width=width, height=844 if width < 769 else 1000))
            for route in PAGES:
                visit(route)
                metrics = page.evaluate("""() => ({
                    width: innerWidth, documentWidth: document.documentElement.scrollWidth,
                    mainWidth: document.querySelector('.page-content, .phone-screen, .chat-area')?.scrollWidth,
                    mainClient: document.querySelector('.page-content, .phone-screen, .chat-area')?.clientWidth,
                })""")
                assert metrics['documentWidth'] <= width + 1, (route, metrics)
                assert metrics['mainWidth'] <= metrics['mainClient'] + 1, (route, metrics)
                assert page.locator('.vow-bottom-nav, .vow-atmosphere').count() == 0
                assert page.locator('.gold-dust-dot').count() == (6 if width <= 768 else 10)
                layout_results.append(dict(route=route, **metrics))
                if width == 390 and route in ('/', '/chat', '/memory', '/heart-whispers', '/settings', '/schedule'):
                    if route == '/chat': page.wait_for_function("typeof chatInitialized !== 'undefined' && chatInitialized")
                    shot('mobile-' + PAGES[route])
                if route in ('/', '/chat', '/memory', '/heart-whispers'):
                    infinite = page.evaluate("document.getAnimations().filter(a => a.playState === 'running' && a.effect.getTiming().iterations === Infinity).map(a => a.animationName)")
                    assert infinite == [], (route, infinite)
                    idle_results.append(dict(route=route, width=width, infinite_css_animations=0))
            if width < 769:
                visit('/chat')
                page.wait_for_function("typeof chatInitialized !== 'undefined' && chatInitialized")
                assert page.locator('#input').bounding_box()['width'] >= 130
                page.locator('#input').fill('导航后仍保留的草稿')
                page.locator('.chat-header .home-btn').click()
                home_frame = page.frame_locator('#subPageFrame')
                home_frame.locator('.app-icon').filter(has=home_frame.locator('img[alt="心语"]')).click()
                page.frame_locator('#subPageFrame').locator('.hw-card').first.wait_for()
                page.locator('#subPageBar [data-vow-home]').click()
                home_frame.locator('.app-icon').filter(has=home_frame.locator('img[alt="聊天"]')).click()
                assert page.locator('#subPageOverlay').is_hidden()
                assert page.locator('#input').input_value() == '导航后仍保留的草稿'
                page.locator('#input').fill('')

        for route in PAGES:
            if route in ('/', '/chat'): continue
            visit(route)
            page.get_by_role('button', name='返回小屋', exact=True).click()
            page.wait_for_url('http://design.test/')
            page.locator('#appGrid').wait_for()
        visit('/memory')
        page.get_by_role('button', name='工具与设置', exact=True).last.click()
        page.locator('[data-vow-dust]').click()
        assert not page.evaluate('VowDust.state().running')
        assert page.locator('body').evaluate("el => el.classList.contains('vow-gilded')")
        page.keyboard.press('Escape')
        visit('/')
        assert page.evaluate('VowDust.state().reason') == 'off'
        visit('/memory')
        page.get_by_role('button', name='工具与设置', exact=True).last.click()
        page.locator('[data-vow-dust]').click()
        page.wait_for_function('VowDust.state().running')
        page.locator('[data-vow-gold]').click()
        assert not page.locator('body').evaluate("el => el.classList.contains('vow-gilded')")
        assert not page.evaluate('VowDust.state().running')
        page.locator('[data-vow-gold]').click()
        page.keyboard.press('Escape')
        page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange'))")
        assert page.locator('body').evaluate("el => el.classList.contains('vow-paused')")
        page.evaluate("delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))")
        assert not page.locator('body').evaluate("el => el.classList.contains('vow-paused')")
        page.emulate_media(reduced_motion='reduce')
        page.wait_for_function("!VowDust.state().running && VowDust.state().reason === 'reduced-motion'")
        stopped = page.evaluate('VowDust.state().updates')
        page.wait_for_timeout(400)
        assert page.evaluate('VowDust.state().updates') == stopped
        visit('/')
        assert page.evaluate("document.getAnimations().filter(a => a.playState === 'running').length") == 0
        assert not page.evaluate('VowDust.state().running')
        page.emulate_media(reduced_motion='no-preference')
        page.set_viewport_size(dict(width=390, height=844))
        page.clock.set_fixed_time(datetime.fromisoformat('2026-09-12T10:18:00+08:00'))
        visit('/')
        assert 'BackGround.webp' in page.locator('#screen').evaluate('el => getComputedStyle(el).backgroundImage')
        shot('mobile-home-day')
        dust_workload = []
        for path in ('/', '/chat'):
            visit(path)
            if path == '/chat': page.wait_for_function("typeof chatInitialized !== 'undefined' && chatInitialized")
            page.wait_for_timeout(2300)
            for enabled in (True, False):
                page.evaluate('enabled => VowDust.setEnabled(enabled)', enabled)
                session = context.new_cdp_session(page)
                session.send('Performance.enable')
                start_updates = page.evaluate('VowDust.state().updates')
                before_metrics = {m['name']: m['value'] for m in session.send('Performance.getMetrics')['metrics']}
                page.wait_for_timeout(3000)
                after_metrics = {m['name']: m['value'] for m in session.send('Performance.getMetrics')['metrics']}
                frames = page.evaluate('VowDust.state().updates') - start_updates
                assert 14 <= frames <= 20 if enabled else frames == 0, (path, enabled, frames)
                dust_workload.append(dict(path=path, enabled=enabled, updates_in_3s=frames, **{key: round((after_metrics[key]-before_metrics[key])*1000, 3) for key in ('TaskDuration', 'LayoutDuration', 'RecalcStyleDuration')}))
                session.detach()
            page.evaluate('VowDust.setEnabled(true)')
        assert not errors, errors
        mutations = [(method, name) for method, name in requests if method not in ('GET', 'HEAD')]
        assert not mutations, mutations
        (OUTPUT / 'results.json').write_text(json.dumps(dict(layouts=layout_results, launcher_geometry_matches_original=launcher_layouts, idle_animations=idle_results, dust_updates_in_1200ms=dust_ticks, dust_workload=dust_workload, dust_hidden_covered_reduced_motion_and_low_battery_pause=True, dust_preference_persisted=True, hidden_clock_paused=True, reduced_motion=True, page_errors=errors, requests=len(requests)), ensure_ascii=False, indent=2))
        page.goto('about:blank')
        context.unroute_all(behavior='wait')
        context.close()
        browser.close()
    print(f'PASS: {len(layout_results)} responsive pages; original launcher geometry; bounded dust, background pause, navigation, retained drafts, stickers and memory. Screenshots: {OUTPUT}')


if __name__ == '__main__':
    main()

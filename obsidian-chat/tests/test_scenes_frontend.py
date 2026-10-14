"""Real browser + real scene UI/runtime, entirely local mocked HTTP; no model calls."""
import mimetypes
import re
from datetime import datetime
from pathlib import Path
import time
from urllib.parse import unquote, urlparse

import pytest

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
CARD = "## 卡1：未拆的信\n这是一张只在结束后揭晓的测试卡。\n\n先建立节奏，再留出变化，最后平稳收束。\n<devices>mock_a,mock_b</devices>"
GLOBAL_BAR = re.search(r'<section id="sceneGlobalBar"[^>]*>.*?</section>',
                       (ROOT / 'static/chat.html').read_text(), re.S).group()
SHELL = """<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/chat.css"><link rel="stylesheet" href="/static/vow.css">
<link rel="stylesheet" href="/static/scene-controls.css">
<style>#modelSelect{display:none}</style></head><body>
<select id="modelSelect"></select>
<div class="app"><main class="chat-area"><div class="chat-header">主聊天</div>
<div id="sceneGlobalChatSlot" class="scene-control-slot">__GLOBAL_BAR__</div>
<div id="messages" class="messages"></div><div class="input-area"><textarea id="chatDraft"></textarea><button id="chatSend">发送</button></div>
</main></div>
<div id="subPageOverlay"><div id="subPageBar"><button onclick="closeSubPage()">返回聊天</button><span id="subPageTitle"></span></div>
<div id="sceneGlobalSubpageSlot" class="scene-control-slot"></div><iframe id="subPageFrame"></iframe></div>
<script>var currentConvId='preview',conversations=[],models=[];window.$=id=>document.getElementById(id);
window.closeSidebar=window.callChatNative=window.refreshChat=window.renderModelSelect=()=>{};
window.api=async()=>[];window.isChatForeground=()=>!document.hidden;
</script><script src="/static/js/chat/subpage.js"></script><script src="/static/js/chat/scene_runtime.js"></script></body></html>""".replace('__GLOBAL_BAR__', GLOBAL_BAR)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


class Server:
    def __init__(self):
        from app.scenes.config import defaults
        self.config = {**defaults(), "compiler_ready": True}
        self.devices = [{**d, "connected": False} for d in self.config["devices"]]
        self.state = None
        self.feedback = None
        self.requests = []
        self.missing = []

    def route(self, route):
        path = unquote(urlparse(route.request.url).path)
        if path.startswith("/api/scenes/"):
            body = route.request.post_data_json if route.request.post_data else {}
            self.requests.append((route.request.method, path, body))
            route.fulfill(json=self.api(route.request.method, path, body))
        elif path == "/chat":
            route.fulfill(body=SHELL, content_type="text/html; charset=utf-8")
        elif path in ("/", "/scenes"):
            route.fulfill(path=ROOT / "static" / ("home.html" if path == "/" else "scenes.html"), content_type="text/html; charset=utf-8")
        elif path.startswith(("/public/", "/static/")):
            file = (ROOT.parent if path.startswith("/public/") else ROOT) / path.lstrip("/")
            if file.is_file():
                route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or "application/octet-stream")
            else:
                self.missing.append(path)
                route.fulfill(status=404, body="missing asset")
        else:
            route.fulfill(json={})

    def api(self, method, path, body):
        if path.endswith("/config"):
            if method == "PUT":
                self.config.update(body)
                # Real PUT does not repeat GET's derived readiness field.
                return {key: value for key, value in self.config.items() if key != "compiler_ready"}
            return self.config
        if "/devices/" in path:
            key, action = path.split("/")[-2:]
            next(d for d in self.devices if d["id"] == key)["connected"] = action == "connect"
            return self.devices
        if path.endswith("/devices"):
            return self.devices
        if path.endswith("/sessions/current"):
            return self.state if self.state and self.state["status"] != "ended" else None
        if path.endswith("/sessions"):
            if method == "GET":
                return [self.state] if self.state and self.state["status"] == "ended" else []
            self.state = dict(id="scene-preview", owner_id=body["owner_id"], status="preparing", revision=1,
                              generation=1, created_at=time.time(), started_at=None, messages=[], devices=[],
                              authorized_devices=body["device_ids"], bindings={})
            return self.state
        if path.endswith("/heartbeat"):
            return dict(status=self.state["status"], generation=self.state["generation"])
        if path.endswith("/feedback"):
            self.feedback = {**body, "note_status": "processed"}
            return self.feedback
        if path.endswith(("/pause", "/resume", "/end", "/finish")):
            action = path.rsplit("/", 1)[-1]
            self.state["status"] = {"pause": "paused", "resume": "active", "end": "paused", "finish": "ended"}[action]
            self.state["revision"] += 1
            self.state["generation"] += 1
            if action in ("pause", "end", "finish"):
                for device in self.state["devices"]:
                    device.update(output={}, position=dict(playing=False, step=0))
            if action == "finish":
                self.state.update(ended_at=time.time(), revealed_at=time.time(), card=dict(raw_text=CARD))
            return self.state
        if path.endswith("/messages"):
            self.state["messages"].append(dict(id="response", role="user", content=body["text"]))
            self.state["revision"] += 1
        return {**(self.state or {}), "feedback": self.feedback}

    def activate(self, page):
        self.state.update(status="active", revision=2, started_at=time.time()-123,
                          messages=[dict(id="m1", role="assistant", content="准备好了。接下来，交给这一场。"),
                                    dict(id="m2", role="user", content="嗯，我在。"),
                                    dict(id="m3", role="assistant", content="有想说的，随时告诉我。")])
        self.state["devices"] = [dict(id=d["id"], label=f"toy{i+1}", connected=True,
            output={channel: 12 + i * 8 for channel in d["capabilities"]["channels"]},
            position=dict(playing=True, step=i)) for i, d in enumerate(self.devices)
            if d["id"] in self.state["authorized_devices"]]
        page.evaluate("value => SceneRuntime.accept(value)", self.state)


def capture_full_scene(page, frame, path):
    """Expand the browser viewport to include the iframe's full scrollable content."""
    content = frame.locator(".scene-content")
    full_height = content.evaluate("el => el.scrollHeight")
    viewport = page.viewport_size
    try:
        page.set_viewport_size({"width": viewport["width"], "height": full_height + 40})
        content.evaluate("el => el.scrollTop = 0")
        frame.locator("#scenePage").screenshot(path=path)
    finally:
        page.set_viewport_size(viewport)


@pytest.mark.parametrize('width,height', [(390, 844), (320, 460), (390, 400), (1280, 900)])
def test_scene_controls_and_composer_do_not_overlap(browser, tmp_path, width, height):
    """Include the real parent bar: an iframe-only preview missed the old overlap."""
    server = Server()
    context = browser.new_context(viewport=dict(width=width, height=height), reduced_motion='reduce')
    context.route('**/*', server.route)
    page = context.new_page()
    expect = playwright.expect
    try:
        page.goto('http://scene.test/chat?scene=1')
        frame = page.frame_locator('#subPageFrame')
        expect(frame.locator('#sceneDevices .scene-device')).to_have_count(2)
        server.state = dict(id='scene-preview', owner_id=page.evaluate('SceneRuntime.ownerId()'),
                            generation=1, revision=1, status='preparing', created_at=time.time(),
                            messages=[], devices=[], authorized_devices=['mock_a', 'mock_b'])
        server.activate(page)
        expect(page.locator('#sceneGlobalBar')).to_be_hidden()
        expect(frame.locator('#sceneStatus')).to_have_text('场景进行中')

        def controls_reachable():
            for selector in ('#scenePause', '#sceneEnd', '#sceneMessage', '#sceneSend'):
                target = frame.locator(selector)
                assert target.evaluate('''el => {
                    const r=el.getBoundingClientRect();
                    return r.top >= 0 && r.bottom <= innerHeight + 1 &&
                        r.left >= 0 && r.right <= innerWidth + 1 &&
                        el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
                }'''), selector
                rect = target.bounding_box()
                assert page.evaluate('''p => document.elementFromPoint(p.x,p.y)?.id === 'subPageFrame' ''',
                                     dict(x=rect['x']+rect['width']/2, y=rect['y']+rect['height']/2))

        controls_reachable()
        # A long transcript and the device inspector must not push the composer away.
        server.state['messages'] *= 15
        server.state['revision'] += 1
        page.evaluate('value => SceneRuntime.accept(value)', server.state)
        frame.locator('.scene-output-panel > summary').click()
        controls_reachable()
        frame.locator('#sceneMessage').fill('保留正在输入的内容\n还有下一行')
        frame.locator('#sceneMessage').focus()
        server.state['revision'] += 1
        server.state['messages'].append(dict(id='new', role='assistant', content='新的场内消息。'))
        page.evaluate('value => SceneRuntime.accept(value)', server.state)
        expect(frame.locator('#sceneMessage')).to_be_focused()
        expect(frame.locator('#sceneMessage')).to_have_value('保留正在输入的内容\n还有下一行')
        controls_reachable()
        frame.locator('#sceneSend').click()
        expect(frame.locator('#sceneMessage')).to_have_value('')
        controls_reachable()
        page.screenshot(path=tmp_path / f'scene-composer-{width}-{height}.png')

        # Other subpages get one in-flow control bar, above (not over) the iframe.
        page.evaluate("openSubPage('/')")
        expect(page.locator('#sceneGlobalSubpageSlot #sceneGlobalBar')).to_be_visible()
        bar = page.locator('#sceneGlobalBar').bounding_box()
        child = page.locator('#subPageFrame').bounding_box()
        assert bar['y'] + bar['height'] <= child['y'] + 1
        page.locator('#sceneGlobalOpen').click()
        expect(frame.locator('#sceneStatus')).to_have_text('场景进行中')
        expect(page.locator('#sceneGlobalBar')).to_be_hidden()
        controls_reachable()
        server.state.update(phase='aftercare', revision=server.state['revision'] + 1)
        server.state['messages'].append(dict(id='wind-down', role='assistant', content='收束后仍可以继续交流。'))
        page.evaluate('value => SceneRuntime.accept(value)', server.state)
        expect(frame.locator('#sceneStatus')).to_have_text('收尾中')
        expect(frame.locator('#sceneReveal')).to_be_hidden()
        expect(frame.locator('#sceneMessageForm')).to_be_visible()
        controls_reachable()

        # Back in chat the same controls occupy their own row; the draft is unobscured.
        page.evaluate('closeSubPage()')
        expect(page.locator('#sceneGlobalChatSlot #sceneGlobalBar')).to_be_visible()
        bar = page.locator('#sceneGlobalBar').bounding_box()
        messages = page.locator('#messages').bounding_box()
        assert bar['y'] + bar['height'] <= messages['y'] + 1
        page.locator('#chatDraft').fill('主聊天也可以正常输入')
        page.locator('#chatSend').click()
        page.locator('#sceneGlobalPause').click()
        expect(page.locator('#sceneGlobalStatus')).to_have_text('场景已暂停')
        assert sum(path.endswith('/pause') for _, path, _ in server.requests) == 1
        page.locator('#sceneGlobalOpen').click()
        expect(frame.locator('#sceneStatus')).to_have_text('场景已暂停')
        controls_reachable()
        # Physical reconnect guidance + a multiline draft in a keyboard-sized viewport.
        for device, driver in zip(server.state['devices'], ('muse_ble', 'sosexy_ble')):
            device['driver'] = driver
        server.state.update(reason='device_disconnected', revision=server.state['revision'] + 1)
        page.evaluate('value => SceneRuntime.accept(value)', server.state)
        expect(frame.locator('#sceneReconnect')).to_be_visible()
        frame.locator('#sceneMessage').fill('\n'.join(['暂停后仍能输入反馈'] * 8))
        frame.locator('.scene-output-panel > summary').click()
        controls_reachable()
        page.screenshot(path=tmp_path / f'scene-paused-composer-{width}-{height}.png')
        frame.locator('#sceneEnd').click()
        expect(frame.locator('#sceneReveal')).to_be_visible()
        expect(page.locator('#sceneGlobalBar')).to_be_hidden()
        expect(frame.locator('#sceneLive')).to_be_visible()
        expect(frame.locator('#sceneMessages')).to_contain_text('收束后仍可以继续交流。')
        expect(frame.locator('#sceneMessageForm')).to_be_hidden()
        assert sum(path.endswith('/finish') for _, path, _ in server.requests) == 1
        assert not server.missing
        print(f'Scene composer screenshot: {tmp_path}')
    finally:
        context.close()


@pytest.mark.parametrize("width", [390, 1280, 320])
def test_scene_home_entry_and_complete_responsive_ui(browser, tmp_path, width):
    server = Server()
    context = browser.new_context(viewport=dict(width=width, height=900 if width > 740 else 844),
                                  reduced_motion="reduce", service_workers="block")
    context.route("**/*", server.route)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    expect = playwright.expect
    try:
        # Top-level home -> scene redirect -> the persistent chat runtime shell.
        page.goto("http://scene.test/")
        expect(page.locator("#appGrid .app-icon")).to_have_count(8)
        expect(page.locator("#dock .app-icon")).to_have_count(4)
        expect(page.locator("#appGrid").get_by_role("button", name="小剧场")).to_have_count(0)
        assert page.locator("#dock img").evaluate_all("imgs => imgs.map(img => img.alt)") == ["聊天", "记忆库", "心语", "小剧场"]
        boxes = [item.bounding_box() for item in page.locator("#dock .icon-img").all()]
        for i, box in enumerate(boxes):
            assert box["width"] == box["height"] == 60
            assert 0 <= box["x"] <= width - box["width"]
            assert box["y"] == boxes[0]["y"]
            if i:
                assert box["x"] > boxes[i - 1]["x"] + boxes[i - 1]["width"]
        icon = page.locator("#dock").get_by_role("button", name="小剧场")
        expect(icon).to_be_visible()
        expect(icon.locator("img")).to_have_js_property("complete", True)
        assert icon.locator("img").evaluate("img => img.naturalWidth > 0")
        page.screenshot(path=tmp_path / f"home-{width}.png")
        icon.click()
        page.wait_for_url("**/chat?scene=1")
        frame = page.frame_locator("#subPageFrame")
        expect(frame.locator("#sceneDevices .scene-device")).to_have_count(2)
        expect(frame.locator("#sceneIntro")).to_have_text("按彼此的了解，准备这一次。")
        expect(frame.locator("#sceneEntryHint")).to_have_text("先启用小剧场")
        expect(frame.locator("#sceneEnter")).to_be_disabled()
        expect(frame.locator("#sceneReveal")).to_be_hidden()
        assert "未拆的信" not in frame.locator("body").inner_text()
        frame.locator("#sceneEnabled").check()
        expect(frame.locator("#sceneEntryHint")).to_have_text("先连接并选择设备")
        for name in ("模拟设备 A", "模拟设备 B"):
            frame.get_by_role("button", name=f"连接{name}", exact=True).click()
            frame.get_by_role("checkbox", name=f"本场允许使用{name}").check()
        expect(frame.locator("#sceneEntryHint")).to_have_text("请填写本场安全词")
        frame.locator("#sceneSafeword").fill("stop-now")
        expect(frame.locator("#sceneEnter")).to_be_enabled()
        expect(frame.locator("#sceneSelectedCount")).to_have_text("已选 2 台")
        # Capture the whole scrollable page without touching application code.
        frame.locator(".scene-content").evaluate("el => el.scrollTop = 0")
        page.screenshot(path=tmp_path / f"entry-{width}.png")
        capture_full_scene(page, frame, tmp_path / f"entry-full-{width}.png")
        assert frame.locator(".scene-content").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        frame.locator("#sceneEnter").click()
        expect(frame.locator("#sceneStatus")).to_have_text("正在准备")
        expect(frame.locator("#scenePause")).to_be_disabled()
        start = next(body for method, path, body in server.requests if method == "POST" and path.endswith("/sessions"))
        assert start["device_ids"] == ["mock_a", "mock_b"] and start["safeword"] == "stop-now"
        server.activate(page)
        expect(frame.locator("#sceneStatus")).to_have_text("场景进行中")
        expect(frame.locator("#sceneIntro")).to_have_text("有想说的，随时说。")
        expect(frame.locator("#sceneSetup")).to_be_hidden()
        expect(frame.locator("#sceneReveal")).to_be_hidden()
        assert frame.locator("#sceneCardText").inner_text() == ""
        frame.locator(".scene-output-panel > summary").click()
        expect(frame.locator("#scenePlayback meter")).to_have_count(3)
        expect(frame.locator("#sceneOutputStatus")).to_have_text("2 / 2 台运行中")
        frame.locator(".scene-content").evaluate("el => el.scrollTop = 0")
        page.screenshot(path=tmp_path / f"live-{width}.png")
        capture_full_scene(page, frame, tmp_path / f"live-full-{width}.png")
        frame.locator(".scene-content").evaluate("el => el.scrollTop = el.scrollHeight")
        assert frame.locator("#sceneEnd").evaluate("el => { const r=el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }")
        assert frame.locator(".scene-content").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        frame.locator("#scenePause").click()
        expect(frame.locator("#sceneStatus")).to_have_text("场景已暂停")
        expect(frame.locator("#sceneOutputStatus")).to_have_text("0 / 2 台运行中")
        frame.locator("#scenePause").click()
        expect(frame.locator("#sceneStatus")).to_have_text("场景进行中")
        # Home navigation swaps only the child; it must not pause or end the scene.
        owner = page.evaluate("SceneRuntime.ownerId()")
        pauses = sum(path.endswith("/pause") for _, path, _ in server.requests)
        page.evaluate("openSubPage('/')")
        expect(frame.locator("#dock .app-icon")).to_have_count(4)
        frame.locator("#dock").get_by_role("button", name="小剧场").click()
        expect(frame.locator("#sceneStatus")).to_have_text("场景进行中")
        assert page.evaluate("SceneRuntime.ownerId()") == owner
        assert sum(path.endswith("/pause") for _, path, _ in server.requests) == pauses
        frame.locator("#sceneMessage").fill("<img src=x onerror=alert(1)>")
        frame.locator("#sceneSend").click()
        expect(frame.locator(".scene-message.user").last).to_have_text("<img src=x onerror=alert(1)>")
        expect(frame.locator("#sceneMessages img")).to_have_count(0)
        frame.locator("#sceneEnd").click()
        expect(frame.locator("#sceneReveal")).to_be_visible()
        expect(frame.locator("#sceneIntro")).to_have_text("看看开始前的想法。")
        expect(frame.locator(".scene-feedback > h3 + .hint")).to_have_text("喜欢的、想调整的，都可以留给下次参考。不写也没关系。")
        expect(frame.locator("#sceneSetup")).to_be_hidden()
        expect(frame.locator("#sceneCardText")).to_have_text(CARD)
        frame.locator(".scene-content").evaluate("el => el.scrollTop = 0")
        page.screenshot(path=tmp_path / f"reveal-{width}.png")
        capture_full_scene(page, frame, tmp_path / f"reveal-full-{width}.png")
        frame.get_by_role("button", name="喜欢", exact=True).click()
        frame.locator("#sceneFeedbackSave").click()
        expect(frame.locator("#sceneFeedbackStatus")).to_contain_text("笔记处理完成")
        assert server.feedback["rating"] == "up" and server.feedback["reason"] == ""
        frame.locator("#sceneAgain").click()
        expect(frame.locator("#sceneSetup")).to_be_visible()
        expect(frame.locator("#sceneReveal")).to_be_hidden()
        expect(frame.locator("#sceneIntro")).to_have_text("按彼此的了解，准备这一次。")
        # A late identical ended snapshot must not reopen a dismissed reveal.
        page.evaluate("SceneRuntime.accept(SceneRuntime.current())")
        expect(frame.locator("#sceneReveal")).to_be_hidden()
        assert not errors and not server.missing
        print(f"Scene UI screenshots: {tmp_path}")
    finally:
        context.close()


def test_four_entry_dock_arrival_animation_finishes(browser):
    server = Server()
    context = browser.new_context(viewport=dict(width=390, height=844),
                                  reduced_motion="no-preference", service_workers="block")
    context.route("**/*", server.route)
    page = context.new_page()
    try:
        page.goto("http://scene.test/")
        playwright.expect(page.locator("#dock .app-icon")).to_have_count(4)
        page.wait_for_function("!document.body.classList.contains('home-arrival')")
        assert page.evaluate("document.getAnimations().filter(a => a.playState === 'running').length") == 0
        playwright.expect(page.locator("#dock").get_by_role("button", name="小剧场")).to_be_visible()
        assert not server.missing
    finally:
        context.close()


def assert_home_icons_on_wall(page):
    """Check against the visible wall in the image, not the layout's CSS vars."""
    metrics = page.evaluate("""() => {
      const rect = el => {
        const r = el.getBoundingClientRect();
        return {x:r.x, y:r.y, width:r.width, height:r.height, bottom:r.bottom};
      };
      const screen = document.getElementById('screen');
      const style = getComputedStyle(screen);
      return {
        viewport: rect(screen), clock: rect(document.querySelector('.clock-widget')),
        dock: rect(document.getElementById('dock')),
        imageSize: style.backgroundSize.split(' ').map(parseFloat),
        imageY: parseFloat(style.backgroundPositionY),
        entries: [...document.querySelectorAll('#appGrid .app-icon')].map(rect),
        icons: [...document.querySelectorAll('#appGrid .icon-img')].map(rect),
        dockIcons: [...document.querySelectorAll('#dock .icon-img')].map(rect),
      };
    }""")
    size = metrics["viewport"]
    scale = metrics["imageSize"][1] / 1279
    assert metrics["imageSize"][0] / 720 == pytest.approx(scale, abs=.001)
    # The sill ends at about 666 px, and the skirting starts at 934 px.
    wall_top = size["y"] + metrics["imageY"] + 666 * scale
    wall_bottom = size["y"] + metrics["imageY"] + 934 * scale
    assert len(metrics["entries"]) == 8
    for entry, icon in zip(metrics["entries"], metrics["icons"]):
        assert entry["y"] >= max(wall_top, metrics["clock"]["bottom"] + 8) - 1
        assert entry["bottom"] <= min(wall_bottom, metrics["dock"]["y"] - 8) + 1
        assert size["x"] <= entry["x"] < entry["x"] + entry["width"] <= size["x"] + size["width"]
        assert 44 <= icon["width"] <= 60
        assert icon["width"] == icon["height"]
    # Moving the gallery must not move or resize the four existing Dock icons.
    assert len(metrics["dockIcons"]) == 4
    assert metrics["dock"]["bottom"] == pytest.approx(size["bottom"] - 28)
    for icon in metrics["dockIcons"]:
        assert icon["width"] == icon["height"] == 60
    return metrics


@pytest.mark.parametrize("size", [(390, 844), (320, 844), (390, 667), (320, 568),
                                  (1280, 900), (1440, 1000), (768, 1024), (844, 390)])
@pytest.mark.parametrize("hour", [10, 21], ids=["day", "night"])
def test_home_gallery_is_anchored_to_the_wall(browser, tmp_path, size, hour):
    server = Server()
    context = browser.new_context(viewport=dict(width=size[0], height=size[1]), timezone_id="UTC",
                                  reduced_motion="reduce", service_workers="block")
    context.route("**/*", server.route)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    try:
        page.clock.set_fixed_time(datetime.fromisoformat(f"2026-09-20T{hour}:18:00+00:00"))
        page.goto("http://scene.test/")
        metrics = assert_home_icons_on_wall(page)
        expected_rows = 1 if size == (844, 390) else 2
        assert len({round(entry["y"]) for entry in metrics["entries"]}) == expected_rows
        background = page.locator("#screen").evaluate("el => getComputedStyle(el).backgroundImage")
        assert ("BackGroundN.webp" if hour == 21 else "BackGround.webp") in background
        page.screenshot(path=tmp_path / f"home-wall-{size[0]}x{size[1]}-{'night' if hour == 21 else 'day'}.png")
        assert not errors and not server.missing
        print(f"Wall layout screenshot: {tmp_path}")
    finally:
        context.close()


def test_home_wall_tracks_rotation_and_day_night_switch(browser):
    server = Server()
    context = browser.new_context(viewport=dict(width=390, height=844), timezone_id="UTC",
                                  reduced_motion="reduce", service_workers="block")
    context.route("**/*", server.route)
    page = context.new_page()
    try:
        page.clock.set_fixed_time(datetime.fromisoformat("2026-09-20T10:18:00+00:00"))
        page.goto("http://scene.test/")
        assert_home_icons_on_wall(page)
        for width, height, columns in [(844, 390, "8"), (390, 844, "4")]:
            page.set_viewport_size(dict(width=width, height=height))
            page.wait_for_function("columns => document.getElementById('screen').style.getPropertyValue('--home-wall-columns') === columns", arg=columns)
            assert_home_icons_on_wall(page)
        page.clock.set_fixed_time(datetime.fromisoformat("2026-09-20T21:18:00+00:00"))
        page.evaluate("updateBackground()")
        assert_home_icons_on_wall(page)
        assert "BackGroundN.webp" in page.locator("#screen").evaluate("el => getComputedStyle(el).backgroundImage")
    finally:
        context.close()


def test_scene_entry_assets_and_cache_versions():
    html = (ROOT / "static/scenes.html").read_text()
    assert '/static/scenes.css?v=20260927-scene-finish"' in html
    assert '/static/js/scenes.js?v=20260927-scene-finish"' in html
    parent = (ROOT / 'static/chat.html').read_text()
    assert '/static/js/chat/scene_runtime.js?v=20260927-scene-finish"' in parent
    assert '/static/js/chat/scene_hardware.js?v=20260926-five-devices"' in parent
    assert '/static/scene-controls.css?v=20260926-scene-layout"' in parent
    assert '/static/js/chat/subpage.js?v=20260926-scene-layout"' in parent
    assert 'position:fixed' not in GLOBAL_BAR and 'bottom:' not in GLOBAL_BAR
    assert (ROOT.parent / "public/optimized/scene-theater-v1.png").is_file()
    home = (ROOT / "static/home.html").read_text()
    assert "id: 'scenes'" in home and "url: '/scenes'" in home
    assert '/static/home.css?v=20260920-wall"' in home
    assert '/static/home.js?v=20260920-wall"' in home


def test_five_device_entry_registers_missing_profiles_and_keeps_owner_introduction(browser):
    from app.scenes.hardware import devices as hardware_devices

    class HardwareServer(Server):
        def route(self, route):
            if urlparse(route.request.url).path == '/chat':
                body = SHELL.replace('</body>', '<script src="/static/js/chat/scene_hardware.js"></script></body>')
                route.fulfill(body=body, content_type='text/html; charset=utf-8')
            else:
                super().route(route)

        def api(self, method, path, body):
            if path.endswith('/hardware'):
                return hardware_devices()
            if path.endswith('/bridge'):
                for device in self.devices:
                    device['connected'] = device['id'] in body['connected']
                return self.devices
            if path.endswith('/config') and method == 'PUT':
                known = {d['id']: d['connected'] for d in self.devices}
                self.devices = [{**d, 'connected': known.get(d['id'], False)} for d in body['devices']]
            return super().api(method, path, body)

    server = HardwareServer()
    server.config.update(enabled=True, devices=hardware_devices()[:2])
    server.config['devices'][0]['description'] = '保留手动填写的设备介绍。'
    server.devices = [{**d, 'connected': False} for d in server.config['devices']]
    context = browser.new_context(viewport=dict(width=390, height=844))
    context.add_init_script("""window.nativeTest = {version:3, background_player:true, foreground:true, active:false,
      drivers:{muse_ble:true,sosexy_ble:false,cx492b_ble:false,sk30_ble:true,sk40_ble:true}};
      window.ObsidianSceneDevices = {status:()=>JSON.stringify(nativeTest),
        connectDevice:driver=>{nativeTest.drivers[driver]=true;return true;},
        disconnectDevice:driver=>{nativeTest.drivers[driver]=false;}, observe:()=>{}};""")
    context.route('**/*', server.route)
    page = context.new_page()
    expect = playwright.expect
    try:
        page.goto('http://scene.test/chat?scene=1')
        frame = page.frame_locator('#subPageFrame')
        expect(frame.locator('#sceneDevices .scene-device')).to_have_count(2)
        frame.locator('#sceneSettings > summary').click()
        frame.locator('#sceneAddHardware').click()
        expect(frame.locator('#sceneDevices .scene-device')).to_have_count(5)
        expect(frame.locator('#sceneConfigStatus')).to_contain_text('已补入 3 台')
        assert server.config['devices'][0]['description'] == '保留手动填写的设备介绍。'
        for index in range(5):
            row = frame.locator('#sceneDevices .scene-device').nth(index)
            row.locator('button').click()
            expect(row.locator('input')).to_be_enabled()
            row.locator('input').check()
        expect(frame.locator('#sceneSelectedCount')).to_have_text('已选 5 台')
        expect(frame.locator('#sceneDevices')).to_contain_text('Cachito')
        expect(frame.locator('#sceneDevices')).to_contain_text('野行 Pro')
        frame.locator('#sceneSafeword').fill('test-stop')
        expect(frame.locator('#sceneEnter')).to_be_enabled()
        assert page.evaluate('SceneHardware.connected().length') == 5
        # Re-adding the catalog is idempotent, never resets custom text/ranges.
        frame.locator('#sceneAddHardware').click()
        expect(frame.locator('#sceneConfigStatus')).to_contain_text('已补入 0 台')
        assert len(server.config['devices']) == 5
        assert server.config['devices'][0]['description'] == '保留手动填写的设备介绍。'
    finally:
        context.close()

"""Exercise actual settings controls with a local, fully intercepted server.

Run: .venv/bin/python obsidian-chat/tests/frontend_thinking_smoke.py
"""
import mimetypes
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_thinking import thinking_capabilities


def main():
    endpoints = [
        dict(id="ds", name="DeepSeek", type="openai", base_url="https://api.deepseek.com/v1"),
        dict(id="sf", name="硅基流动", type="openai", base_url="https://api.siliconflow.cn/v1"),
        dict(id="gemini", name="Gemini", type="vertex", base_url="https://provider.invalid/v1"),
    ]
    slots = {
        "sentinel": dict(endpoint="ds", model="deepseek-v4-flash"),
        "memory_digest": dict(endpoint="ds", model="deepseek-v4-flash"),
        "relational_card_generation": dict(endpoint="sf", model="deepseek-ai/DeepSeek-V3.2"),
        "working_model_gate": dict(endpoint="sf", model="deepseek-ai/DeepSeek-V4-Flash"),
    }
    models = {"Gemini 测试": dict(endpoint="gemini", model="gemini-3.8-flash", thinking_level="HIGH")}
    memory = dict(timeline_generation_timeout_sec=20)
    writes, errors = [], []
    reject_save = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=390, height=844), is_mobile=True,
                                      service_workers="block")
        context.route_web_socket("**/ws", lambda socket: None)

        def handle(route):
            request = route.request
            url = urlparse(request.url)
            path = url.path
            if path == "/settings":
                route.fulfill(path=ROOT / "static/settings.html", content_type="text/html; charset=utf-8")
                return
            if path.startswith("/static/"):
                file = ROOT / path.lstrip("/")
                if file.is_file():
                    route.fulfill(path=file, content_type=mimetypes.guess_type(str(file))[0] or "application/octet-stream")
                else:
                    route.fulfill(status=404, body="missing asset")
                return
            data = {}
            if request.method == "PUT":
                body = request.post_data_json
                writes.append((path, body))
                if reject_save and path == "/api/slots":
                    route.fulfill(status=422, json=dict(detail="合成保存失败"))
                    return
                if path == "/api/slots":
                    slots[body["name"]] = dict(endpoint=body["endpoint"], model=body["model"], **body["extras"])
                elif path == "/api/user_models":
                    models[body["key"]] = {key: value for key, value in body.items() if key != "key"}
                elif path == "/api/memories/v3/config":
                    memory.update(body)
                data = dict(ok=True)
            elif path == "/api/endpoints": data = endpoints
            elif path == "/api/slots": data = slots
            elif path == "/api/user_models": data = models
            elif path == "/api/memories/v3/config": data = memory
            elif path == "/api/model-thinking":
                query = parse_qs(url.query)
                endpoint = next(ep for ep in endpoints if ep["id"] == query["endpoint"][0])
                data = thinking_capabilities(endpoint, query["model"][0])
            elif path in ("/api/provider/summary", "/api/location/diagnostics/summary", "/api/devices"): data = []
            route.fulfill(json=data)

        context.route("**/*", handle)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://thinking.test/settings")
        page.locator('[data-tab="slots"]').click()
        expect(page.locator("#saveSlotsButton")).to_be_enabled()
        expect(page.locator("#sentinelThinkingMode")).to_have_value("default")
        page.locator("#sentinelThinkingMode").select_option("off")
        expect(page.locator("#sentinelThinkingLevel")).to_be_disabled()
        page.locator("#memoryDigestThinkingMode").select_option("on")
        page.locator("#memoryDigestThinkingLevel").select_option("LOW")
        page.locator("#relationalCardThinkingMode").select_option("on")
        page.locator("#relationalCardThinkingBudget").fill("1024")
        assert page.locator("#workingModelGateThinkingLevel option").all_text_contents() == ["模型默认", "高", "最高"]
        page.locator("#timelineTimeout").fill("60")
        page.locator("#saveSlotsButton").click()
        expect(page.locator("#commonToast")).to_contain_text("槽位与时间线超时已保存")
        assert slots["sentinel"]["enable_thinking"] is False
        assert slots["sentinel"]["thinking_level"] is None
        assert slots["memory_digest"]["thinking_level"] == "LOW"
        assert slots["relational_card_generation"]["thinking_budget"] == 1024
        assert memory["timeline_generation_timeout_sec"] == 60

        page.reload()
        page.locator('[data-tab="slots"]').click()
        expect(page.locator("#saveSlotsButton")).to_be_enabled()
        expect(page.locator("#sentinelThinkingMode")).to_have_value("off")
        expect(page.locator("#relationalCardThinkingBudget")).to_have_value("1024")
        page.locator('[data-edit-model="0"]').click()
        expect(page.locator("#umThinkingLevel")).to_have_value("HIGH")
        assert page.locator("#umThinkingMode option[value=off]").count() == 0
        page.locator("#umThinkingLevel").select_option("LOW")
        page.locator("#umSave").click()
        expect(page.locator("#commonToast")).to_contain_text("模型已保存")
        assert models["Gemini 测试"]["thinking_level"] == "LOW"
        assert models["Gemini 测试"]["image_input"] is None

        # A switch of endpoint/model must remove the old provider's controls.
        page.locator("#relationalCardEp").select_option("gemini")
        page.locator("#relationalCardModel").fill("gemini-3.8-flash")
        page.locator("#relationalCardModel").press("Tab")
        expect(page.locator("#relationalCardThinkingBudgetRow")).to_be_hidden()
        expect(page.locator("#relationalCardThinkingLevelRow")).to_be_visible()
        assert page.locator("#relationalCardThinkingMode option[value=off]").count() == 0
        assert page.evaluate("readThinkingControl('relationalCard')") == dict(enable_thinking=None, thinking_level=None, thinking_budget=None)

        reject_save = True
        page.locator("#saveSlotsButton").click()
        expect(page.locator("#commonToast")).to_contain_text("合成保存失败")
        assert not errors, errors
        # Mobile controls must fit without horizontal scrolling.
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        print(f"PASS: mobile controls, provider capabilities, save/reload, model edit, model switch, HTTP errors ({len(writes)} mocked writes)")
        browser.close()


if __name__ == "__main__":
    main()

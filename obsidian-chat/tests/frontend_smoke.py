#!/usr/bin/env python3
"""Browser smoke checks for the static chat frontend.

Run this against a local backend, for example:
  OBSIDIAN_BIND_HOST=127.0.0.1 OBSIDIAN_BIND_PORT=18181 python main.py
  python tests/frontend_smoke.py --base-url http://127.0.0.1:18181
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


FIRST_PAINT_SCRIPT_BUDGET = 29


def _record_request_failure(req) -> dict[str, str]:
    failure: Any = req.failure
    if callable(failure):
        try:
            failure = failure()
        except Exception as exc:  # pragma: no cover - depends on playwright version
            failure = str(exc)
    return {"url": req.url, "failure": str(failure or "")}


def _base_result(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "console_errors": [],
        "page_errors": [],
        "request_failures": [],
    }


def run_desktop(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    result = _base_result("desktop")
    request_urls: list[str] = []
    page.on("console", lambda msg: result["console_errors"].append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: result["page_errors"].append(str(exc)))
    page.on("request", lambda req: request_urls.append(req.url))
    page.on("requestfailed", lambda req: result["request_failures"].append(_record_request_failure(req)))

    page.goto(f"{base_url}/chat", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_selector("#messages", timeout=10_000)
    page.wait_for_selector("#input", timeout=10_000)
    page.wait_for_selector("#convList", timeout=10_000)
    page.wait_for_timeout(1_200)

    initial_request_urls = list(request_urls)
    core = page.evaluate(
        """() => ({
          title: document.title,
          bodyText: document.body.innerText.slice(0, 300),
          scriptCount: document.scripts.length,
          styleTagCount: document.querySelectorAll('style').length,
          inlineStyleAttrCount: document.querySelectorAll('[style]').length,
          inlineHandlerAttrCount: Array.from(document.querySelectorAll('*')).filter(
            el => Array.from(el.attributes || []).some(attr => /^on/i.test(attr.name))
          ).length,
          linkCss: !!document.querySelector('link[href="/static/chat.css"]'),
          inputExists: !!document.querySelector('#input'),
          messagesExists: !!document.querySelector('#messages'),
          sendBtnExists: !!document.querySelector('#sendBtn'),
          convListExists: !!document.querySelector('#convList'),
          convItems: document.querySelectorAll('.conv-item').length,
          chatPerf: window.ChatPerf && window.ChatPerf.getMarks ? window.ChatPerf.getMarks() : null,
          chatApp: window.ChatApp ? {
            hasRegister: typeof window.ChatApp.registerModule === 'function',
            hasGet: typeof window.ChatApp.getModule === 'function',
            moduleNames: Object.keys(window.ChatApp.modules || {}).sort(),
            ttsVoiceListLoaded: window.ChatApp.getModule('tts')?.isVoiceListLoaded?.(),
            stateOwnerKeys: Object.keys(window.ChatApp.getModule('state')?.owners?.() || {}).sort(),
            stateSnapshot: window.ChatApp.getModule('state')?.snapshot?.(),
          } : null,
        })"""
    )

    page.click(".config-btn")
    page.wait_for_timeout(500)
    config_open = page.locator("#configPopup.show").count() == 1
    page.evaluate("document.querySelector('#configPopup')?.classList.remove('show')")

    page.evaluate("openSystemLog()")
    syslog_open = page.locator("#sysLogModal.show").count() == 1
    debug_log_lazy_loaded = page.evaluate("!!window.ChatApp?.getModule?.('debugLog')")
    page.evaluate("closeSystemLog()")

    page.evaluate("openFileManager()")
    page.wait_for_selector("#fileModal.show", timeout=5_000)
    file_manager_open = page.locator("#fileModal.show").count() == 1
    file_manager_lazy_loaded = page.evaluate("!!window.ChatApp?.getModule?.('fileManager')")
    page.evaluate("closeFileManager()")

    voice_lazy_loaded = page.evaluate(
        """() => window.ChatApp.getModule('voiceLoader').ensureLoaded()
          .then(() => !!window.ChatApp.getModule('voice'))"""
    )

    page.evaluate("openSubPage('/settings')")
    page.wait_for_selector("#subPageOverlay.show", timeout=5_000)
    page.wait_for_timeout(800)
    subpage_open = page.locator("#subPageOverlay.show").count() == 1
    frame_src = page.locator("#subPageFrame").get_attribute("src")
    page.evaluate("closeSubPage()")

    page.evaluate("openWhisper()")
    page.wait_for_selector("#whisperModal.show", timeout=5_000)
    whisper_open = page.locator("#whisperModal.show").count() == 1
    whisper_lazy_loaded = page.evaluate("!!window.ChatApp?.getModule?.('whisperToy')")
    page.evaluate("closeWhisper()")

    page.evaluate("openAiDom()")
    page.wait_for_selector("#aiDomModal.show", timeout=5_000)
    aidom_open = page.locator("#aiDomModal.show").count() == 1
    aidom_lazy_loaded = page.evaluate("!!window.ChatApp?.getModule?.('aiDom')")
    page.evaluate("closeAiDom()")

    synthetic_core = page.evaluate(
        """() => {
          const now = Date.now() / 1000;
          conversations = [{
            id: 'smoke_conv',
            title: 'Smoke Conversation',
            model: document.querySelector('#modelSelect')?.value || 'smoke-model',
            message_count: 2,
          }];
          currentConvId = 'smoke_conv';
          hasMoreMessages = false;
          currentMessages = [
            { id: 'smoke_user', conv_id: 'smoke_conv', role: 'user', content: 'hello\\nsmoke', created_at: now - 10, attachments: [] },
            { id: 'smoke_ai', conv_id: 'smoke_conv', role: 'assistant', content: 'reply smoke', created_at: now, attachments: [] },
          ];
          renderConvList();
          renderMessages();
          return {
            convActive: !!document.querySelector('[data-conv-id="smoke_conv"].active'),
            userRow: !!document.querySelector('#m_smoke_user'),
            aiRow: !!document.querySelector('#m_smoke_ai'),
          };
        }"""
    )
    page.hover("#m_smoke_user")
    page.click("#m_smoke_user .msg-dots")
    message_menu_open = page.locator("#menu_smoke_user.show").count() == 1
    page.click('#menu_smoke_user [data-msg-action="edit"]')
    message_edit_open = page.locator("#edit_smoke_user").count() == 1
    page.click('#m_smoke_user [data-edit-action="cancel"]')
    message_edit_cancel = page.locator("#edit_smoke_user").count() == 0

    page.evaluate(
        """() => {
          msgMusicCards.smoke_ai = [{
            id: 123456,
            name: 'Smoke Song',
            artist: 'Smoke Artist',
            album: 'Smoke Album',
            cover: '',
            candidates: [{ id: 654321, name: 'Alt Smoke', artist: 'Alt Artist' }],
          }];
          renderMusicCards('smoke_ai');
        }"""
    )
    music_card_rendered = page.locator("#m_smoke_ai .music-card").count() == 1
    music_candidate_rendered = page.locator("#m_smoke_ai .music-candidates .cand-item").count() == 1

    page.evaluate(
        """() => {
          addSystemLog({
            model: 'smoke-model',
            usage: null,
            recalled_memories: [{ score: 0.9876, type: 'fact', content: 'smoke memory' }],
            prompt_messages: [{ role: 'system', content: 'smoke prompt' }],
            prompt_count: 1,
          });
          openSystemLog();
        }"""
    )
    page.click("#sysLogList [data-syslog-detail]")
    syslog_detail_toggle = page.locator("#sysLogList .syslog-detail.show").count() >= 1
    page.evaluate("closeSystemLog()")

    mock_send_core = page.evaluate(
        """async () => {
          const originalFetch = window.fetch;
          const originalPlayMusicOnline = playMusicOnline;
          const originalToyExecCmd = toyExecCmd;
          const originalAwaitTypingFloor = _awaitTypingFloor;
          const originalTtsSpeak = ttsSpeak;
          const originalToyConnected = toyConnected;
          let controlSession = null;
          window.__smokeSend = { fetchCalled: false, toyCommands: [] };
          playMusicOnline = songId => { window.__smokeSend.musicAutoplay = String(songId); };
          toyExecCmd = cmd => {
            window.__smokeSend.toyCommand = cmd;
            window.__smokeSend.toyCommands.push(String(cmd));
          };
          _awaitTypingFloor = async () => {};
          ttsSpeak = (text, msgId) => { window.__smokeSend.tts = { text, msgId }; };
          toyConnected = true;
          currentConvId = 'smoke_send_conv';
          conversations = [{
            id: 'smoke_send_conv',
            title: 'Smoke Send',
            model: document.querySelector('#modelSelect')?.value || 'smoke-model',
            message_count: 0,
          }];
          currentMessages = [];
          hasMoreMessages = false;
          msgMusicCards = {};
          msgDebugData = {};
          pendingAttachments = [];
          sending = false;
          streamingAiId = null;
          document.querySelector('#input').value = 'stream hello';
          document.querySelector('#sendBtn').disabled = false;
          renderConvList();
          renderMessages();
          controlSession = await ControlRuntime.start('whisper', {
            convId: 'smoke_send_conv',
            snapshot: { whisper_mode: true, toy_connected: true },
            deviceId: 'browser_toy_bridge',
          });
          const sentEvents = [
            { type: 'start', id: 'smoke_send_ai' },
            { type: 'chunk', content: 'Hello ' },
            { type: 'chunk', content: '<meta>x</meta>world [MUSIC:1] [TOY:1] [HEART:secret]' },
            {
              type: 'debug',
              msg_id: 'smoke_send_ai',
              model: 'smoke-model',
              usage: null,
              recalled_memories: [],
              prompt_messages: [{ role: 'system', content: 'smoke prompt' }],
              prompt_count: 1,
            },
            {
              type: 'music',
              msg_id: 'smoke_send_ai',
              cards: [{ id: 789123, name: 'Stream Song', artist: 'Stream Artist', cover: '', candidates: [] }],
            },
            { type: 'toy_command', commands: ['1'] },
            {
              type: 'toy_command',
              commands: ['2'],
              control_session_id: controlSession?.session_id,
              control_epoch: controlSession?.control_epoch,
              owner_client_id: controlSession?.owner_client_id,
            },
            { type: 'heart_whisper', msg_id: 'smoke_send_ai', content: 'secret heart' },
          ];
          window.fetch = (url, opts) => {
            if (String(url).includes('/api/conversations/smoke_send_conv/send')) {
              window.__smokeSend.fetchCalled = true;
              window.__smokeSend.requestBody = JSON.parse(opts.body || '{}');
              const body = sentEvents.map(event => `data: ${JSON.stringify(event)}\\n\\n`).join('');
              return Promise.resolve(new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }));
            }
            return originalFetch(url, opts);
          };
          try {
            await send();
          } finally {
            window.fetch = originalFetch;
            if (controlSession) await ControlRuntime.end('normal');
            playMusicOnline = originalPlayMusicOnline;
            toyExecCmd = originalToyExecCmd;
            _awaitTypingFloor = originalAwaitTypingFloor;
            ttsSpeak = originalTtsSpeak;
            toyConnected = originalToyConnected;
          }
          const ai = currentMessages.find(m => m.id === 'smoke_send_ai');
          const user = currentMessages.find(m => m.id === 'temp_user');
          return {
            fetchCalled: window.__smokeSend.fetchCalled,
            requestContent: window.__smokeSend.requestBody?.content,
            userOptimistic: !!user && user.content === 'stream hello',
            aiContent: ai?.content || '',
            debugLogged: !!msgDebugData.smoke_send_ai,
            musicState: !!msgMusicCards.smoke_send_ai,
            musicRendered: !!document.querySelector('#m_smoke_send_ai .music-card'),
            musicAutoplay: window.__smokeSend.musicAutoplay,
            toyCommand: window.__smokeSend.toyCommand,
            toyCommands: window.__smokeSend.toyCommands,
            heartHint: !!document.querySelector('#m_smoke_send_ai .heart-whisper-hint'),
            ttsText: window.__smokeSend.tts?.text || '',
            ttsMsgId: window.__smokeSend.tts?.msgId || '',
            sendSettled: sending === false && streamingAiId === null && document.querySelector('#sendBtn').disabled === false,
          };
        }"""
    )

    dynamic_inline_handler_count = page.evaluate(
        """() => Array.from(document.querySelectorAll('*')).filter(
          el => Array.from(el.attributes || []).some(attr => /^on/i.test(attr.name))
        ).length"""
    )

    screenshot = screenshot_dir / "obsidianvow-chat-smoke.png"
    page.screenshot(path=str(screenshot), full_page=True)

    result.update(
        {
            "core": core,
            "checks": {
                "config_open": config_open,
                "syslog_open": syslog_open,
                "subpage_open": subpage_open,
                "subpage_frame_src": frame_src,
                "whisper_open": whisper_open,
                "aidom_open": aidom_open,
                "tts_voices_not_loaded_before_config": not any(
                    "/api/tts/voices" in url for url in initial_request_urls
                ),
                "heart_whispers_not_loaded_on_first_paint": not any(
                    "/api/heart-whispers" in url for url in initial_request_urls
                ),
                "first_paint_script_budget": core.get("scriptCount", 999) <= FIRST_PAINT_SCRIPT_BUDGET,
                "first_paint_chat_script_budget": (
                    sum(1 for url in initial_request_urls if "/static/js/chat/" in url) <= FIRST_PAINT_SCRIPT_BUDGET
                ),
                "debug_log_not_loaded_on_first_paint": not any(
                    "/static/js/chat/debug_log.js?v=20260905-brand" in url for url in initial_request_urls
                ),
                "debug_log_loaded_on_open": debug_log_lazy_loaded,
                "file_manager_open": file_manager_open,
                "file_manager_not_loaded_on_first_paint": not any(
                    "/static/js/chat/file_manager.js?v=20260905-brand" in url for url in initial_request_urls
                ),
                "file_manager_loaded_on_open": file_manager_lazy_loaded,
                "voice_not_loaded_on_first_paint": not any(
                    "/static/js/chat/voice.js?v=20260905-brand" in url for url in initial_request_urls
                ),
                "voice_loaded_on_request": voice_lazy_loaded,
                "whisper_toy_not_loaded_on_first_paint": not any(
                    "/static/js/chat/whisper_toy.js?v=20260905-brand" in url for url in initial_request_urls
                ),
                "whisper_toy_loaded_on_open": whisper_lazy_loaded,
                "chatapp_api_registered": bool(
                    core.get("chatApp")
                    and core["chatApp"].get("hasRegister")
                    and core["chatApp"].get("hasGet")
                    and {
                        "attachments",
                        "aiDomLoader",
                        "configPanel",
                        "core",
                        "debugLogLoader",
                        "fileManagerLoader",
                        "lifecycle",
                        "messageActions",
                        "messageMenu",
                        "musicCards",
                        "render",
                        "sidebar",
                        "send",
                        "state",
                        "toyBridge",
                        "tts",
                        "voiceLoader",
                        "whisperToyLoader",
                    }.issubset(set(core["chatApp"].get("moduleNames", [])))
                ),
                "tts_module_still_lazy_before_config": (
                    core.get("chatApp", {}).get("ttsVoiceListLoaded") is False
                ),
                "state_boundary_registered": {
                    "currentConvId",
                    "currentMessages",
                    "sending",
                    "streamingAiId",
                    "msgMusicCards",
                    "systemLogs",
                    "voiceState",
                    "whisperState",
                    "aiDomState",
                }.issubset(set(core.get("chatApp", {}).get("stateOwnerKeys", []))),
                "state_snapshot_available": bool(
                    core.get("chatApp", {}).get("stateSnapshot")
                    and "conversationCount" in core["chatApp"]["stateSnapshot"]
                    and "messageCount" in core["chatApp"]["stateSnapshot"]
                    and "sending" in core["chatApp"]["stateSnapshot"]
                ),
                "aidom_not_loaded_on_first_paint": not any(
                    "/static/js/chat/ai_dom.js?v=20260905-brand" in url for url in initial_request_urls
                ),
                "aidom_loaded_on_open": aidom_lazy_loaded,
                "synthetic_conversation_rendered": bool(
                    synthetic_core.get("convActive")
                    and synthetic_core.get("userRow")
                    and synthetic_core.get("aiRow")
                ),
                "message_menu_open": message_menu_open,
                "message_edit_open": message_edit_open,
                "message_edit_cancel": message_edit_cancel,
                "music_card_rendered": music_card_rendered,
                "music_candidate_rendered": music_candidate_rendered,
                "syslog_detail_toggle": syslog_detail_toggle,
                "mock_send_fetch_called": mock_send_core.get("fetchCalled") is True,
                "mock_send_request_body": mock_send_core.get("requestContent") == "stream hello",
                "mock_send_user_optimistic": mock_send_core.get("userOptimistic") is True,
                "mock_send_ai_streamed": mock_send_core.get("aiContent") == "Hello world",
                "mock_send_debug_logged": mock_send_core.get("debugLogged") is True,
                "mock_send_music_state": mock_send_core.get("musicState") is True,
                "mock_send_music_rendered": mock_send_core.get("musicRendered") is True,
                "mock_send_music_autoplay": mock_send_core.get("musicAutoplay") == "789123",
                "mock_send_toy_without_metadata_blocked": "1" not in (mock_send_core.get("toyCommands") or []),
                "mock_send_toy_with_metadata_executed": "2" in (mock_send_core.get("toyCommands") or []),
                "mock_send_heart_hint": mock_send_core.get("heartHint") is True,
                "mock_send_tts_clean_text": (
                    mock_send_core.get("ttsText") == "Hello world"
                    and mock_send_core.get("ttsMsgId") == "smoke_send_ai"
                ),
                "mock_send_settled": mock_send_core.get("sendSettled") is True,
                "dynamic_inline_handlers_absent": dynamic_inline_handler_count == 0,
            },
            "screenshot": str(screenshot),
        }
    )
    return result


def run_mobile(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    result = _base_result("mobile")
    page.on("console", lambda msg: result["console_errors"].append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: result["page_errors"].append(str(exc)))
    page.on("requestfailed", lambda req: result["request_failures"].append(_record_request_failure(req)))

    page.goto(f"{base_url}/chat", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_selector("#messages", timeout=10_000)
    page.wait_for_selector("#input", timeout=10_000)
    page.wait_for_timeout(1_000)

    core = page.evaluate(
        """() => ({
          width: window.innerWidth,
          docScrollWidth: document.documentElement.scrollWidth,
          bodyScrollWidth: document.body.scrollWidth,
          noHorizontalOverflow: document.documentElement.scrollWidth <= window.innerWidth + 2,
          menuDisplay: getComputedStyle(document.querySelector('.menu-btn')).display,
          sidebarInitiallyOpen: document.querySelector('#sidebar').classList.contains('open'),
          chatPerfCount: window.ChatPerf && window.ChatPerf.getMarks ? window.ChatPerf.getMarks().length : 0,
          inputExists: !!document.querySelector('#input'),
          messagesExists: !!document.querySelector('#messages'),
        })"""
    )

    page.click(".menu-btn")
    sidebar_open = page.locator("#sidebar.open").count() == 1
    overlay_show = page.locator("#overlay.show").count() == 1
    page.click("#overlay")
    sidebar_closed = page.locator("#sidebar.open").count() == 0
    page.fill("#input", "smoke test text")
    input_state = page.evaluate(
        """() => ({
          value: document.querySelector('#input').value,
          sendDisabled: document.querySelector('#sendBtn').disabled,
        })"""
    )

    screenshot = screenshot_dir / "obsidianvow-chat-smoke-mobile.png"
    page.screenshot(path=str(screenshot), full_page=True)

    result.update(
        {
            "core": core,
            "checks": {
                "sidebar_open": sidebar_open,
                "overlay_show": overlay_show,
                "sidebar_closed": sidebar_closed,
                "input_accepts_text": input_state["value"] == "smoke test text",
            },
            "input_state": input_state,
            "screenshot": str(screenshot),
        }
    )
    return result


def failed_checks(results: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for result in results:
        name = result["name"]
        for key, value in result.get("checks", {}).items():
            if key.endswith("_src"):
                continue
            if value is not True:
                failures.append(f"{name}:{key}")
        core = result.get("core", {})
        if name == "desktop":
            required = ("inputExists", "messagesExists", "sendBtnExists", "convListExists", "linkCss")
            if (
                any(not core.get(key) for key in required)
                or core.get("styleTagCount") != 0
                or core.get("inlineStyleAttrCount") != 0
                or core.get("inlineHandlerAttrCount") != 0
            ):
                failures.append("desktop:core_dom")
        if name == "mobile":
            required = ("inputExists", "messagesExists", "noHorizontalOverflow")
            if any(not core.get(key) for key in required):
                failures.append("mobile:core_layout")
        if result["console_errors"]:
            failures.append(f"{name}:console_errors")
        if result["page_errors"]:
            failures.append(f"{name}:page_errors")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Run browser smoke checks for /chat.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--screenshot-dir", default="/tmp")
    parser.add_argument("--skip-mobile", action="store_true")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    screenshot_dir = Path(args.screenshot_dir)
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
      browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
      desktop = browser.new_page(viewport={"width": 1280, "height": 800})
      results = [run_desktop(desktop, base_url, screenshot_dir)]
      desktop.close()
      if not args.skip_mobile:
          mobile = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
          results.append(run_mobile(mobile, base_url, screenshot_dir))
          mobile.close()
      browser.close()

    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    failures = failed_checks(results)
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("All frontend smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Obsidian Vow — 入口文件
FastAPI app 创建、lifespan、静态文件挂载、路由注册
"""

import sys

if sys.version_info < (3, 10):
    raise RuntimeError(
        "Obsidian Vow backend requires Python 3.10+; "
        f"current interpreter is {sys.version.split()[0]}. "
        "Use Python 3.11 to match the Docker runtime."
    )

import asyncio, hmac, json, logging, os, time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse, RedirectResponse

# 过滤高频轮询路径的 access log，避免淹没有用的日志
class _QuietCamFilter(logging.Filter):
    _noisy = ("/api/cam/frame", "/api/cam/status")
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._noisy)

logging.getLogger("uvicorn.access").addFilter(_QuietCamFilter())
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from config import BASE_DIR, PUBLIC_DIR, UPLOADS_DIR, SCREENSHOTS_DIR, SETTINGS, TEST_MODE, load_ai_behavior
from database import get_db, init_db
from app.background_tasks import begin_task_lifecycle, create_tracked_task
from app.lifecycle import RuntimeResources
from ws import manager
from app.devices import device_service
from sentinel_runtime import sentinel_runtime
from voice import voice
from schedule import schedule_mgr, catch_up_missed_alarms
from _supervisor import Watchdog
from cleanup import run_startup_cleanup
from opportunity import opportunity_runner
from app.daily_signals import reconcile_daily_signals, run_daily_signal_reconcile_loop
from app.daily_signals.config import daily_timezone_name

from routes import chat, cam as cam_routes, files, settings, memories
from routes import image_memory as image_memory_routes
from routes import modes as modes_routes
from routes import devices as devices_routes
from routes import control as control_routes
from routes import voice as voice_routes
from routes import music as music_routes
from routes import schedule as schedule_routes
from routes import location as location_routes
from routes import heart_whispers as heart_whispers_routes
from routes import activity as activity_routes
from routes import sensing as sensing_routes
from routes import sentinel as sentinel_routes
from routes import events as events_routes
from routes import avatars as avatars_routes
from routes import pc_screen as pc_screen_routes
from routes import mobile_screen as mobile_screen_routes
from routes import vows as vows_routes
from routes import presence as presence_routes
from routes import push as push_routes


_RING_KEEPALIVE_COOLDOWN_SEC = 60.0
_ring_keepalive_last_sent_at = 0.0


def _should_send_ring_keepalive(device_type: str | None, metadata: dict) -> bool:
    global _ring_keepalive_last_sent_at
    if device_type != "smart_ring":
        return False
    if not SETTINGS.get("smart_ring_keep_connected", False):
        return False
    if metadata.get("ble_connected") is True:
        return False
    # keep-connected is an explicit retry policy; one transient scan/connect
    # error should not disable future reconnect attempts.
    now = time.time()
    if now - _ring_keepalive_last_sent_at < _RING_KEEPALIVE_COOLDOWN_SEC:
        return False
    _ring_keepalive_last_sent_at = now
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    resources = RuntimeResources()
    app.state.runtime_resources = resources
    app.state.ready = False
    begin_task_lifecycle()
    try:
        async with _runtime_lifespan(app):
            app.state.ready = True
            yield
    finally:
        app.state.ready = False
        await resources.shutdown(timeout=5.0)


@asynccontextmanager
async def _runtime_lifespan(app: FastAPI):
    from app.tools.registry import validate_tool_registry

    validate_tool_registry()
    await init_db()
    if TEST_MODE:
        # 隔离检查只初始化临时数据库，不重放任务、启动线程或执行清理。
        yield
        return
    from app.presence import sprite_library

    await sprite_library.ensure_seed_sprites()
    await sprite_library.reconcile_seed_lifecycle(device_id="pc")
    from app.web_search import web_search_service
    await web_search_service.resume_queued()
    # First migration/startup preserves every raw day still on disk in its
    # durable row before the configured retention policy prunes old files.
    try:
        print(f"[DailySignals] authoritative timezone={daily_timezone_name()}")
        await asyncio.to_thread(reconcile_daily_signals)
    except Exception as e:
        print(f"[DailySignals] startup reconcile failed: {e}")
    try:
        run_startup_cleanup()
    except Exception as e:
        print(f"[Cleanup] startup prune failed: {e}")
    from app.pc_screen.service import cleanup_expired_files as cleanup_screen_files
    from app.mobile_screen import mobile_screen_service
    def cleanup_all_screen_files():
        cleanup_screen_files()
        mobile_screen_service.cleanup_expired_files()
    try:
        cleanup_all_screen_files()
    except Exception as e:
        print(f"[PCScreen] startup cleanup failed: {e}")
    loop = asyncio.get_event_loop()
    resources = app.state.runtime_resources
    # 哨兵：文字 timeline 版，无需打开摄像头
    resources.add_stop("sentinel", sentinel_runtime.stop_monitoring)
    sentinel_runtime.set_event_loop(loop)
    sentinel_runtime.start_monitoring()
    # 语音模块初始化
    resources.add_stop("voice", voice.stop)
    voice.set_event_loop(loop)
    voice.set_ws_manager(manager)
    # 日程/闹铃模块初始化
    resources.add_stop("schedule", schedule_mgr.stop)
    schedule_mgr.set_event_loop(loop)
    # 先补扫错过的闹铃再启动循环，避免启动瞬间一次性轰炸多个通知
    try:
        await catch_up_missed_alarms()
    except Exception as e:
        print(f"[Schedule] catch-up failed: {e}")
    schedule_mgr.start()
    # PC activity V1 uses a remote PC agent; the legacy local tracker stays off.

    # 机会机制：Fibonacci 间隔的自由意志窗口
    if load_ai_behavior().get("opportunity_enabled", False):
        resources.add_stop("opportunity", opportunity_runner.stop)
        opportunity_runner.start()

    # 看门狗：定期检查关键后台线程，挂了自动重启
    watchdog = Watchdog(poll_interval=30.0)
    resources.add_stop("watchdog", watchdog.stop)

    def _voice_alive():
        # 只在用户开启了语音时才看护
        if not voice.enabled:
            return True
        return voice._thread is not None and voice._thread.is_alive()

    def _voice_restart():
        voice.start(voice.wake_word)

    def _sentinel_alive():
        if not sentinel_runtime.monitoring:
            return True
        return (
            sentinel_runtime._monitor_thread is not None
            and sentinel_runtime._monitor_thread.is_alive()
        )

    def _sentinel_restart():
        sentinel_runtime.monitoring = False  # 让 start 真正重建线程
        sentinel_runtime.start_monitoring()

    def _sched_alive():
        if not schedule_mgr._running:
            return True
        return schedule_mgr._thread is not None and schedule_mgr._thread.is_alive()

    def _sched_restart():
        schedule_mgr._running = False
        schedule_mgr.start()

    watchdog.register("voice", _voice_alive, _voice_restart)
    watchdog.register("sentinel", _sentinel_alive, _sentinel_restart)
    watchdog.register("schedule", _sched_alive, _sched_restart)
    create_tracked_task(watchdog.run(), name="watchdog")
    create_tracked_task(run_daily_signal_reconcile_loop(), name="daily_signals")
    async def _opportunity_loop():
        while True:
            await asyncio.sleep(10)
            try:
                await opportunity_runner.maybe_fire()
            except Exception as e:
                print(f"[Opportunity] loop error: {e}")
    create_tracked_task(_opportunity_loop(), name="opportunity")

    from app.presence.night_round import night_round_scheduler
    from app.presence.summon import summon_coordinator

    await summon_coordinator.recover_after_restart()
    await night_round_scheduler.recover_after_restart()
    create_tracked_task(
        night_round_scheduler.run_loop(poll_interval_sec=30.0), name="presence_night_round",
    )

    from app.self_wake.service import run_scan_loop as run_self_wake_scan_loop

    create_tracked_task(run_self_wake_scan_loop(interval_sec=10.0), name="self_wake_scan")

    from context_delivery_shadow_runtime import run_context_trigger_outcome_loop

    create_tracked_task(
        run_context_trigger_outcome_loop(), name="context_trigger_outcomes",
    )

    from app.memory_v3.card_generation import run_relational_card_generation_loop

    create_tracked_task(
        run_relational_card_generation_loop(interval_sec=300.0), name="memory_relational_cards",
    )

    from app.presence import presence_service

    create_tracked_task(
        presence_service.run_lease_cleanup_loop(interval_sec=5.0), name="presence_leases",
    )

    async def _screen_cleanup_loop():
        while True:
            await asyncio.sleep(900)
            cleanup_all_screen_files()
    create_tracked_task(_screen_cleanup_loop(), name="screen_cleanup")

    yield

app = FastAPI(lifespan=lifespan)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    if not getattr(app.state, "ready", False):
        return JSONResponse({"status": "not_ready"}, status_code=503)

    async def check_database():
        async with get_db(timeout=0.2, read_only=True) as db:
            await db.execute("SELECT id FROM conversations LIMIT 1")

    try:
        await asyncio.wait_for(check_database(), timeout=1.0)
    except Exception as exc:
        logging.getLogger(__name__).warning("健康检查数据库不可读：%s", type(exc).__name__)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


# 静态资源缓存：页面保持短缓存，图片/CSS/JS 用较长缓存减少公网重复下载。
_LONG_CACHE_PREFIXES = ("/public/optimized/",)
_STATIC_CACHE_PREFIXES = ("/static/", "/public/")
_STATIC_CACHE_EXTS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    ".ico", ".mp3", ".woff", ".woff2",
)


def _static_cache_header(path: str) -> str:
    if path.startswith(_LONG_CACHE_PREFIXES):
        return "public, max-age=604800, immutable"
    if path.startswith(_STATIC_CACHE_PREFIXES) and path.lower().endswith(_STATIC_CACHE_EXTS):
        return "public, max-age=86400"
    return ""


# ── 鉴权中间件：单人部署的极简 Bearer Token ────────────────────────
# AION_AUTH_TOKEN 为空 → 完全关闭鉴权（保持本地开发零配置）
# 否则全站要求 token，以下三种任一通过：
#   1. Cookie aion_token=<TOKEN>
#   2. Header Authorization: Bearer <TOKEN>
#   3. Query ?token=<TOKEN>（首次访问后会写 cookie 并跳转到去掉 token 的 URL）
_AUTH_TOKEN = (os.environ.get("AION_AUTH_TOKEN") or "").strip()
_AUTH_COOKIE = "aion_token"
# 这些路径永远放行，避免 PWA / 静态页加载失败
_AUTH_PUBLIC_PREFIXES = ("/static/", "/public/")
_AUTH_PUBLIC_EXACT = {"/sw.js", "/manifest.json", "/favicon.ico", "/download-apk", "/healthz"}


def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _check_token(request: Request) -> bool:
    cookie = request.cookies.get(_AUTH_COOKIE, "")
    if cookie and _safe_eq(cookie, _AUTH_TOKEN):
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and _safe_eq(auth[7:].strip(), _AUTH_TOKEN):
        return True
    return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _AUTH_TOKEN:
        return await call_next(request)
    path = request.url.path
    if path in _AUTH_PUBLIC_EXACT or any(path.startswith(p) for p in _AUTH_PUBLIC_PREFIXES):
        return await call_next(request)
    if _check_token(request):
        return await call_next(request)
    # ?token=xxx 首次进入：校验通过则写 cookie 并 303 到去掉 token 的 URL
    qtoken = request.query_params.get("token")
    if qtoken and _safe_eq(qtoken, _AUTH_TOKEN):
        kept = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
        new_query = "&".join(f"{k}={v}" for k, v in kept)
        new_url = path + (f"?{new_query}" if new_query else "")
        resp = RedirectResponse(new_url, status_code=303)
        # max_age = 一年；HttpOnly 防 JS 偷；SameSite=Lax 适合普通浏览
        resp.set_cookie(_AUTH_COOKIE, _AUTH_TOKEN,
                        max_age=365 * 24 * 3600, httponly=True, secure=True, samesite="lax")
        return resp
    return JSONResponse({"detail": "unauthorized"}, status_code=401)


@app.middleware("http")
async def static_cache_middleware(request: Request, call_next):
    response = await call_next(request)
    cache = _static_cache_header(request.url.path)
    if cache and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = cache
    return response


# 静态文件
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
app.mount("/public", StaticFiles(directory=str(PUBLIC_DIR)), name="public")
app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")

# 路由
app.include_router(chat.router)
app.include_router(cam_routes.router)
app.include_router(files.router)
app.include_router(settings.router)
app.include_router(memories.router)
app.include_router(image_memory_routes.router)
app.include_router(modes_routes.router)
app.include_router(devices_routes.router)
app.include_router(control_routes.router)
app.include_router(voice_routes.router)
app.include_router(music_routes.router)
app.include_router(schedule_routes.router)
app.include_router(location_routes.router)
app.include_router(heart_whispers_routes.router)
app.include_router(activity_routes.router)
app.include_router(sensing_routes.router)
app.include_router(sentinel_routes.router)
app.include_router(events_routes.router)
app.include_router(avatars_routes.router)
app.include_router(pc_screen_routes.router)
app.include_router(mobile_screen_routes.router)
app.include_router(vows_routes.router)
app.include_router(presence_routes.router)
app.include_router(push_routes.router)


# 页面
@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "static" / "home.html")

# 页面缓存策略：短缓存 + must-revalidate，浏览器在缓存有效期内直接用本地版本
# 更新代码后重启容器 → ETag 会变 → 下次 revalidate 自动拿新版
_PAGE_CACHE = {"Cache-Control": "public, max-age=300, must-revalidate"}

@app.get("/chat")
async def chat_page():
    return FileResponse(BASE_DIR / "static" / "chat.html", headers=_PAGE_CACHE)

@app.get("/settings")
async def settings_page():
    return FileResponse(BASE_DIR / "static" / "settings.html", headers=_PAGE_CACHE)

@app.get("/worldbook")
async def worldbook_page():
    return FileResponse(BASE_DIR / "static" / "worldbook.html", headers=_PAGE_CACHE)

@app.get("/memory")
async def memory_page():
    return FileResponse(BASE_DIR / "static" / "memory.html", headers=_PAGE_CACHE)

@app.get("/schedule")
async def schedule_page():
    return FileResponse(BASE_DIR / "static" / "schedule.html", headers=_PAGE_CACHE)

@app.get("/camera")
async def camera_page():
    return FileResponse(BASE_DIR / "static" / "camera.html", headers=_PAGE_CACHE)

@app.get("/monitor-logs")
async def monitor_logs_page():
    return FileResponse(BASE_DIR / "static" / "monitor-logs.html", headers=_PAGE_CACHE)

@app.get("/location")
async def location_page():
    return FileResponse(BASE_DIR / "static" / "location.html", headers=_PAGE_CACHE)

@app.get("/devices")
async def devices_page():
    return FileResponse(BASE_DIR / "static" / "devices.html", headers=_PAGE_CACHE)

@app.get("/heart-whispers")
async def heart_whispers_page():
    return FileResponse(BASE_DIR / "static" / "heart-whispers.html", headers=_PAGE_CACHE)

@app.get("/activity-logs")
async def activity_logs_page():
    return FileResponse(BASE_DIR / "static" / "activity-logs.html", headers=_PAGE_CACHE)

@app.get("/vows")
async def vows_page():
    # 誓约管理嵌在记忆库页的"誓约"tab；设计 §8 的 /vows 入口保留为跳转
    return RedirectResponse("/memory#vows")

# PWA：Service Worker 必须从根路径提供，作用域才能覆盖所有页面
@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")

@app.get("/manifest.json")
async def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.json", media_type="application/manifest+json")


@app.get("/download-apk")
async def download_apk():
    return FileResponse(
        BASE_DIR / "static" / "obsidianvow-debug.apk",
        media_type="application/vnd.android.package-archive",
        filename="obsidianvow-debug.apk",
    )

# WebSocket
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if _AUTH_TOKEN:
        cookie = ws.cookies.get(_AUTH_COOKIE, "")
        qtoken = ws.query_params.get("token", "")
        auth = ws.headers.get("authorization", "")
        ok = ((cookie and _safe_eq(cookie, _AUTH_TOKEN))
              or (qtoken and _safe_eq(qtoken, _AUTH_TOKEN))
              or (auth.startswith("Bearer ") and _safe_eq(auth[7:].strip(), _AUTH_TOKEN)))
        if not ok:
            await ws.close(code=4401)
            return
    await manager.connect(ws)
    try:
        while True:
            text = await ws.receive_text()
            # 处理来自客户端的 ping 心跳（Android 推送服务定期发送）
            try:
                msg = json.loads(text)
                msg_type = msg.get("type")
                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                if msg_type == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                elif msg_type == "device_state_report":
                    device_type = data.get("device_type") or data.get("driver_id")
                    manager.register_device_ws(device_type, ws)
                    device_id = str(data.get("device_id") or device_type or "").strip()
                    if device_id:
                        metadata = dict(data.get("metadata") or {})
                        if device_type:
                            metadata["device_type"] = device_type
                        await device_service.report_state(
                            device_id,
                            status=str(data.get("status") or "online"),
                            name=data.get("name"),
                            kind=data.get("kind"),
                            capabilities=data.get("capabilities") if isinstance(data.get("capabilities"), list) else None,
                            battery=data.get("battery") if isinstance(data.get("battery"), int) else None,
                            metadata=metadata,
                        )
                        if _should_send_ring_keepalive(device_type, metadata):
                            await device_service.execute_command(device_id, "keepalive")
                elif msg_type == "ring_touch_ack":
                    driver = device_service.get_driver("smart_ring")
                    handler = getattr(driver, "handle_ack", None)
                    if handler is not None:
                        await handler(data)
            except (json.JSONDecodeError, Exception):
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.getLogger("ws").warning("WS endpoint error: %s", e)
    finally:
        manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    import sys
    # 容器内默认 0.0.0.0；本机直接 python main.py 也保持原行为
    bind_host = os.environ.get("AION_BIND_HOST", "0.0.0.0")
    bind_port = int(os.environ.get("AION_BIND_PORT", "18080"))
    reload = "--reload" in sys.argv
    uvicorn.run("main:app", host=bind_host, port=bind_port, reload=reload,
                # 反代部署时让 starlette 信任 X-Forwarded-* 头
                forwarded_allow_ips="*", proxy_headers=True)

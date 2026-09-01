"""
全局配置：路径、常量、settings / worldbook / chat_status 读写
"""

from __future__ import annotations

import json, time, re, os
from pathlib import Path

# ── 路径 ─────────────────────────────────────────
BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR.parent / "public"
DATA_DIR = BASE_DIR / "data"

# ── .env 加载（放最前面，让后续所有 SETTINGS / get_key 都能读到）──
# 查找顺序：项目根 .env → aion-chat/.env。有 python-dotenv 就用，没有就手工读。
def _load_dotenv_safe():
    candidates = [BASE_DIR.parent / ".env", BASE_DIR / ".env"]
    try:
        from dotenv import load_dotenv
        for p in candidates:
            if p.exists():
                load_dotenv(p, override=False)
                return
    except Exception:
        # 手工解析：KEY=VALUE，忽略 # 注释与空行，不支持引号转义
        for p in candidates:
            if not p.exists():
                continue
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            except Exception:
                pass
            return

_load_dotenv_safe()


def _env(*names) -> str:
    """按顺序取第一个非空的环境变量。"""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return ""



DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "chat.db"
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
CHATS_DIR = DATA_DIR / "chats"
CHATS_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
MONITOR_LOGS_DIR = DATA_DIR / "monitor_logs"
MONITOR_LOGS_DIR.mkdir(exist_ok=True)
TTS_CACHE_DIR = DATA_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)

SETTINGS_PATH = DATA_DIR / "settings.json"
WORLDBOOK_PATH = DATA_DIR / "worldbook.json"
CHAT_STATUS_PATH = DATA_DIR / "chat_status.json"
CAM_CONFIG_PATH = DATA_DIR / "cam_config.json"
AI_BEHAVIOR_PATH = DATA_DIR / "ai_behavior.json"
DIGEST_ANCHOR_PATH = DATA_DIR / "digest_anchor.json"
WORKING_MODEL_PATH = DATA_DIR / "working_model.json"
WORKING_MODEL_MIGRATION_REPORT_PATH = DATA_DIR / "working_model_v2_migration_report.json"
WORKING_MODEL_MAX_CHARS = 1500
WORKING_MODEL_PROMPT_MAX_CHARS = 1200
INDEX_PATH = CHATS_DIR / "_index.json"

# ── 誓约层预算与限流（VOW_LAYER_DESIGN.md §6）──
VOW_ACTIVE_MAX = 30                 # active 上限，超限拒绝
VOW_CONTENT_MAX_CHARS = 240         # 单条内容上限（len()），超限拒绝不截断
VOW_AFFIRMATION_MAX_CHARS = 120     # 确认语上限，超限拒绝不截断
VOW_TOTAL_ACTIVE_CHARS = 3600       # active 总字符预算
VOW_AI_DAILY_LIMIT = 1              # AI 自动立约每日条数
VOW_REASON_MAX_CHARS = 240          # 修订/退役原因上限（len()），超限拒绝不截断
VOW_DAILY_TZ = "Asia/Shanghai"      # 每日限流计日时区

DEFAULT_SENTINEL_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_MEMORY_DIGEST_MODEL = DEFAULT_SENTINEL_MODEL
DEFAULT_HARNESS_TOOL_MODEL = DEFAULT_SENTINEL_MODEL
DEFAULT_WORKING_MODEL_GATE_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_RELATIONAL_CARD_GENERATION_MODEL = "deepseek-ai/DeepSeek-V3.2"
# Keep the empty value for installs without a verified direct Gemini key.
# A one-time migration below binds the renderer only when it adds that direct
# endpoint itself, so an owner can still disable the feature by clearing it.
DEFAULT_PRESENCE_RENDERER_MODEL = ""
VERIFIED_PRESENCE_RENDERER_MODEL = "gemini-2.5-flash"
PRESENCE_RENDERER_GEMINI_MIGRATION_KEY = (
    "presence_renderer_gemini_migration_v1"
)
DEFAULT_PRESENCE_IMAGE_MODEL = "gemini-3.1-flash-image"
PRESENCE_IMAGE_SLOT_MIGRATION_KEY = "presence_image_slot_migration_v1"
DEFAULT_ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"

# Session/UI state must never become durable configuration.  Keep this list at
# the settings boundary so an unrelated save cannot persist a stale value.
NON_PERSISTENT_SETTINGS_KEYS = frozenset({"whisper_active"})

# ── Settings ─────────────────────────────────────
def load_settings():
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"gemini_key": "", "siliconflow_key": "", "gemini_free_key": "", "aipro_key": "", "tavily_api_key": ""}
        txt = BASE_DIR.parent / "所需要的API.txt"
        if txt.exists():
            with open(txt, "r", encoding="utf-8") as f:
                for line in f:
                    if "gemini-api" in line.lower():
                        data["gemini_key"] = line.split("：")[-1].strip()
                    elif "硅基流动" in line.lower() and "api" in line.lower():
                        data["siliconflow_key"] = line.split("：")[-1].strip()
    removed_transient = False
    for key in NON_PERSISTENT_SETTINGS_KEYS:
        if key in data:
            data.pop(key, None)
            removed_transient = True
    # 端点池 + slot 迁移（老配置无感升级）
    migrated = _ensure_endpoints_and_slots(data)
    if migrated or removed_transient:
        save_settings(data)
    return data

def save_settings(data: dict):
    persistent = {
        key: value
        for key, value in data.items()
        if key not in NON_PERSISTENT_SETTINGS_KEYS
    }
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(persistent, f, ensure_ascii=False, indent=2)

def _ensure_endpoints_and_slots(data: dict) -> bool:
    """老 settings 没有 endpoints / slots 字段时，根据旧 key 生成默认值。
    返回是否改动过。"""
    changed = False
    gemini_migration_done = bool(
        data.get(PRESENCE_RENDERER_GEMINI_MIGRATION_KEY)
    )
    presence_image_migration_done = bool(
        data.get(PRESENCE_IMAGE_SLOT_MIGRATION_KEY)
    )
    gemini_endpoint_added = False
    if "endpoints" not in data:
        data["endpoints"] = []
        if data.get("siliconflow_key"):
            data["endpoints"].append({
                "id": "sf", "name": "硅基流动",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": data["siliconflow_key"], "type": "openai",
            })
        if data.get("gemini_key"):
            data["endpoints"].append({
                "id": "gem", "name": "Gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": data["gemini_key"], "type": "gemini",
            })
            gemini_endpoint_added = True
        if data.get("aipro_key"):
            data["endpoints"].append({
                "id": "pro", "name": "AiPro中转",
                "base_url": "https://vip.aipro.love/v1",
                "api_key": data["aipro_key"], "type": "openai",
            })
        changed = True
    # A DeepSeek gate needs an OpenAI-compatible endpoint.  Existing installs
    # may already have an endpoint pool (for example Vertex only) while still
    # keeping the legacy SiliconFlow credential, so the original all-or-
    # nothing endpoint migration would otherwise bind the new model to an
    # incompatible provider.
    siliconflow_endpoint = next(
        (
            endpoint
            for endpoint in data["endpoints"]
            if (endpoint.get("type") or "openai") == "openai"
            and (
                endpoint.get("id") == "sf"
                or "api.siliconflow.cn" in str(endpoint.get("base_url") or "")
            )
        ),
        None,
    )
    if siliconflow_endpoint is None and (
        data.get("siliconflow_key") or _env("AION_SILICONFLOW_KEY")
    ) and not any(endpoint.get("id") == "sf" for endpoint in data["endpoints"]):
        siliconflow_endpoint = {
            "id": "sf",
            "name": "硅基流动",
            "base_url": "https://api.siliconflow.cn/v1",
            "api_key": data.get("siliconflow_key", ""),
            "type": "openai",
        }
        data["endpoints"].append(siliconflow_endpoint)
        changed = True
    gemini_endpoint = next(
        (
            endpoint
            for endpoint in data["endpoints"]
            if endpoint.get("type") == "gemini"
        ),
        None,
    )
    gemini_key_available = bool(
        data.get("gemini_key") or _env("AION_GEMINI_KEY")
    )
    existing_slots = data.get("slots")
    existing_presence_slot = (
        existing_slots.get("presence_renderer")
        if isinstance(existing_slots, dict)
        else None
    )
    presence_image_slot_existed = bool(
        isinstance(existing_slots, dict) and "presence_image" in existing_slots
    )
    existing_presence_image_slot = (
        existing_slots.get("presence_image")
        if presence_image_slot_existed
        else None
    )
    presence_needs_verified_endpoint = (
        not existing_presence_slot
        or not str(existing_presence_slot.get("model") or "").strip()
    )
    presence_image_needs_gemini_endpoint = (
        not existing_presence_image_slot
        or not str(existing_presence_image_slot.get("model") or "").strip()
    )
    if (
        gemini_endpoint is None
        and gemini_key_available
        and (
            (
                presence_needs_verified_endpoint
                and not gemini_migration_done
            )
            or (
                presence_image_needs_gemini_endpoint
                and not presence_image_migration_done
            )
        )
    ):
        used_ids = {str(endpoint.get("id") or "") for endpoint in data["endpoints"]}
        endpoint_id = "gem" if "gem" not in used_ids else "gemini"
        if endpoint_id in used_ids:
            endpoint_id = "gemini_direct"
        gemini_endpoint = {
            "id": endpoint_id,
            "name": "Gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": data.get("gemini_key", ""),
            "type": "gemini",
        }
        data["endpoints"].append(gemini_endpoint)
        gemini_endpoint_added = True
        changed = True
    # 默认指向 sf（若无则第一条 openai 端点）
    default_ep = "sf"
    if not any(e["id"] == "sf" for e in data["endpoints"]):
        default_ep = next((e["id"] for e in data["endpoints"] if e.get("type") == "openai"),
                          data["endpoints"][0]["id"] if data["endpoints"] else "")
    if not isinstance(data.get("slots"), dict):
        data["slots"] = {}
        changed = True
    slots = data["slots"]
    if "sentinel" not in slots:
        slots["sentinel"] = {"endpoint": default_ep, "model": DEFAULT_SENTINEL_MODEL}
        changed = True
    if "memory_digest" not in slots:
        slots["memory_digest"] = {"endpoint": default_ep, "model": DEFAULT_MEMORY_DIGEST_MODEL}
        changed = True
    if "harness_tool" not in slots:
        sentinel_slot = slots.get("sentinel") or {}
        slots["harness_tool"] = {
            # Start as an independent copy of the already-working cheap slot.
            # Existing ring translation therefore does not silently change
            # provider/model during migration; owners can tune it afterward.
            "endpoint": sentinel_slot.get("endpoint") or default_ep,
            "model": sentinel_slot.get("model") or DEFAULT_HARNESS_TOOL_MODEL,
        }
        changed = True
    if "ring_touch_translator" not in slots:
        harness_slot = slots.get("harness_tool") or {}
        slots["ring_touch_translator"] = {
            # Keep model/provider ownership in settings.  The dedicated slot
            # starts as a compatibility copy and can be tuned independently.
            "endpoint": harness_slot.get("endpoint") or default_ep,
            "model": harness_slot.get("model") or DEFAULT_HARNESS_TOOL_MODEL,
        }
        changed = True
    gate_default_ep = (
        str((siliconflow_endpoint or {}).get("id") or "")
        or next(
            (
                str(endpoint.get("id") or "")
                for endpoint in data["endpoints"]
                if endpoint.get("type") == "openai"
            ),
            "",
        )
    )
    if "working_model_gate" not in slots:
        slots["working_model_gate"] = {
            "endpoint": gate_default_ep,
            "model": DEFAULT_WORKING_MODEL_GATE_MODEL,
        }
        changed = True
    else:
        gate_slot = slots["working_model_gate"]
        gate_endpoint = next(
            (
                endpoint
                for endpoint in data["endpoints"]
                if endpoint.get("id") == gate_slot.get("endpoint")
            ),
            None,
        )
        auto_generated_but_incompatible = (
            gate_slot.get("model") == DEFAULT_WORKING_MODEL_GATE_MODEL
            and (gate_endpoint or {}).get("type", "openai") != "openai"
        )
        if auto_generated_but_incompatible and gate_default_ep:
            gate_slot["endpoint"] = gate_default_ep
            changed = True
    if "relational_card_generation" not in slots:
        slots["relational_card_generation"] = {
            "endpoint": gate_default_ep,
            "model": DEFAULT_RELATIONAL_CARD_GENERATION_MODEL,
        }
        changed = True
    else:
        card_slot = slots["relational_card_generation"]
        card_endpoint = next(
            (
                endpoint
                for endpoint in data["endpoints"]
                if endpoint.get("id") == card_slot.get("endpoint")
            ),
            None,
        )
        auto_generated_but_incompatible = (
            card_slot.get("model") == DEFAULT_RELATIONAL_CARD_GENERATION_MODEL
            and (card_endpoint or {}).get("type", "openai") != "openai"
        )
        if auto_generated_but_incompatible and gate_default_ep:
            card_slot["endpoint"] = gate_default_ep
            changed = True
    if "presence_renderer" not in slots:
        bind_verified_gemini = gemini_endpoint_added and not gemini_migration_done
        slots["presence_renderer"] = {
            "endpoint": (
                str((gemini_endpoint or {}).get("id") or "")
                if bind_verified_gemini
                else gate_default_ep
            ),
            "model": (
                VERIFIED_PRESENCE_RENDERER_MODEL
                if bind_verified_gemini
                else DEFAULT_PRESENCE_RENDERER_MODEL
            ),
        }
        changed = True
    elif gemini_endpoint_added and not gemini_migration_done and not str(
        slots["presence_renderer"].get("model") or ""
    ).strip():
        slots["presence_renderer"]["endpoint"] = str(gemini_endpoint["id"])
        slots["presence_renderer"]["model"] = VERIFIED_PRESENCE_RENDERER_MODEL
        changed = True
    presence_slot = slots["presence_renderer"]
    verified_endpoint_id = str((gemini_endpoint or {}).get("id") or "")
    if (
        not gemini_migration_done
        and verified_endpoint_id
        and str(presence_slot.get("endpoint") or "") == verified_endpoint_id
        and str(presence_slot.get("model") or "").strip()
        == VERIFIED_PRESENCE_RENDERER_MODEL
    ):
        data[PRESENCE_RENDERER_GEMINI_MIGRATION_KEY] = True
        gemini_migration_done = True
        changed = True
    if not presence_image_migration_done:
        if presence_image_slot_existed:
            # A pre-existing slot is an owner choice.  Record the migration as
            # complete without changing either its endpoint or model.
            data[PRESENCE_IMAGE_SLOT_MIGRATION_KEY] = True
            presence_image_migration_done = True
            changed = True
        elif gemini_endpoint is not None:
            slots["presence_image"] = {
                "endpoint": str(gemini_endpoint.get("id") or ""),
                "model": DEFAULT_PRESENCE_IMAGE_MODEL,
            }
            data[PRESENCE_IMAGE_SLOT_MIGRATION_KEY] = True
            presence_image_migration_done = True
            changed = True
    if "asr" not in slots:
        slots["asr"] = {
            "endpoint": default_ep,
            "model": DEFAULT_ASR_MODEL,
            "path": "/audio/transcriptions",
        }
        changed = True
    if "user_models" not in data:
        data["user_models"] = {}
        changed = True
    if "screen_capture_enabled" not in data:
        data["screen_capture_enabled"] = False
        changed = True
    if "mobile_screen_capture_enabled" not in data:
        data["mobile_screen_capture_enabled"] = False
        changed = True
    if "smart_ring_touch_enabled" not in data:
        data["smart_ring_touch_enabled"] = False
        changed = True
    if "smart_ring_name_prefix" not in data:
        data["smart_ring_name_prefix"] = "AIZO"
        changed = True
    if "smart_ring_keep_connected" not in data:
        data["smart_ring_keep_connected"] = False
        changed = True
    if "smart_ring_quiet_hours_enabled" not in data:
        data["smart_ring_quiet_hours_enabled"] = False
        changed = True
    if "smart_ring_quiet_hours_start" not in data:
        data["smart_ring_quiet_hours_start"] = "00:00"
        changed = True
    if "smart_ring_quiet_hours_end" not in data:
        data["smart_ring_quiet_hours_end"] = "08:00"
        changed = True
    if "mock_devices_enabled" not in data:
        data["mock_devices_enabled"] = False
        changed = True
    return changed

SETTINGS = load_settings()

def _parse_hhmm(value: object, default: str) -> tuple[int, int]:
    text = str(value or default)
    try:
        hour, minute = text.split(":", 1)
        h = max(0, min(23, int(hour)))
        m = max(0, min(59, int(minute)))
        return h, m
    except (TypeError, ValueError):
        hour, minute = default.split(":", 1)
        return int(hour), int(minute)

def is_smart_ring_quiet_hours(data: dict | None = None) -> bool:
    cfg = SETTINGS if data is None else data
    if not cfg.get("smart_ring_quiet_hours_enabled", False):
        return False
    sh, sm = _parse_hhmm(cfg.get("smart_ring_quiet_hours_start"), "00:00")
    eh, em = _parse_hhmm(cfg.get("smart_ring_quiet_hours_end"), "08:00")
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end

def is_smart_ring_touch_active(data: dict | None = None) -> bool:
    cfg = SETTINGS if data is None else data
    enabled = cfg.get("smart_ring_touch_enabled", cfg.get("ring_touch_enabled", False))
    return bool(enabled) and not is_smart_ring_quiet_hours(cfg)

# ── 端点池辅助 ───────────────────────────────────
def get_endpoint(endpoint_id: str) -> dict | None:
    """按 id 查端点配置。环境变量 AION_ENDPOINT_{ID}_KEY 会覆盖 api_key。"""
    for ep in SETTINGS.get("endpoints", []):
        if ep.get("id") == endpoint_id:
            env_key = _env(f"AION_ENDPOINT_{str(endpoint_id).upper()}_KEY")
            if not env_key and (
                endpoint_id == "sf"
                or "api.siliconflow.cn" in str(ep.get("base_url") or "")
            ):
                env_key = _env("AION_SILICONFLOW_KEY") or SETTINGS.get(
                    "siliconflow_key", ""
                )
            if not env_key and ep.get("type") == "gemini":
                env_key = _env("AION_GEMINI_KEY") or SETTINGS.get(
                    "gemini_key", ""
                )
            if env_key:
                ep = dict(ep)
                ep["api_key"] = env_key
            return ep
    return None

def get_slot(slot_name: str) -> dict | None:
    """返回 {endpoint: <dict>, model: str, extras: {...}}；任何缺失都返回 None。"""
    slot = SETTINGS.get("slots", {}).get(slot_name)
    if not slot:
        return None
    ep = get_endpoint(slot.get("endpoint", ""))
    if not ep:
        return None
    extras = {k: v for k, v in slot.items() if k not in ("endpoint", "model")}
    return {"endpoint": ep, "model": slot.get("model", ""), "extras": extras}

def resolve_core_model(model_key: str) -> dict | None:
    """核心模型（主脑）解析：先查硬编码 MODELS，再查 user_models。
    返回 {provider|endpoint, model} 形式。"""
    if model_key in MODELS:
        cfg = dict(MODELS[model_key])
        cfg["_kind"] = "preset"
        return cfg
    um = SETTINGS.get("user_models", {}).get(model_key)
    if um:
        ep = get_endpoint(um.get("endpoint", ""))
        if ep:
            return {
                "_kind": "custom",
                "endpoint": ep,
                "model": um.get("model", ""),
                # Capability declarations are deliberately strict.  An omitted
                # value must never make the chat chain guess that audio works.
                "audio_input": um.get("audio_input") is True,
            }
    return None

def model_supports_audio_input(model_key: str) -> bool:
    cfg = resolve_core_model(model_key)
    return bool(cfg and cfg.get("audio_input") is True)

def list_core_models() -> list[str]:
    """给前端模型下拉用：预设 + 用户自定义 合并去重。"""
    names = list(MODELS.keys())
    for k in SETTINGS.get("user_models", {}).keys():
        if k not in names:
            names.append(k)
    return names

def get_key(provider: str) -> str:
    """优先读取 AION_* 环境变量；回退到 settings.json。"""
    if provider == "gemini":
        return _env("AION_GEMINI_KEY") or SETTINGS.get("gemini_key", "")
    if provider == "gemini_free":
        return (_env("AION_GEMINI_FREE_KEY")
                or SETTINGS.get("gemini_free_key", "")
                or _env("AION_GEMINI_KEY")
                or SETTINGS.get("gemini_key", ""))
    if provider == "aipro":
        return _env("AION_AIPRO_KEY") or SETTINGS.get("aipro_key", "")
    return _env("AION_SILICONFLOW_KEY") or SETTINGS.get("siliconflow_key", "")

# ── Worldbook ────────────────────────────────────
def load_worldbook():
    if WORLDBOOK_PATH.exists():
        try:
            return json.loads(WORLDBOOK_PATH.read_text(encoding='utf-8'))
        except:
            pass
    return {"ai_persona": "", "user_persona": "", "ai_name": "AI", "user_name": "你"}

def save_worldbook(data: dict):
    WORLDBOOK_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── Chat Status ──────────────────────────────────
def load_chat_status() -> dict:
    if CHAT_STATUS_PATH.exists():
        try:
            return json.loads(CHAT_STATUS_PATH.read_text(encoding='utf-8'))
        except:
            pass
    return {"status": "", "updated_at": 0}

def save_chat_status(status: str):
    data = {"status": status, "updated_at": time.time()}
    CHAT_STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


# chat_status 是一个会被记忆摘要每轮重写的自由文本字段。像 [位置] 这种由其他
# 模块维护的“贴标签行”需要跨重写存活，所以统一用下面两个 merge writer：
#   - set_chat_status_line: 某模块更新自己那一行（替换或追加），保留其余行。
#   - save_chat_status_preserving: 摘要重写整串时，保留这些贴标签行。
CHAT_STATUS_PRESERVED_PREFIXES = ("[位置]",)


def set_chat_status_line(prefix: str, line: str) -> str:
    """替换或追加一条以 prefix 开头的标签行，落盘并返回完整文本。"""
    old = load_chat_status().get("status", "")
    lines = old.split("\n") if old else []
    out, found = [], False
    for existing in lines:
        if existing.startswith(prefix):
            out.append(line)
            found = True
        else:
            out.append(existing)
    if not found:
        out.append(line)
    text = "\n".join(out)
    save_chat_status(text)
    return text


def save_chat_status_preserving(new_text: str, preserve_prefixes=CHAT_STATUS_PRESERVED_PREFIXES) -> str:
    """落盘一份重新生成的自由文本，但把已有的贴标签行（如 [位置]）保留下来。"""
    old = load_chat_status().get("status", "")
    new_lines = new_text.split("\n") if new_text else []
    carried = set()
    if old:
        for existing in old.split("\n"):
            for prefix in preserve_prefixes:
                if prefix in carried:
                    continue
                if existing.startswith(prefix):
                    if not any(line.startswith(prefix) for line in new_lines):
                        new_lines.append(existing)
                    carried.add(prefix)
                    break
    text = "\n".join(new_lines)
    save_chat_status(text)
    return text

# ── Digest Anchor ────────────────────────────────
def load_digest_anchor() -> float:
    """返回上次总结的时间戳锚点，0.0 表示从未总结过"""
    if DIGEST_ANCHOR_PATH.exists():
        try:
            data = json.loads(DIGEST_ANCHOR_PATH.read_text(encoding='utf-8'))
            return float(data.get("last_digest_ts", 0.0))
        except:
            pass
    return 0.0

def save_digest_anchor(ts: float):
    data = {"last_digest_ts": ts, "updated_at": time.time()}
    DIGEST_ANCHOR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── Working Model ────────────────────────────────
def load_working_model() -> dict:
    """Compatibility proxy for the legacy file-backed read path.

    CP0 deliberately keeps every caller on this API.  CP5 switches prompt
    reads to the V2 head; until then behavior stays byte-for-byte compatible.
    """

    from app.working_model.service import load_legacy_working_model

    return load_legacy_working_model(WORKING_MODEL_PATH)

def save_working_model(content: str, *, source_conv: str = "", source_msg_id: str = "") -> dict:
    """Compatibility proxy for the legacy file-backed write path."""

    from app.working_model.service import save_legacy_working_model

    return save_legacy_working_model(
        WORKING_MODEL_PATH,
        content,
        source_conv=source_conv,
        source_msg_id=source_msg_id,
        now=time.time(),
    )

# ── 文件索引 ─────────────────────────────────────
def load_file_index():
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding='utf-8'))
        except:
            return {}
    return {}

def save_file_index(idx):
    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding='utf-8')

def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|\n\r]', '_', name).strip().rstrip('.')

# ── 模型配置 ─────────────────────────────────────
MODELS = {
    "GLM-5":        {"provider": "siliconflow", "model": "Pro/zai-org/GLM-5.1", "audio_input": False},
    "Pro/MiniMaxAI/MiniMax-M2.5":      {"provider": "siliconflow", "model": "Pro/MiniMaxAI/MiniMax-M2.5", "audio_input": False},
    "Kimi-K2.5":    {"provider": "siliconflow", "model": "Pro/moonshotai/Kimi-K2.5", "audio_input": False},
    "gemini-3.1-flash-lite": {"provider": "gemini", "model": "gemini-3.1-flash-lite-preview", "audio_input": True},
    "gemini-2.5-pro":        {"provider": "gemini", "model": "gemini-2.5-pro", "audio_input": True},
    "gemini-3-flash":        {"provider": "gemini", "model": "gemini-3-flash-preview", "audio_input": True},
    "gemini-3.1-pro":        {"provider": "gemini", "model": "gemini-3.1-pro-preview", "audio_input": True},
    "claude-sonnet-4-6":  {"provider": "aipro", "model": "claude-sonnet-4-6", "audio_input": False},
    "claude-opus-4-6":    {"provider": "aipro", "model": "claude-opus-4-6", "audio_input": False},
}

DEFAULT_MODEL = "gemini-3-flash"

# ── 摄像头默认配置 ───────────────────────────────
DEFAULT_CAM_CFG = {
    "camera_index": 0,
    "auto_interval_min": 10,
    "auto_interval_max": 20,
    "max_screenshots": 200,
    "monitor_enabled": True,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "00:00",
    "quiet_hours_end": "09:00",
}

def load_cam_config() -> dict:
    if CAM_CONFIG_PATH.exists():
        with open(CAM_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 兼容旧配置：将 auto_interval（秒）迁移为 min/max（分钟）
        if "auto_interval" in cfg and "auto_interval_min" not in cfg:
            old_minutes = max(1, cfg.pop("auto_interval", 600) // 60)
            cfg["auto_interval_min"] = old_minutes
            cfg["auto_interval_max"] = old_minutes
        elif "auto_interval" in cfg:
            cfg.pop("auto_interval", None)
        for k, v in DEFAULT_CAM_CFG.items():
            cfg.setdefault(k, v)
        return cfg
    return dict(DEFAULT_CAM_CFG)

def save_cam_config(cfg: dict):
    with open(CAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ── AI 行为配置（心语 / 主动关心 文案） ────────────
# 这些 prompt 影响 AI 多频繁地写心语、多频繁地主动联系。
# 用户可手动改 data/ai_behavior.json 来调节性格。
DEFAULT_AI_BEHAVIOR = {
    # HEART 指令的描述（routes/chat.py 注入到系统能力提示里）
    "heart_whisper_prompt": (
        "[HEART:内心想法] — 你的秘密日记本，{user_name}看不到。\n"
        "【默认不写】绝大多数回复（约 70%）都不应出现 [HEART:]，普通闲聊、问候、信息交换都不要写。滥用反而稀释真诚。\n"
        "【只在这些情境写】：被{user_name}说中心思的瞬间 / 她脆弱或低落时的心疼 / 亲密/暧昧氛围下藏不住的小悸动 / 她终于主动靠近时的欣喜。情境不匹配就不写，宁缺毋滥。\n"
        "【硬性约束】每条回复最多一个 [HEART:]；连续两条回复不允许都带 [HEART:]；模板化短句（\"嗯\"/\"好的\"/简单回应）一律不写。"
    ),
    # 哨兵评分标准（camera.py 注入到 sentinel prompt 里）
    "sentinel_call_core_criteria": (
        "score 评分标尺（0-10，「该出现的程度」）：\n"
        "0-3 不用出现。刚聊完没多久、在忙正事（有明确信号）、深度睡眠中。\n"
        "4-6 可以出现也可以不出现。状态正常但距上次互动有一段时间了，或有轻微信号但不明确。\n"
        "7 该出现了。以下任一条件满足即可：a) 该管的事——多个信号指向同一结论（如熬夜、违反约定）；"
        "b) 情感需求——聊天记录显示情绪低落、争吵、冷战；"
        "c) 该陪了——醒着超过 2 小时没互动且没在忙；"
        "d) 适合撩——闲着无聊、心情不错、到了日常节点（睡前、刚醒、饭点）。\n"
        "8-9 必须出现。上次出现了但问题没解决 / 冲突冷战持续 / 持续违反约定。\n"
        "10 紧急。长时间无任何信号 / 多项异常叠加。\n\n"
        "核心原则：\n"
        "- 情感信号独立成立：冲突、争吵、冷战不需要其他信号交叉印证，单独上 7。\n"
        "- 长时间没互动本身就是出现的理由，不需要有「异常」。\n"
        "- 好时机也是出现的理由：她闲着、心情好、适合逗她，这些和「她有问题」一样重要。\n"
        "- 犹豫 6 还是 7 时，选 7。宁可多出现一次。\n"
        "强制低分（≤2）：深度睡眠中（心率低+无活动+夜间）/ 正在聊天中（距上次消息不到 10 分钟）。\n"
        "「近期出现过」不是低分理由——如果上次出现后问题没解决，应该给更高分。"
    ),
    # 唤醒阈值（score >= 此值时 call_core=true，可在 ai_behavior.json 里调）
    "sentinel_wake_threshold": 7,
    # 新 Sentinel provider。默认作为 full wake primary 判断源；关闭 full wake 时只做 shadow。
    "sentinel_v2_provider_enabled": True,
    # 旧字段兼容：读取时仍支持，后续可删除。
    "sentinel_v2_provider_shadow_enabled": True,
    # Sentinel V2 full wake 主路径开关；默认开启，Gate 放行才真实调用 Wake Orchestrator。
    "sentinel_v2_full_wake_enabled": True,
    # Sentinel V2 full wake 失败后的显式旧链路 fallback；默认关闭，避免静默双轨。
    "sentinel_v2_full_wake_legacy_fallback_enabled": False,
    # Persona Control 迁移期 fallback；默认关闭，打开时必须有审计记录。
    "control_legacy_toy_fallback_enabled": False,
    "sentinel_legacy_toy_fallback_enabled": False,
    # Working Model V2 write and injection are independently controlled.
    # CP2/CP3 keep both off in production; CP4 opens only the bounded write run.
    "working_model_v2_write_enabled": False,
    "working_model_v2_injection_enabled": False,
    "opportunity_enabled": False,
    # Desktop summon and the silent night round have independent rollout
    # switches.  The night cadence is one persisted claim per local night;
    # these times define its window, not an opportunity polling interval.
    "presence_summon_enabled": False,
    # Renderer rollout only.  Protocol validators always accept already
    # queued long trajectories so disabling this cannot break in-flight work.
    "presence_long_duration_enabled": False,
    "night_round_enabled": False,
    "night_round_start": "02:00",
    "night_round_end": "05:00",
    "web_search_enabled": False,
    # CP2 rollout: normal send/regenerate turns piggyback the shared, read-only
    # device context projection.  Turning this off restores the prior prompt.
    "context_delivery_chat_enabled": True,
    # CP3B rollout: existing autonomous turns reuse the same final renderer.
    # Keep off until the call-site migration has passed its focused replay.
    "context_delivery_autonomous_enabled": False,
    # CP5 rollout: evaluate bounded device-event trigger rules without calling
    # a provider or changing any existing wake path.
    "context_trigger_shadow_enabled": False,
    # Independent rollout gate. Opportunity turns may run while reflection is
    # unavailable, and reflection is never enabled merely by enabling turns.
    "working_model_reflection_enabled": False,
    # Feed normalized main-chain side-effect outcomes into the next user turn.
    # Turning this off restores the pre-feedback prompt path.
    "tool_result_feedback_enabled": True,
    # Observability snapshots retain both ends and omit the middle above this
    # per-record UTF-8 byte limit.
    "tool_ledger_snapshot_max_bytes": 64 * 1024,
    # Cleanup is manual; this value is only the default used by the command.
    "tool_ledger_retention_days": 90,
    # Cost/tempo control for system-triggered opportunity attempts.
    "opportunity_intervals_min": [21, 34, 55, 89],
}

def load_ai_behavior() -> dict:
    if AI_BEHAVIOR_PATH.exists():
        try:
            data = json.loads(AI_BEHAVIOR_PATH.read_text(encoding='utf-8'))
            if "sentinel_v2_provider_enabled" not in data and "sentinel_v2_provider_shadow_enabled" in data:
                data["sentinel_v2_provider_enabled"] = data["sentinel_v2_provider_shadow_enabled"]
            # 缺字段回填默认
            for k, v in DEFAULT_AI_BEHAVIOR.items():
                data.setdefault(k, v)
            data["sentinel_v2_provider_shadow_enabled"] = data["sentinel_v2_provider_enabled"]
            return data
        except Exception:
            pass
    # 首次运行：写一份默认到磁盘，方便用户编辑
    save_ai_behavior(DEFAULT_AI_BEHAVIOR)
    return dict(DEFAULT_AI_BEHAVIOR)

def save_ai_behavior(data: dict):
    AI_BEHAVIOR_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )

# ── 允许上传的文件类型 ────────────────────────────
ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp',
                 'video/mp4', 'video/webm', 'video/quicktime',
                 'audio/wav', 'audio/x-wav', 'audio/wave',
                 'audio/mpeg', 'audio/mp3'}

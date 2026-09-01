"""
网易云音乐集成：pyncm 搜索 + 获取歌曲信息
支持 MUSIC_U Cookie 登录（VIP 可播放付费歌曲），未配置时退回匿名登录
会话每 2 小时自动刷新，获取音频失败时自动重试一次

pyncm 是可选运行依赖：缺失时只禁用音乐 API，不阻塞整个后端启动。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

_init_lock = threading.Lock()
_inited = False
_last_login_time = 0.0
_SESSION_TTL = 2 * 3600  # 会话有效期：2小时

_pyncm_import_error: Exception | None = None
LoginViaAnonymousAccount: Callable[..., Any] | None = None
LoginViaCookie: Callable[..., Any] | None = None
GetSearchResult: Callable[..., Any] | None = None
GetTrackDetail: Callable[..., Any] | None = None
GetTrackAudio: Callable[..., Any] | None = None


class MusicUnavailableError(RuntimeError):
    """Raised when the optional music backend is unavailable."""


def _load_pyncm():
    """Import pyncm only when a music feature is used."""
    global _pyncm_import_error
    global LoginViaAnonymousAccount, LoginViaCookie
    global GetSearchResult, GetTrackDetail, GetTrackAudio

    if GetSearchResult is not None and GetTrackDetail is not None and GetTrackAudio is not None:
        return

    try:
        from pyncm.apis.login import LoginViaAnonymousAccount as _LoginViaAnonymousAccount
        from pyncm.apis.login import LoginViaCookie as _LoginViaCookie
        from pyncm.apis.cloudsearch import GetSearchResult as _GetSearchResult
        from pyncm.apis.track import GetTrackDetail as _GetTrackDetail
        from pyncm.apis.track import GetTrackAudio as _GetTrackAudio
    except Exception as exc:
        _pyncm_import_error = exc
        raise MusicUnavailableError(
            "音乐功能依赖 pyncm 不可用；请按 requirements.txt 安装 pyncm wheel 后重试"
        ) from exc

    LoginViaAnonymousAccount = _LoginViaAnonymousAccount
    LoginViaCookie = _LoginViaCookie
    GetSearchResult = _GetSearchResult
    GetTrackDetail = _GetTrackDetail
    GetTrackAudio = _GetTrackAudio
    _pyncm_import_error = None


def get_music_dependency_status() -> dict[str, Any]:
    """Return whether the optional music dependency can be imported."""
    try:
        _load_pyncm()
    except MusicUnavailableError as exc:
        return {
            "available": False,
            "error": str(exc),
            "cause": repr(_pyncm_import_error),
        }
    return {"available": True, "error": "", "cause": ""}


def _ensure_login():
    """确保已登录且会话未过期（优先 MUSIC_U Cookie，否则匿名）"""
    global _inited, _last_login_time

    _load_pyncm()
    assert LoginViaAnonymousAccount is not None
    assert LoginViaCookie is not None

    now = time.time()
    if _inited and (now - _last_login_time < _SESSION_TTL):
        return
    with _init_lock:
        now = time.time()
        if _inited and (now - _last_login_time < _SESSION_TTL):
            return
        try:
            from config import SETTINGS, _env
            music_u = (_env("AION_NETEASE_MUSIC_U")
                       or SETTINGS.get("netease_music_u", "").strip())
            if music_u:
                LoginViaCookie(MUSIC_U=music_u)
                _inited = True
                _last_login_time = now
                log.info("pyncm MUSIC_U Cookie 登录成功（VIP）")
            else:
                LoginViaAnonymousAccount()
                _inited = True
                _last_login_time = now
                log.info("pyncm 匿名登录成功（未配置 MUSIC_U）")
        except Exception as e:
            log.error("pyncm 登录失败: %s", e)
            raise


def _force_relogin():
    """强制重新登录（会话可能已失效）"""
    global _inited, _last_login_time
    with _init_lock:
        _inited = False
        _last_login_time = 0
    _ensure_login()


def reload_login():
    """重新登录（settings 更新 MUSIC_U 后调用）"""
    _force_relogin()


def search_songs(keyword: str, limit: int = 5) -> list[dict]:
    """搜索歌曲，返回精简结果列表"""
    _ensure_login()
    assert GetSearchResult is not None
    resp = GetSearchResult(keyword, limit=limit)
    songs = resp.get("result", {}).get("songs", [])
    results = []
    for s in songs:
        artists = [a["name"] for a in s.get("ar", [])]
        album_info = s.get("al", {})
        results.append({
            "id": s["id"],
            "name": s["name"],
            "artists": artists,
            "artist": " / ".join(artists),
            "album": album_info.get("name", ""),
            "cover": (album_info.get("picUrl") or "") + "?param=200y200",
            "duration": s.get("dt", 0),  # 毫秒
        })
    return results


def get_song_detail(song_id: int) -> dict | None:
    """获取单曲详情"""
    _ensure_login()
    assert GetTrackDetail is not None
    resp = GetTrackDetail([song_id])
    songs = resp.get("songs", [])
    if not songs:
        return None
    s = songs[0]
    artists = [a["name"] for a in s.get("ar", [])]
    album_info = s.get("al", {})
    return {
        "id": s["id"],
        "name": s["name"],
        "artists": artists,
        "artist": " / ".join(artists),
        "album": album_info.get("name", ""),
        "cover": (album_info.get("picUrl") or "") + "?param=200y200",
        "duration": s.get("dt", 0),
    }


def get_audio_url(song_id: int) -> str | None:
    """尝试获取播放 URL，失败时自动重新登录重试一次"""
    _ensure_login()
    assert GetTrackAudio is not None
    resp = GetTrackAudio([song_id])
    for d in resp.get("data", []):
        url = d.get("url")
        if url:
            return url
    # 可能会话过期，强制重新登录后重试
    log.info("get_audio_url(%s) 返回空，尝试重新登录重试", song_id)
    _force_relogin()
    resp = GetTrackAudio([song_id])
    for d in resp.get("data", []):
        url = d.get("url")
        if url:
            return url
    return None

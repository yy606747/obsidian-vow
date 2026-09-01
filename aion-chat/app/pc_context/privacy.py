"""Pure PC title sanitization rules."""

from __future__ import annotations

import os.path
import re


REDACTED_TITLE = "[redacted]"
TITLE_MAX_CHARS = 40

LOCK_SCREEN_PROCESSES = frozenset({
    "lockapp.exe",
    "logonui.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
})
BLACKLISTED_PROCESSES = frozenset({
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe",
    "lastpass.exe", "wechat.exe", "weixin.exe", "qq.exe", "tim.exe",
    "discord.exe", "telegram.exe", "slack.exe", "outlook.exe",
    "hxoutlook.exe", "mail.exe", "alipay.exe",
})
BROWSER_PROCESSES = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "360se.exe", "360chrome.exe", "qqbrowser.exe",
})
PATH_SENSITIVE_PROCESSES = frozenset({
    "code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe",
    "pycharm64.exe", "idea64.exe", "webstorm64.exe", "clion64.exe",
    "devenv.exe", "windowsterminal.exe", "cmd.exe", "powershell.exe",
    "pwsh.exe",
})
TERMINAL_PROCESSES = frozenset({
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
})

SENSITIVE_KEYWORDS = (
    "incognito", "private browsing", "account settings", "password",
    "token", "api key", "secret", "login", "bank", "payment",
    "checkout", "account", "验证码", "密码", "支付", "银行卡",
)

_URL_PATTERNS = (
    re.compile(r"https?://\S+", re.IGNORECASE),
    re.compile(r"www\.\S+", re.IGNORECASE),
    re.compile(r"file://\S+", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/\S*)?"),
)
_TITLE_SEP_RE = re.compile(r"\s+(?:-|\||\u2014)\s+")


def sanitize_title(raw_process: str | None, raw_title: str | None) -> str:
    process = _process_key(raw_process)
    title = _normalize_spaces(raw_title)
    if not title:
        return ""
    if process in BLACKLISTED_PROCESSES:
        return REDACTED_TITLE
    if process in BROWSER_PROCESSES:
        title = strip_url_like(title)
    elif process in PATH_SENSITIVE_PROCESSES:
        if process in TERMINAL_PROCESSES and str(raw_title or "").strip().startswith("-"):
            return REDACTED_TITLE
        title = strip_path_prefix(strip_url_like(title))
        if process in TERMINAL_PROCESSES and _terminal_title_is_sensitive(title):
            return REDACTED_TITLE
    if matches_sensitive_keyword(title):
        return REDACTED_TITLE
    return truncate_title(title)


def is_blacklisted_process(process: str | None) -> bool:
    return _process_key(process) in BLACKLISTED_PROCESSES


def is_browser_process(process: str | None) -> bool:
    return _process_key(process) in BROWSER_PROCESSES


def is_path_sensitive_process(process: str | None) -> bool:
    return _process_key(process) in PATH_SENSITIVE_PROCESSES


def is_lock_screen_process(process: str | None) -> bool:
    key = _process_key(process)
    text = str(process or "").strip().lower()
    return key in LOCK_SCREEN_PROCESSES or text in {"lockapp", "logonui"}


def strip_url_like(text: str | None) -> str:
    result = str(text or "")
    for pattern in _URL_PATTERNS:
        result = pattern.sub("", result)
    return _normalize_spaces(result)


def strip_path_prefix(text: str | None) -> str:
    cleaned = _normalize_spaces(text)
    if not cleaned:
        return ""
    parts = [part.strip() for part in _TITLE_SEP_RE.split(cleaned) if part.strip()]
    for part in parts:
        if "\\" not in part and "/" not in part:
            return part
    candidate = parts[0] if parts else cleaned
    path_bits = [bit for bit in re.split(r"[\\/]+", candidate) if bit]
    return path_bits[-1].strip() if path_bits else candidate.strip()


def matches_sensitive_keyword(text: str | None) -> bool:
    value = str(text or "")
    lower_value = value.lower()
    for keyword in SENSITIVE_KEYWORDS:
        if _has_cjk(keyword):
            if keyword in value:
                return True
            continue
        words = keyword.split()
        if len(words) == 1:
            pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])"
        else:
            joined = r"[\s_-]+".join(re.escape(word) for word in words)
            pattern = rf"(?<![A-Za-z0-9]){joined}(?![A-Za-z0-9])"
        if re.search(pattern, lower_value, re.IGNORECASE):
            return True
    return False


def truncate_title(text: str | None, max_chars: int = TITLE_MAX_CHARS) -> str:
    value = _normalize_spaces(text)
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip()


def _terminal_title_is_sensitive(title: str) -> bool:
    value = title.strip()
    lowered = value.lower()
    return lowered.startswith("-") or "ssh " in lowered or "@" in value or "=" in value


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _process_key(value: str | None) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return os.path.basename(text).strip().lower()


def _normalize_spaces(value: str | None) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([|/-])\s+", r" \1 ", text)
    return text.strip(" -|\t\r\n")


__all__ = [
    "BLACKLISTED_PROCESSES",
    "BROWSER_PROCESSES",
    "LOCK_SCREEN_PROCESSES",
    "PATH_SENSITIVE_PROCESSES",
    "REDACTED_TITLE",
    "SENSITIVE_KEYWORDS",
    "TITLE_MAX_CHARS",
    "is_blacklisted_process",
    "is_browser_process",
    "is_lock_screen_process",
    "is_path_sensitive_process",
    "matches_sensitive_keyword",
    "sanitize_title",
    "strip_path_prefix",
    "strip_url_like",
    "truncate_title",
]

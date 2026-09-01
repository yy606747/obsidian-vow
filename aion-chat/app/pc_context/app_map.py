"""Pure process to PC app/tag mapping."""

from __future__ import annotations

import os.path


PROCESS_TO_APP: dict[str, str] = {
    "code.exe": "VS Code",
    "code - insiders.exe": "VS Code Insiders",
    "cursor.exe": "Cursor",
    "windsurf.exe": "Windsurf",
    "pycharm64.exe": "PyCharm",
    "idea64.exe": "IntelliJ IDEA",
    "webstorm64.exe": "WebStorm",
    "clion64.exe": "CLion",
    "devenv.exe": "Visual Studio",
    "windowsterminal.exe": "Terminal",
    "cmd.exe": "Command Prompt",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "opera.exe": "Opera",
    "vivaldi.exe": "Vivaldi",
    "360se.exe": "360 Browser",
    "360chrome.exe": "360 Chrome",
    "qqbrowser.exe": "QQ Browser",
    "spotify.exe": "Spotify",
    "cloudmusic.exe": "NetEase Cloud Music",
    "qqmusic.exe": "QQ Music",
    "potplayermini64.exe": "PotPlayer",
    "vlc.exe": "VLC",
    "mpv.exe": "mpv",
    "lockapp.exe": "LockApp",
    "logonui.exe": "LogonUI",
    "shellexperiencehost.exe": "Shell Experience Host",
    "startmenuexperiencehost.exe": "Start Menu",
}

PROCESS_TO_FEATURE_TAG: dict[str, str] = {
    "code.exe": "pc_foreground_dev_tool",
    "code - insiders.exe": "pc_foreground_dev_tool",
    "cursor.exe": "pc_foreground_dev_tool",
    "windsurf.exe": "pc_foreground_dev_tool",
    "pycharm64.exe": "pc_foreground_dev_tool",
    "idea64.exe": "pc_foreground_dev_tool",
    "webstorm64.exe": "pc_foreground_dev_tool",
    "clion64.exe": "pc_foreground_dev_tool",
    "devenv.exe": "pc_foreground_dev_tool",
    "windowsterminal.exe": "pc_foreground_dev_tool",
    "cmd.exe": "pc_foreground_dev_tool",
    "powershell.exe": "pc_foreground_dev_tool",
    "pwsh.exe": "pc_foreground_dev_tool",
    "chrome.exe": "pc_foreground_browser",
    "msedge.exe": "pc_foreground_browser",
    "firefox.exe": "pc_foreground_browser",
    "brave.exe": "pc_foreground_browser",
    "opera.exe": "pc_foreground_browser",
    "vivaldi.exe": "pc_foreground_browser",
    "360se.exe": "pc_foreground_browser",
    "360chrome.exe": "pc_foreground_browser",
    "qqbrowser.exe": "pc_foreground_browser",
    "spotify.exe": "pc_foreground_media",
    "cloudmusic.exe": "pc_foreground_media",
    "qqmusic.exe": "pc_foreground_media",
    "potplayermini64.exe": "pc_foreground_media",
    "vlc.exe": "pc_foreground_media",
    "mpv.exe": "pc_foreground_media",
}

APP_TO_FEATURE_TAG: dict[str, str] = {
    PROCESS_TO_APP[process]: tag for process, tag in PROCESS_TO_FEATURE_TAG.items()
}


def normalize_app_name(raw_process: str | None) -> str:
    process = _process_key(raw_process)
    if process in PROCESS_TO_APP:
        return PROCESS_TO_APP[process]
    basename = _basename(raw_process)
    if not basename:
        return "Unknown"
    if basename.lower().endswith(".exe"):
        return basename[:-4] or "Unknown"
    return basename


def get_feature_tag(raw_process_or_app: str | None) -> str | None:
    process = _process_key(raw_process_or_app)
    if process in PROCESS_TO_FEATURE_TAG:
        return PROCESS_TO_FEATURE_TAG[process]
    return APP_TO_FEATURE_TAG.get(str(raw_process_or_app or "").strip())


def _process_key(value: str | None) -> str:
    return _basename(value).lower()


def _basename(value: str | None) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return os.path.basename(text).strip()


__all__ = [
    "APP_TO_FEATURE_TAG",
    "PROCESS_TO_APP",
    "PROCESS_TO_FEATURE_TAG",
    "get_feature_tag",
    "normalize_app_name",
]

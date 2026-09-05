"""Small helpers for paired private markers."""

from __future__ import annotations

import re


def _suffix_prefix_len(text: str, marker: str) -> int:
    lower = text.lower()
    marker = marker.lower()
    for size in range(min(len(lower), len(marker) - 1), 0, -1):
        if marker.startswith(lower[-size:]):
            return size
    return 0


def strip_paired_private_marker(text: str, open_marker: str, close_marker: str) -> str:
    """Strip complete/unclosed blocks, orphan closes and a trailing marker prefix."""

    raw = str(text or "")
    pair = re.compile(
        re.escape(open_marker) + r"[\s\S]*?" + re.escape(close_marker),
        re.IGNORECASE,
    )
    cleaned = pair.sub("", raw)
    unclosed = re.compile(re.escape(open_marker) + r"[\s\S]*$", re.IGNORECASE)
    cleaned = unclosed.sub("", cleaned)
    cleaned = re.sub(re.escape(close_marker), "", cleaned, flags=re.IGNORECASE)
    keep = max(
        _suffix_prefix_len(cleaned, open_marker),
        _suffix_prefix_len(cleaned, close_marker),
    )
    if keep:
        cleaned = cleaned[:-keep]
    return cleaned.strip()


class PairedPrivateMarkerStreamFilter:
    """Hide a paired marker even when either delimiter crosses chunks."""

    def __init__(self, open_marker: str, close_marker: str):
        self.open_marker = open_marker
        self.close_marker = close_marker
        self._pending = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        text = self._pending + str(chunk or "")
        self._pending = ""
        visible: list[str] = []
        while text:
            lower = text.lower()
            if self._inside:
                end = lower.find(self.close_marker.lower())
                if end < 0:
                    keep = _suffix_prefix_len(text, self.close_marker)
                    if keep:
                        self._pending = text[-keep:]
                    return "".join(visible)
                text = text[end + len(self.close_marker):]
                self._inside = False
                continue

            start = lower.find(self.open_marker.lower())
            orphan_close = lower.find(self.close_marker.lower())
            if orphan_close >= 0 and (start < 0 or orphan_close < start):
                visible.append(text[:orphan_close])
                text = text[orphan_close + len(self.close_marker):]
                continue
            if start >= 0:
                visible.append(text[:start])
                text = text[start + len(self.open_marker):]
                self._inside = True
                continue

            keep = max(
                _suffix_prefix_len(text, self.open_marker),
                _suffix_prefix_len(text, self.close_marker),
            )
            if keep:
                visible.append(text[:-keep])
                self._pending = text[-keep:]
            else:
                visible.append(text)
            break
        return "".join(visible)

    def flush(self) -> str:
        self._inside = False
        self._pending = ""
        return ""


__all__ = ["PairedPrivateMarkerStreamFilter", "strip_paired_private_marker"]

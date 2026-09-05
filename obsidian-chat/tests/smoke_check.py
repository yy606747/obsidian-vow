#!/usr/bin/env python3
"""Minimal backend smoke check for Obsidian Vow.

This script intentionally uses only the Python standard library so it can run
before project dependencies are fully trusted.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Check:
    name: str
    path: str
    expected_statuses: tuple[int, ...] = (200,)
    expect_content_type: str | None = None


CHECKS: tuple[Check, ...] = (
    Check("home page", "/", expect_content_type="text/html"),
    Check("chat page", "/chat", expect_content_type="text/html"),
    Check("settings page", "/settings", expect_content_type="text/html"),
    Check("worldbook page", "/worldbook", expect_content_type="text/html"),
    Check("memory page", "/memory", expect_content_type="text/html"),
    Check("schedule page", "/schedule", expect_content_type="text/html"),
    Check("location page", "/location", expect_content_type="text/html"),
    Check("manifest", "/manifest.json", expect_content_type="application/manifest+json"),
    Check("models api", "/api/models", expect_content_type="application/json"),
    Check("settings api", "/api/settings", expect_content_type="application/json"),
    Check("conversations api", "/api/conversations", expect_content_type="application/json"),
    Check("location status api", "/api/location/status", expect_content_type="application/json"),
    Check("music optional dependency status", "/api/music/status", expect_content_type="application/json"),
)


def request(base_url: str, path: str, token: str, timeout: float):
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    headers = {"User-Agent": "obsidian-vow-smoke/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096)
            return resp.status, resp.headers.get("content-type", ""), body, None
    except HTTPError as exc:
        body = exc.read(4096)
        return exc.code, exc.headers.get("content-type", ""), body, None
    except URLError as exc:
        return 0, "", b"", str(exc)


def content_type_ok(actual: str, expected: str | None) -> bool:
    if not expected:
        return True
    return expected.lower() in actual.lower()


def summarize_body(body: bytes) -> str:
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return text[:180]


def run_checks(checks: Iterable[Check], base_url: str, token: str, timeout: float) -> int:
    failures = 0
    for check in checks:
        status, content_type, body, error = request(base_url, check.path, token, timeout)
        ok = (
            error is None
            and status in check.expected_statuses
            and content_type_ok(content_type, check.expect_content_type)
        )
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {check.name}: {check.path} status={status} content_type={content_type or '-'}")
        if not ok:
            failures += 1
            if error:
                print(f"       error: {error}")
            else:
                print(f"       body: {summarize_body(body)}")
        elif check.expect_content_type == "application/json" and body:
            try:
                json.loads(body.decode("utf-8"))
            except Exception as exc:
                failures += 1
                print(f"       FAIL json parse: {exc}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Run minimal Obsidian Vow backend smoke checks.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--token", default="")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    failures = run_checks(CHECKS, args.base_url, args.token, args.timeout)
    if failures:
        print(f"\n{failures} smoke check(s) failed.")
        return 1
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

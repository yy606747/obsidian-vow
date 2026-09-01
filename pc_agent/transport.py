"""Small authenticated urllib transport shared by pc_agent workers."""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any


_SSL_CONTEXT: ssl.SSLContext | None = None


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    token: str,
    timeout: int = 10,
) -> dict[str, Any] | None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
        if response.status >= 400:
            raise urllib.error.HTTPError(url, response.status, "HTTP error", response.headers, None)
        content = response.read()
    return json.loads(content.decode("utf-8")) if content else None


def get_json(url: str, *, token: str, timeout: int = 40) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
        if response.status == 204:
            return None
        if response.status >= 400:
            raise urllib.error.HTTPError(url, response.status, "HTTP error", response.headers, None)
        content = response.read()
    return json.loads(content.decode("utf-8")) if content else None


def get_bytes(url: str, *, token: str, timeout: int = 40) -> bytes:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
        if response.status >= 400:
            raise urllib.error.HTTPError(url, response.status, "HTTP error", response.headers, None)
        return response.read()


def post_file(url: str, content: bytes, *, filename: str, token: str) -> None:
    boundary = f"----AionScreen{int(time.time() * 1000)}"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="screenshot"; filename="{filename}"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + content + tail
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
        if response.status >= 400:
            raise urllib.error.HTTPError(url, response.status, "HTTP error", response.headers, None)


def ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    context = ssl.create_default_context()
    if os.name == "nt" and hasattr(ssl, "enum_certificates"):
        for store_name in ("ROOT", "CA"):
            try:
                certificates = ssl.enum_certificates(store_name)
            except Exception:
                continue
            for certificate, encoding, _trust in certificates:
                if encoding != "x509_asn":
                    continue
                try:
                    context.load_verify_locations(
                        cadata=ssl.DER_cert_to_PEM_cert(certificate)
                    )
                except Exception:
                    continue
    _SSL_CONTEXT = context
    return context


__all__ = ["get_bytes", "get_json", "post_file", "post_json", "ssl_context"]

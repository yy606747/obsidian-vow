from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import requests

from . import repository
from .keys import VAPID_SUBJECT, ensure_vapid_private_key


log = logging.getLogger("web_push")

PUSH_TIMEOUT_SECONDS = 10.0
PUSH_TTL_SECONDS = 300


@dataclass(frozen=True)
class PushAttempt:
    status_code: int | None
    error_type: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def subscription_gone(self) -> bool:
        return self.status_code in {404, 410}


def _invoke_webpush(**kwargs):
    # Keep import-time startup tolerant while the offline wheelhouse is being
    # staged; deployment installs requirements before the process restarts.
    from pywebpush import webpush

    return webpush(**kwargs)


def _push_proxy_url() -> str:
    value = str(os.environ.get("AION_WEB_PUSH_PROXY") or "").strip()
    if value.lower() in {"", "none", "direct", "off", "false", "0"}:
        return ""
    return value


def _send_one(subscription: dict, payload_json: str, private_key_path: str) -> PushAttempt:
    session = requests.Session()
    session.trust_env = False
    proxy_url = _push_proxy_url()
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    try:
        try:
            response = _invoke_webpush(
                subscription_info={
                    "endpoint": subscription["endpoint"],
                    "keys": {
                        "p256dh": subscription["p256dh"],
                        "auth": subscription["auth"],
                    },
                },
                data=payload_json,
                vapid_private_key=private_key_path,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=PUSH_TTL_SECONDS,
                timeout=PUSH_TIMEOUT_SECONDS,
                requests_session=session,
            )
            return PushAttempt(status_code=getattr(response, "status_code", None))
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            return PushAttempt(
                status_code=status_code,
                error_type=exc.__class__.__name__,
            )
    finally:
        session.close()


async def broadcast_alarm(data: dict) -> dict[str, int]:
    """Push an alarm to every subscription; errors never escape to schedule."""
    summary = {"sent": 0, "deleted": 0, "failed": 0}
    try:
        subscriptions = await repository.list_subscriptions()
        if not subscriptions:
            return summary

        private_key_path = str(await asyncio.to_thread(ensure_vapid_private_key))
        payload_json = json.dumps(
            {
                "type": "schedule_alarm",
                "title": "⏰ 闹铃",
                "body": data.get("content") or "日程提醒",
                "url": "/chat",
                "data": data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        attempts = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _send_one, subscription, payload_json, private_key_path
                )
                for subscription in subscriptions
            ),
            return_exceptions=True,
        )

        for subscription, attempt in zip(subscriptions, attempts):
            endpoint = subscription["endpoint"]
            try:
                if isinstance(attempt, Exception):
                    await repository.mark_failure(endpoint)
                    summary["failed"] += 1
                elif attempt.succeeded:
                    await repository.mark_success(endpoint)
                    summary["sent"] += 1
                elif attempt.subscription_gone:
                    await repository.delete_subscription(endpoint)
                    summary["deleted"] += 1
                else:
                    await repository.mark_failure(endpoint)
                    summary["failed"] += 1
            except Exception:
                summary["failed"] += 1
                log.warning("failed to persist Web Push result", exc_info=True)
        if summary["failed"]:
            log.warning("Web Push alarm delivery completed with failures: %s", summary)
        else:
            log.info("Web Push alarm delivery completed: %s", summary)
    except Exception:
        summary["failed"] += 1
        log.warning("Web Push alarm delivery failed", exc_info=True)
    return summary

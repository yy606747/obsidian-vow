"""Queue one deterministic Desktop Presence trajectory for Windows smoke tests."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.presence.schema import ALLOWED_ANCHORS, ALLOWED_TARGET_SCREENS
from app.presence.service import presence_service
from app.presence.sprites import sprite_library
from database import get_db


async def _latest_conversation_id() -> str:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("no conversation exists; pass --conv-id after creating one")
    return str(row[0])


def build_trajectory(
    *,
    sprite_id: str,
    target_screen: str,
    anchor: str,
    duration_ms: int = 6_000,
) -> dict:
    """Exercise every V1 prop while remaining inside the frozen schema."""

    duration_ms = int(duration_ms)
    if not 6_000 <= duration_ms <= 600_000:
        raise ValueError("duration_ms must be between 6000 and 600000")
    hold_end = duration_ms - 1_200
    return {
        "sprite_id": sprite_id,
        "target_screen": target_screen,
        "anchor": anchor,
        "transform_origin": "bottom_center",
        "duration_ms": duration_ms,
        "tracks": [
            {
                "prop": "x",
                "keys": [[0, 100], [1_400, 0], [hold_end, 0], [duration_ms, -30]],
                "ease": "out_cubic",
            },
            {
                "prop": "y",
                "keys": [[0, 70], [1_400, 0], [hold_end, 0], [duration_ms, 45]],
                "ease": "in_out_cubic",
            },
            {
                "prop": "scale",
                "keys": [
                    [0, 0.72], [1_400, 1.08], [2_000, 1],
                    [hold_end, 1], [duration_ms, 0.88],
                ],
                "ease": "out_cubic",
            },
            {
                "prop": "rotation",
                "keys": [
                    [0, -8], [1_400, 4], [2_000, 0],
                    [hold_end, 0], [duration_ms, -5],
                ],
                "ease": "in_out_quad",
            },
            {
                "prop": "opacity",
                "keys": [[0, 0], [500, 1], [hold_end, 1], [duration_ms, 0]],
                "ease": "linear",
            },
        ],
    }


async def queue_smoke(args: argparse.Namespace) -> dict:
    if not await presence_service.agent_online(device_id=args.device_id):
        raise RuntimeError(
            f"device {args.device_id!r} is offline; start pc_agent and wait for one poll"
        )

    sprites = await sprite_library.available_sprites(device_id=args.device_id)
    if args.sprite_id:
        sprites = [row for row in sprites if row["sprite_id"] == args.sprite_id]
    if not sprites:
        raise RuntimeError(
            "no synced sprite is available; leave pc_agent running until sprite sync completes"
        )

    conv_id = args.conv_id or await _latest_conversation_id()
    sprite_id = str(sprites[0]["sprite_id"])
    event = await presence_service.enqueue_trajectory(
        conv_id=conv_id,
        intent_text="Windows smoke trajectory exercising x/y/scale/rotation/opacity",
        device_id=args.device_id,
        trajectory=build_trajectory(
            sprite_id=sprite_id,
            target_screen=args.target_screen,
            anchor=args.anchor,
            duration_ms=round(float(args.duration_sec) * 1_000),
        ),
    )
    return {
        "event_id": event["event_id"],
        "status": event["status"],
        "conv_id": conv_id,
        "device_id": args.device_id,
        "sprite_id": sprite_id,
        "target_screen": args.target_screen,
        "anchor": args.anchor,
        "duration_ms": round(float(args.duration_sec) * 1_000),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conv-id", help="Outcome inbox conversation; defaults to latest")
    parser.add_argument("--device-id", default="pc")
    parser.add_argument("--sprite-id", help="Use one already-synced sprite")
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=6.0,
        help="Playback duration, 6..600 seconds; use 120 for the long smoke",
    )
    parser.add_argument(
        "--target-screen",
        choices=sorted(ALLOWED_TARGET_SCREENS),
        default="active",
    )
    parser.add_argument(
        "--anchor",
        choices=sorted(ALLOWED_ANCHORS),
        default="bottom_right",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(queue_smoke(args))
    except Exception as exc:
        print(f"presence smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

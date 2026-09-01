import asyncio
import io

import aiosqlite
import pytest
from PIL import Image

from app.presence.schema import (
    MAX_DURATION_MS,
    PRESENCE_RENDERER_RESPONSE_SCHEMA,
    SpriteValidationError,
    TrajectoryValidationError,
    inspect_transparent_png,
    validate_trajectory,
)
from app.presence.db import init_presence_tables


def _trajectory():
    return {
        "sprite_id": "fog_003",
        "target_screen": "active",
        "anchor": "bottom_right",
        "transform_origin": "center",
        "duration_ms": 4200,
        "tracks": [
            {
                "prop": "x",
                "keys": [[0, 120], [800, 0], [4200, 120]],
                "ease": "out_cubic",
            },
            {"prop": "opacity", "keys": [[0, 0], [400, 1], [4200, 0]]},
        ],
    }


def _png(*, alpha: int) -> bytes:
    image = Image.new("RGBA", (16, 12), (120, 80, 200, alpha))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_trajectory_schema_accepts_contract_example_shape():
    normalized = validate_trajectory(_trajectory())
    assert normalized["duration_ms"] == 4200
    assert normalized["tracks"][1]["ease"] == "linear"


def test_trajectory_schema_always_accepts_ten_minutes_and_rejects_more():
    value = _trajectory()
    value["duration_ms"] = MAX_DURATION_MS
    assert validate_trajectory(value)["duration_ms"] == 600_000

    value["duration_ms"] = MAX_DURATION_MS + 1
    with pytest.raises(TrajectoryValidationError, match="duration_ms:out_of_range"):
        validate_trajectory(value)


def test_provider_schema_leaves_unsupported_bounds_to_local_validation():
    schema = PRESENCE_RENDERER_RESPONSE_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "sprite_id",
        "target_screen",
        "anchor",
        "transform_origin",
        "duration_ms",
        "tracks",
    }
    tracks = schema["properties"]["tracks"]
    assert "minItems" not in tracks
    assert "maxItems" not in tracks
    assert "oneOf" not in tracks["items"]
    assert tracks["items"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(target_screen="screen_2"),
        lambda value: value.update(duration_ms=600_001),
        lambda value: value["tracks"].append(
            {"prop": "x", "keys": [[0, 0], [1, 1]]}
        ),
        lambda value: value["tracks"][0].update(keys=[[10, 0], [10, 1]]),
        lambda value: value["tracks"][0].update(keys=[[0, float("nan")]]),
        lambda value: value["tracks"][0].update(prop="width"),
    ],
)
def test_trajectory_schema_fails_closed_without_repairs(mutate):
    value = _trajectory()
    mutate(value)
    with pytest.raises(TrajectoryValidationError):
        validate_trajectory(value)


def test_sprite_inspection_requires_real_nonempty_alpha():
    with pytest.raises(SpriteValidationError, match="alpha_opaque"):
        inspect_transparent_png(_png(alpha=255))
    with pytest.raises(SpriteValidationError, match="alpha_empty"):
        inspect_transparent_png(_png(alpha=0))

    image = Image.new("RGBA", (16, 12), (120, 80, 200, 0))
    for x in range(4, 12):
        for y in range(3, 9):
            image.putpixel((x, y), (120, 80, 200, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    inspected = inspect_transparent_png(output.getvalue())
    assert inspected.sha256.startswith("sha256:")
    assert inspected.transparent_pixels > 0
    assert inspected.visible_pixels > 0


def test_presence_schema_adds_form_to_old_database_idempotently(tmp_path):
    async def scenario():
        path = tmp_path / "old-presence.db"
        async with aiosqlite.connect(path) as db:
            await db.execute(
                """
                CREATE TABLE presence_sprites (
                    sprite_id TEXT PRIMARY KEY,
                    sprite_hash TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL UNIQUE,
                    base_height_dip REAL NOT NULL,
                    description TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    width_px INTEGER NOT NULL,
                    height_px INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            await init_presence_tables(db)
            await init_presence_tables(db)
            cursor = await db.execute("PRAGMA table_info(presence_sprites)")
            columns = {row[1]: row for row in await cursor.fetchall()}
            assert columns["form"][3] == 1
            assert columns["form"][4] == "'unknown'"
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    """
                    INSERT INTO presence_sprites(
                        sprite_id,sprite_hash,file_name,base_height_dip,
                        description,prompt,form,provider,width_px,height_px,
                        active,archived,created_at
                    ) VALUES('bad','sha256:bad','bad.png',260,'','','animal','',1,1,1,0,0)
                    """
                )
            await db.rollback()

    asyncio.run(scenario())

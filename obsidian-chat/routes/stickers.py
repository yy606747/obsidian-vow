"""The owner's sticker picker. Importing does not add to Core's collection."""

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from app.stickers.service import MAX_IMAGE_BYTES, sticker_service


router = APIRouter()


class StickerCollect(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)


class StickerRemove(BaseModel):
    ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1)


@router.get('/api/stickers')
async def list_stickers():
    return {'stickers': await sticker_service.panel_library()}


@router.post('/api/stickers')
async def add_sticker(file: UploadFile = File(...)):
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, '表情文件大小须在 20 MB 以内，且不能为空')
    try:
        item = await sticker_service.add_to_panel(data)
    except (ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, '请添加有效的 PNG、JPEG、WebP 或 GIF 表情图片') from exc
    return {'sticker': item}


@router.post('/api/stickers/collect')
async def collect_sticker(body: StickerCollect):
    try:
        item = await sticker_service.collect_from_message(body.message_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'sticker': item}


@router.post('/api/stickers/remove')
async def remove_stickers(body: StickerRemove):
    return {'removed_ids': await sticker_service.remove_from_panel(body.ids)}

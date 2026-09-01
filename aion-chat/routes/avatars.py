"""
自定义头像：
- POST /api/avatar/{kind}  上传（multipart），resize 成 256×256 png 存 data/avatars/
- GET  /api/avatar/{kind}  有自定义返回自定义，否则 fallback 到 /public/{Default}Icon.png
- DELETE /api/avatar/{kind}  删除自定义，回到默认
- GET  /api/avatar/info    返回两个头像的 mtime，前端拿来做缓存破坏
kind ∈ {"ai", "user"}
"""

import io
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from config import DATA_DIR, PUBLIC_DIR

AVATARS_DIR = DATA_DIR / "avatars"
AVATARS_DIR.mkdir(exist_ok=True)

_KINDS = {"ai", "user"}
_DEFAULTS = {
    "ai": PUBLIC_DIR / "optimized" / "AIIcon.png",
    "user": PUBLIC_DIR / "optimized" / "UserIcon.png",
}
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

router = APIRouter()


def _custom_path(kind: str) -> Path:
    return AVATARS_DIR / f"{kind}.png"


@router.get("/api/avatar/info")
async def avatar_info():
    """返回两个头像的 mtime；前端把它拼到 img src 后面破缓存。"""
    out = {}
    for k in _KINDS:
        p = _custom_path(k)
        if p.exists():
            out[k] = int(p.stat().st_mtime)
            out[f"{k}_custom"] = True
        else:
            # 用默认图的 mtime 保证第一次加载也有稳定版本号
            d = _DEFAULTS[k]
            out[k] = int(d.stat().st_mtime) if d.exists() else 0
            out[f"{k}_custom"] = False
    return out


@router.get("/api/avatar/{kind}")
async def get_avatar(kind: str):
    if kind not in _KINDS:
        raise HTTPException(404)
    p = _custom_path(kind)
    if p.exists():
        return FileResponse(p, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=3600"})
    d = _DEFAULTS[kind]
    if d.exists():
        return FileResponse(d, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=3600"})
    raise HTTPException(404)


@router.post("/api/avatar/{kind}")
async def upload_avatar(kind: str, file: UploadFile = File(...)):
    if kind not in _KINDS:
        raise HTTPException(404, "kind must be ai or user")
    # 读取 + 尺寸校验
    data = await file.read()
    if len(data) > _MAX_BYTES:
        return JSONResponse({"error": "文件过大（>5MB）"}, status_code=400)
    if not data:
        return JSONResponse({"error": "空文件"}, status_code=400)

    # 用 PIL 中心裁切 + 压成 256×256 PNG，顺手剥元数据
    try:
        from PIL import Image
    except ImportError:
        return JSONResponse({"error": "服务器缺少 Pillow 依赖"}, status_code=500)

    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((256, 256), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        _custom_path(kind).write_bytes(out.getvalue())
    except Exception as e:
        return JSONResponse({"error": f"图片处理失败: {e}"}, status_code=400)

    return {"ok": True, "version": int(_custom_path(kind).stat().st_mtime)}


@router.delete("/api/avatar/{kind}")
async def delete_avatar(kind: str):
    if kind not in _KINDS:
        raise HTTPException(404)
    p = _custom_path(kind)
    if p.exists():
        p.unlink()
    return {"ok": True}

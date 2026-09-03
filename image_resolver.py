from __future__ import annotations

import base64
import mimetypes
import os


def resolve(raw: str | None) -> str | None:
    """url → http 直传；file://或绝对路径 → base64；其他 → None"""
    if not raw:
        return None
    u = raw.strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("file://"):
        path = u[7:]
    elif os.path.isabs(u):
        path = u
    else:
        return None
    try:
        return _to_b64(path)
    except Exception:
        return None


def _to_b64(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"

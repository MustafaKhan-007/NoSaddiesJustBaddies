"""Validate owner-uploaded videos and build 16:9 thumbnails.

Bytes are stored in the database (like avatars/assets) so they survive Render
deploys. Videos are served with HTTP range support so the browser can seek.
"""
import io
import os

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_VIDEO_BYTES = 128 * 1024 * 1024   # 128 MB
MAX_THUMB_BYTES = 6 * 1024 * 1024
THUMB_W, THUMB_H = 1280, 720
THUMB_MIME = "image/jpeg"

EXT_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".ogg": "video/ogg", ".ogv": "video/ogg",
}


class VideoError(ValueError):
    pass


def _sniff(ext: str, raw: bytes) -> bool:
    head = raw[:16]
    if ext in (".mp4", ".m4v", ".mov"):
        return raw[4:8] == b"ftyp"
    if ext == ".webm":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    if ext in (".ogg", ".ogv"):
        return head[:4] == b"OggS"
    return False


def process_video(file_storage):
    """Validate an uploaded video. Returns (data, mime, filename)."""
    name = os.path.basename(file_storage.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in EXT_MIME:
        raise VideoError("Please upload an MP4, MOV, WEBM or OGG video.")
    raw = file_storage.read(MAX_VIDEO_BYTES + 1)
    if not raw:
        raise VideoError("That file was empty.")
    if len(raw) > MAX_VIDEO_BYTES:
        raise VideoError("That video is over 128 MB \u2014 please trim or compress it.")
    if not _sniff(ext, raw):
        raise VideoError("That didn't look like a valid video file.")
    return raw, EXT_MIME[ext], name[:255]


def process_thumb(file_storage):
    """Return (jpeg_bytes, mime) for a 16:9 thumbnail, or raise VideoError."""
    raw = file_storage.read(MAX_THUMB_BYTES + 1)
    if not raw:
        raise VideoError("That thumbnail was empty.")
    if len(raw) > MAX_THUMB_BYTES:
        raise VideoError("That thumbnail is over 6 MB \u2014 try a smaller one.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        raise VideoError("That didn't look like an image we could read.")
    img = ImageOps.fit(img, (THUMB_W, THUMB_H), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue(), THUMB_MIME

"""Validate owner-uploaded videos and build 16:9 thumbnails.

Video files are streamed to a directory on disk (a mounted persistent disk in
production) in fixed-size chunks, so even large uploads never load fully into
memory. Only the small thumbnail is kept in the database. Videos are served
with HTTP range support so the browser can seek.
"""
import io
import os
import secrets

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_THUMB_BYTES = 6 * 1024 * 1024
THUMB_W, THUMB_H = 1280, 720
THUMB_MIME = "image/jpeg"
_CHUNK = 1024 * 1024   # 1 MB streaming buffer

EXT_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".ogg": "video/ogg", ".ogv": "video/ogg",
}


class VideoError(ValueError):
    pass


def _sniff(ext: str, head: bytes) -> bool:
    if ext in (".mp4", ".m4v", ".mov"):
        return head[4:8] == b"ftyp"
    if ext == ".webm":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    if ext in (".ogg", ".ogv"):
        return head[:4] == b"OggS"
    return False


def _safe_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _validate_video_upload(file_storage):
    """Return (original_filename, ext, stream, head) or raise VideoError."""
    name = os.path.basename(file_storage.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in EXT_MIME:
        raise VideoError("Please upload an MP4, MOV, WEBM or OGG video.")

    stream = file_storage.stream
    head = stream.read(16)
    if not head:
        raise VideoError("That file was empty.")
    if not _sniff(ext, head):
        raise VideoError("That didn't look like a valid video file.")
    return name[:255], ext, stream, head


def process_video_bytes(file_storage, max_bytes: int):
    """Validate an upload and return ``(mime, filename, size, data)``.

    Used for reel-review raw videos so they survive ephemeral disks (stored in
    the database like course files). Cap ``max_bytes`` appropriately for DB size.
    """
    name, ext, stream, head = _validate_video_upload(file_storage)
    chunks = [head]
    size = len(head)
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise VideoError(
                f"That video is over {max_bytes // (1024 * 1024)} MB \u2014 "
                "please trim or compress it.")
        chunks.append(chunk)
    return EXT_MIME[ext], name, size, b"".join(chunks)


def process_video(file_storage, dest_dir: str, max_bytes: int):
    """Stream an uploaded video to ``dest_dir`` in chunks, enforcing the size
    cap as we go. Returns (disk_name, mime, original_filename, size)."""
    name, ext, stream, head = _validate_video_upload(file_storage)

    os.makedirs(dest_dir, exist_ok=True)
    disk_name = secrets.token_hex(16) + ext
    path = os.path.join(dest_dir, disk_name)
    size = 0
    try:
        with open(path, "wb") as f:
            f.write(head)
            size = len(head)
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise VideoError(
                        f"That video is over {max_bytes // (1024 * 1024)} MB \u2014 "
                        "please trim or compress it.")
                f.write(chunk)
    except VideoError:
        _safe_remove(path)
        raise
    except OSError:
        _safe_remove(path)
        raise VideoError("We couldn't save that upload just now \u2014 please try again.")
    return disk_name, EXT_MIME[ext], name, size


def delete_stored(dest_dir: str, disk_name: str):
    """Remove a stored video file (best effort)."""
    if dest_dir and disk_name:
        _safe_remove(os.path.join(dest_dir, disk_name))


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

"""Download helper for the ComfyUI_DataLoader command node.

A single public entry point, :func:`download_to_path`, streams a URL to an
absolute path inside the container. Destination resolution and the command
parsing live in ``nodes.py``.
"""

import os
import hashlib

import folder_paths

try:  # requests ships with ComfyUI, but degrade gracefully just in case.
    import requests

    _HAS_REQUESTS = True
except Exception:  # pragma: no cover - fallback path
    import urllib.request

    _HAS_REQUESTS = False

CHUNK = 1 << 20  # 1 MiB
USER_AGENT = "ComfyUI_DataLoader/1.0"


def comfy_base_path() -> str:
    """Absolute path of the ComfyUI root, used to resolve relative destinations."""
    base = getattr(folder_paths, "base_path", None)
    if base:
        return base
    return os.path.dirname(folder_paths.models_dir)


def refresh_folder_cache() -> None:
    """Invalidate ComfyUI's filename-list cache so new files are discoverable
    by dropdown loaders in subsequent requests."""
    try:
        cache = getattr(folder_paths, "filename_list_cache", None)
        if isinstance(cache, dict):
            cache.clear()
    except Exception:
        pass


def _verify_sha256(path: str, expected: str) -> None:
    expected = (expected or "").strip().lower()
    if not expected:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        os.remove(path)
        raise ValueError(
            f"sha256 mismatch for {os.path.basename(path)}: "
            f"expected {expected}, got {actual}"
        )


def _report(name, done, total, progress_cb):
    if progress_cb:
        progress_cb(done, total)
    if total:
        print(f"[DataLoader] {name}: {done * 100 // total}% "
              f"({done >> 20}/{total >> 20} MiB)", flush=True)
    else:
        print(f"[DataLoader] {name}: {done >> 20} MiB", flush=True)


def _stream_requests(url, headers, tmp, timeout, progress_cb=None):
    with requests.get(
        url, headers=headers, stream=True, timeout=timeout, allow_redirects=True
    ) as r:
        if r.status_code in (401, 403):
            raise PermissionError(
                f"HTTP {r.status_code} for {url} - authentication required "
                f"(pass a headers dict with an auth token)."
            )
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        name = os.path.basename(tmp)
        done = 0
        next_mark = 0
        step = max(total // 100, CHUNK) if total else 4 * CHUNK
        if progress_cb:
            progress_cb(0, total)
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if done >= next_mark:
                    _report(name, done, total, progress_cb)
                    next_mark = done + step
        _report(name, done, total, progress_cb)


def _stream_urllib(url, headers, tmp, timeout, progress_cb=None):  # pragma: no cover
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        name = os.path.basename(tmp)
        done = 0
        next_mark = 0
        step = max(total // 100, CHUNK) if total else 4 * CHUNK
        if progress_cb:
            progress_cb(0, total)
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done >= next_mark:
                    _report(name, done, total, progress_cb)
                    next_mark = done + step
        _report(name, done, total, progress_cb)


def download_to_path(
    url: str,
    dest_path: str,
    headers: dict = None,
    overwrite: bool = False,
    sha256: str = "",
    timeout: int = 120,
    progress_cb=None,
):
    """Stream ``url`` to ``dest_path`` atomically.

    Returns ``(dest_path, downloaded)`` - ``downloaded`` is ``False`` when an
    existing file was reused. Writes to a ``.part`` temp file and renames on
    success so a partial download never looks like a complete model.

    ``progress_cb(done_bytes, total_bytes)`` is called periodically while
    streaming (``total_bytes`` is ``0`` when the server omits Content-Length).
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("source URL must not be empty")

    if os.path.exists(dest_path) and not overwrite:
        print(f"[DataLoader] Already present, skipping: {dest_path}", flush=True)
        return dest_path, False

    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update({str(k): str(v) for k, v in headers.items()})

    parent = os.path.dirname(dest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest_path + ".part"

    print(f"[DataLoader] Downloading {url} -> {dest_path}", flush=True)
    try:
        if _HAS_REQUESTS:
            _stream_requests(url, req_headers, tmp, timeout, progress_cb)
        else:
            _stream_urllib(url, req_headers, tmp, timeout, progress_cb)
        _verify_sha256(tmp, sha256)
        os.replace(tmp, dest_path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    print(f"[DataLoader] Done: {dest_path}", flush=True)
    return dest_path, True

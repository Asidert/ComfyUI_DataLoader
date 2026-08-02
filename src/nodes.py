"""Command-driven download node for ComfyUI.

A single node, ``DataLoader``, takes a list of download commands and fetches each
file into the container at workflow-execution time - e.g. adding or updating a
LoRA on a running pod without re-provisioning it.

Command format
--------------
``commands`` is a JSON value. Each item is a **pair** ``[destination, source]``
with an optional third element - a **headers dict** (put your auth token there):

    [destination, source]
    [destination, source, {"Authorization": "Bearer <token>"}]

* ``destination`` - where to save inside the container. Absolute paths are used
  as-is; relative paths resolve against the ComfyUI root (e.g.
  ``"models/loras/zit/x.safetensors"``).
* ``source`` - the URL to download from.

You can pass a single file or a whole list:

    # one file
    ["models/loras/x.safetensors", "https://host/x.safetensors"]

    # one file with headers
    ["models/loras/x.safetensors", "https://host/x.safetensors", {"Authorization": "Bearer TOK"}]

    # a list of files
    [
      ["models/loras/x.safetensors", "https://host/x.safetensors"],
      ["models/checkpoints/y.safetensors", "https://host/y.safetensors", {"Authorization": "Bearer TOK"}]
    ]

An object form is also accepted per item for readability::

    {"destination": "...", "source": "...", "headers": {...}, "sha256": "..."}

Dry run
-------
In ``manifest`` mode, ``dry_run`` computes the same diff but downloads nothing
and reports it as a ``dataloader.plan`` websocket event. It lets a caller show
what is stale on a running worker without touching it.
"""

import os
import json
import time

from .download import (
    download_to_path,
    refresh_folder_cache,
    comfy_base_path,
    fetch_json,
    resolve_url,
    local_mtime,
    stamp_mtime,
    _Speed,
)

try:  # optional - only present inside a running ComfyUI
    from server import PromptServer
except Exception:
    PromptServer = None

CATEGORY = "data"


def _send(event, data):
    """Push a UI event to the frontend over ComfyUI's WebSocket (best effort)."""
    if PromptServer is None:
        return
    try:
        PromptServer.instance.send_sync(event, data)
    except Exception:
        pass


def _is_string_list(value):
    """True for a single ``[dest, source, ...]`` item (first two are strings)."""
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], str)
        and isinstance(value[1], str)
    )


def _normalize(commands):
    """Turn the ``commands`` input into a list of item dicts.

    Accepts a JSON string or an already-parsed value. Disambiguates a single
    ``[dest, source, headers?]`` item from a list of such items by checking
    whether the first two elements are strings.
    """
    if isinstance(commands, str):
        commands = commands.strip()
        if not commands:
            return []
        commands = json.loads(commands)

    # Single item as an object, e.g. {"destination": ..., "source": ...}
    if isinstance(commands, dict):
        return [_parse_item(commands)]

    if not isinstance(commands, (list, tuple)):
        raise ValueError("commands must be a JSON array or object")

    # Single item as a pair/triple of scalars: ["dest", "src", {...}?]
    if _is_string_list(commands):
        return [_parse_item(commands)]

    # Otherwise: a list of items.
    return [_parse_item(item) for item in commands]


def _parse_item(item):
    if isinstance(item, dict):
        dest = item.get("destination") or item.get("dest") or item.get("to")
        source = item.get("source") or item.get("url") or item.get("from")
        headers = item.get("headers")
        sha256 = item.get("sha256", "")
    elif isinstance(item, (list, tuple)):
        if len(item) < 2:
            raise ValueError(f"Each command needs at least [destination, source]: {item!r}")
        dest, source = item[0], item[1]
        headers = item[2] if len(item) > 2 else None
        sha256 = item[3] if len(item) > 3 else ""
    else:
        raise ValueError(f"Unsupported command item: {item!r}")

    if not dest or not source:
        raise ValueError(f"Command missing destination or source: {item!r}")
    if headers is not None and not isinstance(headers, dict):
        raise ValueError(f"headers must be an object/dict: {headers!r}")
    return {"destination": str(dest), "source": str(source),
            "headers": headers, "sha256": str(sha256 or "")}


def _resolve_dest(destination: str) -> str:
    destination = destination.replace("\\", "/")
    if os.path.isabs(destination):
        return os.path.normpath(destination)
    return os.path.normpath(os.path.join(comfy_base_path(), *destination.split("/")))


class DataLoader:
    """Download files into the container, either from a manual list of commands
    or by syncing against a manifest endpoint (updating only changed files)."""

    MODES = ["manual", "manifest"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (cls.MODES, {"default": "manual"}),
            },
            "optional": {
                # manual mode - managed by the node's web UI (Add File rows).
                "commands": ("STRING", {"default": "[]"}),
                "overwrite": ("BOOLEAN", {"default": False}),
                # manifest mode.
                "manifest_url": ("STRING", {
                    "default": "https://flammaverse.com/worker_models/image",
                }),
                "token": ("STRING", {"default": ""}),
                "force": ("BOOLEAN", {"default": False}),
                # Посчитать разницу и отчитаться событием dataloader.plan,
                # ничего не скачивая. Нужно бэкенду, чтобы показать в админке,
                # что на воркере устарело, не трогая сам под.
                "dry_run": ("BOOLEAN", {"default": False}),
                # manifest mode: emit a `dataloader.speedcheck` WS event after
                # this many seconds so the caller can decide to wait or bail on a
                # slow worker (0 = off).
                "speed_probe_seconds": ("INT", {"default": 10, "min": 0, "max": 600}),
                # shared.
                "stop_on_error": ("BOOLEAN", {"default": True}),
                "timeout": ("INT", {"default": 120, "min": 1, "max": 3600}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("result",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = CATEGORY

    def run(self, mode="manual", commands="[]", overwrite=False,
            manifest_url="", token="", force=False, speed_probe_seconds=10,
            stop_on_error=True, timeout=120, dry_run=False, unique_id=None):
        node_id = str(unique_id) if unique_id is not None else ""
        if mode == "manifest":
            return self._run_manifest(
                node_id, manifest_url, token, force, stop_on_error, timeout,
                speed_probe_seconds, dry_run)
        return self._run_manual(
            node_id, commands, overwrite, stop_on_error, timeout)

    # ------------------------------------------------------------------ manual
    def _run_manual(self, node_id, commands, overwrite, stop_on_error, timeout):
        items = _normalize(commands)
        resolved = [(_resolve_dest(it["destination"]), it) for it in items]

        _send("dataloader.start", {
            "node": node_id,
            "files": [{"index": i, "name": os.path.basename(dest)}
                      for i, (dest, _) in enumerate(resolved)],
        })

        results = []
        any_downloaded = False

        for idx, (dest, item) in enumerate(resolved):
            entry = {"destination": dest, "source": item["source"]}

            def progress_cb(done, total, speed=0.0, eta=None, _i=idx):
                _send("dataloader.progress", {
                    "node": node_id, "index": _i, "done": done, "total": total,
                    "speed": speed, "eta": eta,
                })

            try:
                _, downloaded = download_to_path(
                    item["source"], dest,
                    headers=item["headers"],
                    overwrite=overwrite,
                    sha256=item["sha256"],
                    timeout=timeout,
                    progress_cb=progress_cb,
                )
                entry["ok"] = True
                entry["downloaded"] = downloaded
                any_downloaded = any_downloaded or downloaded
                _send("dataloader.file", {
                    "node": node_id, "index": idx,
                    "status": "downloaded" if downloaded else "cached",
                })
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[DataLoader] ERROR {item['source']} -> {dest}: {entry['error']}",
                      flush=True)
                _send("dataloader.file", {
                    "node": node_id, "index": idx,
                    "status": "error", "error": entry["error"],
                })
                results.append(entry)
                if stop_on_error:
                    summary = json.dumps(results, ensure_ascii=False)
                    _send("dataloader.done", {"node": node_id})
                    raise RuntimeError(
                        f"DataLoader failed on {item['source']}: {entry['error']}\n{summary}"
                    ) from exc
                continue
            results.append(entry)

        if any_downloaded:
            refresh_folder_cache()

        _send("dataloader.done", {"node": node_id})
        summary = json.dumps(results, ensure_ascii=False)
        print(f"[DataLoader] Summary: {summary}", flush=True)
        return {"ui": {"text": [summary]}, "result": (summary,)}

    # ---------------------------------------------------------------- manifest
    def _run_manifest(self, node_id, manifest_url, token, force, stop_on_error,
                      timeout, speed_probe_seconds=0, dry_run=False):
        headers = {"Authorization": f"Bearer {token}"} if token else None

        data = fetch_json(manifest_url, headers=headers, timeout=timeout)
        files = data.get("files") if isinstance(data, dict) else data
        if not isinstance(files, list):
            raise ValueError("Manifest has no 'files' list")

        base = comfy_base_path()
        entries = []
        for f in files:
            if not isinstance(f, dict):
                continue
            target = f.get("target") or f.get("path")
            src = f.get("url") or f.get("source")
            if not target or not src:
                continue
            rel = str(target).replace("\\", "/").lstrip("/")
            dest = os.path.normpath(os.path.join(base, *rel.split("/")))
            entries.append({
                "target": rel,
                "name": os.path.basename(rel),
                # The manifest url is only a path; join it onto the manifest
                # endpoint's scheme+host so the domain is derived automatically.
                "url": resolve_url(manifest_url, src),
                "updated_at": int(f.get("updated_at") or 0),
                "size": int(f.get("size_bytes") or f.get("size") or 0),
                "dest": dest,
            })

        # Only files that are missing or older than the manifest are (re)fetched.
        to_update, up_to_date = [], []
        for e in entries:
            lm = local_mtime(e["dest"])
            if force or lm is None or (e["updated_at"] and lm < e["updated_at"]):
                to_update.append(e)
            else:
                up_to_date.append(e)

        if dry_run:
            plan = [
                {
                    "target": e["target"],
                    "size": e["size"],
                    "updated_at": e["updated_at"],
                    "reason": "missing" if local_mtime(e["dest"]) is None else "outdated",
                }
                for e in to_update
            ]
            _send("dataloader.plan", {
                "node": node_id,
                "files": plan,
                "total_bytes": sum(e["size"] for e in to_update),
                "up_to_date": len(up_to_date),
                "total": len(entries),
            })
            _send("dataloader.done", {"node": node_id})
            summary = json.dumps(
                {
                    "mode": "manifest",
                    "dry_run": True,
                    "files": plan,
                    "counts": {
                        "total": len(entries),
                        "to_update": len(plan),
                        "up_to_date": len(up_to_date),
                    },
                },
                ensure_ascii=False,
            )
            print(f"[DataLoader] Dry run: {len(plan)} file(s) to update", flush=True)
            return {"ui": {"text": [summary]}, "result": (summary,)}

        files_total = len(to_update)
        total_bytes = sum(e["size"] for e in to_update)

        _send("dataloader.start", {
            "node": node_id,
            "files": [{"index": i, "name": e["name"], "size": e["size"]}
                      for i, e in enumerate(to_update)],
            "overall": {"total_bytes": total_bytes, "files_total": files_total},
        })

        overall = _Speed()
        done_base = 0  # bytes of files already finished
        probe = {"t0": None, "sent": False}

        def _maybe_probe(overall_done):
            # After `speed_probe_seconds` from the first byte, emit an early
            # average-speed reading + projected total time so the caller can
            # decide whether the worker is worth waiting for.
            if not speed_probe_seconds or probe["sent"]:
                return
            now = time.time()
            if probe["t0"] is None:
                probe["t0"] = now
                return
            elapsed = now - probe["t0"]
            if elapsed < speed_probe_seconds:
                return
            speed = overall_done / elapsed if elapsed > 0 else 0.0
            remaining = max(total_bytes - overall_done, 0)
            eta_total = (remaining / speed) if speed > 0 else None
            probe["sent"] = True
            _send("dataloader.speedcheck", {
                "node": node_id,
                "window_seconds": round(elapsed, 1),
                "bytes": overall_done,
                "speed": speed,             # bytes/sec, averaged over the window
                "total_bytes": total_bytes,
                "files_total": files_total,
                "eta_total": eta_total,     # seconds to finish the whole update
            })
            print(f"[DataLoader] speedcheck: {speed / 1048576:.1f} MB/s over "
                  f"{elapsed:.0f}s, est. total "
                  f"{'?' if eta_total is None else int(eta_total)}s", flush=True)

        def _emit_overall(overall_done, files_done):
            osp, oeta = overall.sample(overall_done, total_bytes)
            _send("dataloader.overall", {
                "node": node_id, "done": overall_done, "total": total_bytes,
                "files_done": files_done, "files_total": files_total,
                "speed": osp, "eta": oeta,
            })
            _maybe_probe(overall_done)

        updated, errors = [], []
        for idx, e in enumerate(to_update):
            last = {"done": 0}

            def progress_cb(done, total, speed=0.0, eta=None, _i=idx, _last=last):
                _last["done"] = done
                _send("dataloader.progress", {
                    "node": node_id, "index": _i, "done": done, "total": total,
                    "speed": speed, "eta": eta,
                })
                _emit_overall(done_base + done, _i)

            try:
                download_to_path(
                    e["url"], e["dest"],
                    headers=headers, overwrite=True,
                    timeout=timeout, progress_cb=progress_cb,
                )
                stamp_mtime(e["dest"], e["updated_at"])
                done_base += e["size"] or last["done"]
                _emit_overall(done_base, idx + 1)
                updated.append({"target": e["target"], "updated_at": e["updated_at"]})
                _send("dataloader.file",
                      {"node": node_id, "index": idx, "status": "downloaded"})
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                errors.append({"target": e["target"], "error": msg})
                _send("dataloader.file",
                      {"node": node_id, "index": idx, "status": "error", "error": msg})
                print(f"[DataLoader] SYNC ERROR {e['url']} -> {e['dest']}: {msg}", flush=True)
                if stop_on_error:
                    _send("dataloader.done", {"node": node_id})
                    raise RuntimeError(f"DataLoader sync failed on {e['target']}: {msg}") from exc

        if updated:
            refresh_folder_cache()
        _send("dataloader.done", {"node": node_id})

        summary = {
            "mode": "manifest",
            "updated": updated,
            "up_to_date": [e["target"] for e in up_to_date],
            "errors": errors,
            "counts": {
                "total": len(entries), "updated": len(updated),
                "up_to_date": len(up_to_date), "errors": len(errors),
            },
        }
        text = json.dumps(summary, ensure_ascii=False)
        print(f"[DataLoader] Sync {summary['counts']}", flush=True)
        return {"ui": {"text": [text]}, "result": (text,)}


NODE_CLASS_MAPPINGS = {
    "DataLoader": DataLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DataLoader": "Data Loader (Download / Sync)",
}

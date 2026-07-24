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
"""

import os
import json

from .download import download_to_path, refresh_folder_cache, comfy_base_path

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
    """Download one or many files into the container from a command list."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # Managed by the node's web UI (Add File rows). Hidden in the
                # editor; holds the serialized JSON list of downloads.
                "commands": ("STRING", {"default": "[]"}),
            },
            "optional": {
                "overwrite": ("BOOLEAN", {"default": False}),
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

    def run(self, commands, overwrite=False, stop_on_error=True,
            timeout=120, unique_id=None):
        items = _normalize(commands)
        node_id = str(unique_id) if unique_id is not None else ""
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

            def progress_cb(done, total, _i=idx):
                _send("dataloader.progress",
                      {"node": node_id, "index": _i, "done": done, "total": total})

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


NODE_CLASS_MAPPINGS = {
    "DataLoader": DataLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DataLoader": "Data Loader (Download Files)",
}

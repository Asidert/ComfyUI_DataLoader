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

GC diagnostics
--------------
The package also registers ``GET /flamma/gc_stats`` on the ComfyUI server: a
cheap snapshot of the garbage collector, the timings of recent full collections
and the size of the execution cache's bookkeeping. ``?objects=1`` adds a census
of live objects by type and ``?collect=1`` times one full ``gc.collect()``;
both freeze the process and are refused unless the queue is empty.

Cache hygiene
-------------
At the start of every prompt the package prunes ComfyUI's RAM_PRESSURE result
cache: bookkeeping keys of nodes that never produced a result, entries untouched
for ``FLAMMA_CACHE_TTL_SECONDS`` (default 600) except models (ModelPatcher, and
anything whose ``.patcher`` is one: CLIP, VAE), and prompt history of the same
age. ``FLAMMA_CACHE_PATCH=0`` leaves ComfyUI untouched.
"""

import asyncio
import gc
import os
import json
import sys
import threading
import time
from collections import Counter, deque

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


# ------------------------------------------------------------------ gc stats
# Диагностика сборщика мусора процесса ComfyUI (19.10.2026). На видео-поде после
# каждой задачи весь HTTP ComfyUI замирал на gc.collect() из prompt_worker, и
# пауза росла с числом обработанных промптов: в процессе копились живые объекты.
# Шелла на поде нет, а эта нода ставится на каждый под с main при старте, —
# поэтому смотреть внутрь процесса удобнее всего отсюда.
#
# Лёгкая часть — O(1): счётчики gc, длительности полных сборок (ловим через
# gc.callbacks с момента загрузки ноды), размеры кэша исполнения. Тяжёлая часть
# (перепись объектов по типам, замер полного gc.collect()) держит GIL секундами,
# поэтому только по явному параметру, только на пустой очереди и не чаще раза в
# GC_HEAVY_COOLDOWN_SECONDS. Снаружи порт пода и так закрыт ключом гейтвея.

GC_HEAVY_COOLDOWN_SECONDS = 30
GC_TOP_DEFAULT = 40
GC_TOP_MAX = 200

_LOADED_AT = time.time()
_gc_full_collections = deque(maxlen=50)
# поколение -> [число сборок, суммарные секунды] с момента загрузки ноды
_gc_totals = {0: [0, 0.0], 1: [0, 0.0], 2: [0, 0.0]}
_gc_started_at = [None]
_heavy_lock = threading.Lock()
_heavy_last_at = [0.0]


def _flamma_gc_callback(phase, info):
    # Сборки не пересекаются (GIL), поэтому одного слота под старт хватает.
    if phase == "start":
        _gc_started_at[0] = time.perf_counter()
        return
    started = _gc_started_at[0]
    if started is None:
        return
    _gc_started_at[0] = None
    seconds = time.perf_counter() - started
    generation = info.get("generation", 2)
    totals = _gc_totals.setdefault(generation, [0, 0.0])
    totals[0] += 1
    totals[1] += seconds
    if generation == 2:
        _gc_full_collections.append({
            "at": round(time.time(), 3),
            "seconds": round(seconds, 4),
            "collected": info.get("collected", 0),
            "uncollectable": info.get("uncollectable", 0),
        })


# Повторный импорт модуля не должен навешивать второй колбэк.
gc.callbacks[:] = [cb for cb in gc.callbacks
                   if getattr(cb, "__name__", "") != _flamma_gc_callback.__name__]
gc.callbacks.append(_flamma_gc_callback)


def _queue_state():
    """(выполняется, ждёт) по очереди ComfyUI; (None, None), если не достать."""
    try:
        running, queued = PromptServer.instance.prompt_queue.get_current_queue_volatile()
        return len(running), len(queued)
    except Exception:
        return None, None


def _find_prompt_executor():
    """PromptExecutor из локальных переменных prompt_worker (main.py ComfyUI).

    Исполнитель — локальная переменная `e` функции prompt_worker, которая
    крутится в своём потоке всю жизнь процесса; снаружи ссылок на него нет.
    Кадр потока достаём через sys._current_frames() — это O(число потоков) и
    ничего в ComfyUI не патчит.
    """
    for frame in sys._current_frames().values():
        while frame is not None:
            if frame.f_code.co_name == "prompt_worker":
                executor = frame.f_locals.get("e")
                if executor is not None and hasattr(executor, "caches"):
                    return executor
            frame = frame.f_back
    return None


def _exec_cache_state():
    """Размеры кэша результатов нод ComfyUI.

    В режиме RAM_PRESSURE (дефолт ComfyUI 0.29) кэш пополняет used_generation и
    timestamps на каждую ноду каждого промпта, а чистит их только вместе с
    записью кэша. Ключ без записи (`keys_without_entry`) — нода, которая в
    промпте была, но не выполнилась (её выход никуда не подключён, или промпт
    упал/прерван раньше), и такой ключ остаётся в процессе навсегда.
    """
    executor = _find_prompt_executor()
    cache = getattr(getattr(executor, "caches", None), "outputs", None)
    if cache is None:
        return None
    entries = getattr(cache, "cache", None)
    if not isinstance(entries, dict):
        return {"type": type(cache).__name__}
    used = getattr(cache, "used_generation", None)
    # list() — атомарная копия под GIL: словари меняет поток prompt_worker.
    used_keys = list(used) if isinstance(used, dict) else []
    return {
        "type": type(cache).__name__,
        "generation": getattr(cache, "generation", None),
        "entries": len(entries),
        "used_generation": len(used_keys),
        "timestamps": len(getattr(cache, "timestamps", None) or ()),
        "keys_without_entry": sum(1 for key in used_keys if key not in entries),
    }


def _gc_light_stats():
    full = list(_gc_full_collections)
    recent = [item["seconds"] for item in full[-20:]]
    stats = {
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "uptime_seconds": round(time.time() - _LOADED_AT, 1),
        "gc": {
            "enabled": gc.isenabled(),
            "threshold": gc.get_threshold(),
            "count": gc.get_count(),
            "stats": gc.get_stats(),
            "totals": {
                str(generation): {"collections": count, "seconds": round(seconds, 3)}
                for generation, (count, seconds) in sorted(_gc_totals.items())
            },
            "full_recent_max_seconds": max(recent) if recent else None,
            "full_recent_avg_seconds": round(sum(recent) / len(recent), 4) if recent else None,
            "full_collections": full,
        },
        "exec_cache": _exec_cache_state(),
        "cache_hygiene": dict(_cache_hygiene),
    }
    running, queued = _queue_state()
    stats["queue"] = {"running": running, "pending": queued}
    try:
        import psutil
        stats["rss_bytes"] = psutil.Process().memory_info().rss
    except Exception:
        stats["rss_bytes"] = None
    return stats


def _object_census(top):
    """Перепись отслеживаемых gc объектов: всего и топ типов по числу."""
    started = time.perf_counter()
    objects = gc.get_objects()
    try:
        total = len(objects)
        by_type = Counter(map(type, objects))
    finally:
        del objects
    rows = []
    for kind, count in by_type.most_common(top):
        module = getattr(kind, "__module__", "") or ""
        name = getattr(kind, "__qualname__", None) or getattr(kind, "__name__", repr(kind))
        rows.append({"type": name if module == "builtins" else f"{module}.{name}", "count": count})
    return {
        "tracked": total,
        "types": len(by_type),
        "top": rows,
        "census_seconds": round(time.perf_counter() - started, 3),
    }


def _timed_full_collect():
    started = time.perf_counter()
    collected = gc.collect()
    return {"seconds": round(time.perf_counter() - started, 4), "collected": collected}


def _heavy_refusal():
    """Причина отказа тяжёлому запросу или None; при None занимает слот."""
    running, queued = _queue_state()
    if running is None:
        return 503, "queue_unavailable"
    if running or queued:
        return 409, "queue_busy"
    now = time.time()
    if not _heavy_lock.acquire(blocking=False):
        return 429, "heavy_in_progress"
    wait = GC_HEAVY_COOLDOWN_SECONDS - (now - _heavy_last_at[0])
    if wait > 0:
        _heavy_lock.release()
        return 429, f"cooldown_{int(wait) + 1}s"
    return None


def _register_gc_routes():
    instance = getattr(PromptServer, "instance", None) if PromptServer is not None else None
    if instance is None:
        return
    from aiohttp import web

    @instance.routes.get("/flamma/gc_stats")
    async def flamma_gc_stats(request):
        query = request.rel_url.query
        want_objects = query.get("objects") == "1"
        want_collect = query.get("collect") == "1"
        try:
            top = min(GC_TOP_MAX, max(1, int(query.get("top", GC_TOP_DEFAULT))))
        except ValueError:
            return web.json_response({"error": "bad_top"}, status=400)

        if not (want_objects or want_collect):
            return web.json_response(_gc_light_stats())

        refusal = _heavy_refusal()
        if refusal is not None:
            status, reason = refusal
            return web.json_response({"error": reason}, status=status)
        try:
            payload = {}
            # Сначала сборка: перепись после неё не считает уже мёртвый мусор.
            if want_collect:
                payload["collect"] = await asyncio.to_thread(_timed_full_collect)
            if want_objects:
                payload["objects"] = await asyncio.to_thread(_object_census, top)
            payload.update(_gc_light_stats())
            return web.json_response(payload)
        finally:
            _heavy_last_at[0] = time.time()
            _heavy_lock.release()


try:
    _register_gc_routes()
except Exception as exc:  # диагностика не должна ронять загрузку ноды
    print(f"[DataLoader] gc_stats route not registered: {type(exc).__name__}: {exc}", flush=True)


# ------------------------------------------------------------- cache hygiene
# Чистка памяти процесса ComfyUI от старых задач (19.10.2026). Кэш результатов в
# режиме RAM_PRESSURE (дефолт ComfyUI 0.29, в апстриме на 19.10.2026 так же):
# 1) пишет ключ каждого узла каждого промпта в used_generation/timestamps, а
#    чистит их только вместе с записью кэша — ключ узла, который не выполнился
#    (выход никуда не подключён, промпт упал или прерван), остаётся навсегда;
# 2) неактивные записи вытесняет, только когда свободной RAM меньше
#    min(128 ГБ, вся RAM): на машине с 236 ГБ результаты всех задач лежат, пока
#    не съедят ~100 ГБ.
# И то и другое — живые объекты (ключ узла — ~100–230 frozenset/tuple), которые
# gc.collect() после каждой задачи обходит под GIL: на видео-поде с 26 тысячами
# промптов это была пауза ~4 с. Поэтому в начале каждого промпта (в clean_unused
# кэша, поток prompt_worker — гонок с исполнением нет):
# - удаляем ключи без записи, кроме ключей текущего промпта;
# - выгружаем записи, к которым не обращались дольше FLAMMA_CACHE_TTL_SECONDS,
#   кроме моделей: ModelPatcher (чекпойнт, UNet, лора) и всё, у чего .patcher —
#   ModelPatcher (CLIP, VAE). Модели и лоры остаются загруженными; вытеснение
#   ComfyUI по нехватке RAM работает как раньше;
# - удаляем историю промптов старше того же срока.
# FLAMMA_CACHE_TTL_SECONDS=0 — только ключи без записи; FLAMMA_CACHE_PATCH=0 —
# не трогать ComfyUI вовсе. Счётчики — в /flamma/gc_stats → cache_hygiene.

CACHE_TTL_DEFAULT_SECONDS = 600

_cache_hygiene = {
    "installed": False,
    "ttl_seconds": None,
    "runs": 0,
    "orphan_keys_dropped": 0,
    "entries_dropped": 0,
    "history_dropped": 0,
    "last_run_at": None,
    "last_run_seconds": None,
    "error": None,
}
# Классы «модели»; заполняется при установке патча (в тестах — подменяется).
_MODEL_TYPES = ()


def _cache_ttl_seconds():
    raw = os.environ.get("FLAMMA_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return CACHE_TTL_DEFAULT_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        print(f"[DataLoader] FLAMMA_CACHE_TTL_SECONDS={raw!r} is not a number, "
              f"using {CACHE_TTL_DEFAULT_SECONDS}", flush=True)
        return CACHE_TTL_DEFAULT_SECONDS


def _is_model_object(value):
    if isinstance(value, _MODEL_TYPES):
        return True
    try:
        return isinstance(getattr(value, "patcher", None), _MODEL_TYPES)
    except Exception:
        return False


def _holds_model(outputs):
    """Есть ли среди выходов узла модель (выходы — вложенные списки/кортежи)."""
    stack = [outputs]
    while stack:
        value = stack.pop()
        if isinstance(value, (list, tuple)):
            stack.extend(value)
        elif _is_model_object(value):
            return True
    return False


def prune_exec_cache(cache, now, ttl_seconds):
    """Почистить кэш результатов; вернуть (ключей без записи, записей) удалено."""
    live = set(cache.cache_key_set.get_used_keys())
    entries = cache.cache
    side = [d for d in (getattr(cache, "used_generation", None),
                        getattr(cache, "timestamps", None),
                        getattr(cache, "children", None)) if isinstance(d, dict)]
    timestamps = getattr(cache, "timestamps", None) or {}

    orphans = set()
    for bookkeeping in side:
        orphans.update(key for key in bookkeeping if key not in entries and key not in live)
    for bookkeeping in side:
        for key in orphans:
            bookkeeping.pop(key, None)

    dropped = 0
    if ttl_seconds > 0:
        for key, entry in list(entries.items()):
            if key in live:
                continue
            touched = timestamps.get(key)
            if touched is None or now - touched <= ttl_seconds:
                continue
            if _holds_model(getattr(entry, "outputs", None)):
                continue
            del entries[key]
            for bookkeeping in side:
                bookkeeping.pop(key, None)
            dropped += 1
    return len(orphans), dropped


def _history_created_at(item):
    """Время постановки промпта (сек) по записи истории ComfyUI; None — не понять."""
    try:
        created = item["prompt"][3].get("create_time")
        if created:
            return float(created) / 1000.0
    except Exception:
        pass
    try:
        stamps = [data.get("timestamp") for _, data in item["status"]["messages"]]
        stamps = [stamp for stamp in stamps if stamp]
        return max(stamps) / 1000.0 if stamps else None
    except Exception:
        return None


def prune_history(queue, now, ttl_seconds):
    """Удалить из истории промпты старше ttl_seconds; вернуть, сколько удалено."""
    if ttl_seconds <= 0:
        return 0
    with queue.mutex:
        stale = [prompt_id for prompt_id, item in queue.history.items()
                 if (created := _history_created_at(item)) is not None
                 and now - created > ttl_seconds]
        for prompt_id in stale:
            queue.history.pop(prompt_id, None)
    return len(stale)


def _run_cache_hygiene(cache):
    started = time.perf_counter()
    now = time.time()
    ttl = _cache_hygiene["ttl_seconds"] or 0
    orphans, entries = prune_exec_cache(cache, now, ttl)
    history = 0
    queue = getattr(getattr(PromptServer, "instance", None), "prompt_queue", None) if PromptServer else None
    if queue is not None:
        history = prune_history(queue, now, ttl)
    _cache_hygiene["runs"] += 1
    _cache_hygiene["orphan_keys_dropped"] += orphans
    _cache_hygiene["entries_dropped"] += entries
    _cache_hygiene["history_dropped"] += history
    _cache_hygiene["last_run_at"] = round(now, 3)
    _cache_hygiene["last_run_seconds"] = round(time.perf_counter() - started, 4)


def _install_cache_hygiene():
    global _MODEL_TYPES
    if os.environ.get("FLAMMA_CACHE_PATCH", "1").strip() == "0":
        _cache_hygiene["error"] = "disabled_by_env"
        return
    try:
        from comfy_execution import caching
        from comfy.model_patcher import ModelPatcher
    except Exception as exc:
        _cache_hygiene["error"] = f"comfy_unavailable: {type(exc).__name__}"
        return
    cache_cls = getattr(caching, "RAMPressureCache", None)
    original = getattr(cache_cls, "clean_unused", None)
    if original is None:
        # Другая версия ComfyUI — патч рассчитан на устройство кэша 0.29.
        _cache_hygiene["error"] = "ram_pressure_cache_not_found"
        print("[DataLoader] cache hygiene skipped: RAMPressureCache.clean_unused not found", flush=True)
        return
    if getattr(original, "_flamma_hygiene", False):
        return
    _MODEL_TYPES = (ModelPatcher,)

    def clean_unused(self):
        original(self)
        try:
            _run_cache_hygiene(self)
        except Exception as exc:
            # Чистка — не повод ронять промпт: пишем и работаем дальше как ComfyUI.
            if _cache_hygiene["error"] is None:
                print(f"[DataLoader] cache hygiene failed: {type(exc).__name__}: {exc}", flush=True)
            _cache_hygiene["error"] = f"{type(exc).__name__}: {exc}"

    clean_unused._flamma_hygiene = True
    cache_cls.clean_unused = clean_unused
    _cache_hygiene["installed"] = True
    _cache_hygiene["ttl_seconds"] = _cache_ttl_seconds()
    print(f"[DataLoader] cache hygiene installed, ttl={_cache_hygiene['ttl_seconds']}s", flush=True)


try:
    _install_cache_hygiene()
except Exception as exc:  # патч не должен ронять загрузку ноды
    _cache_hygiene["error"] = f"{type(exc).__name__}: {exc}"
    print(f"[DataLoader] cache hygiene not installed: {type(exc).__name__}: {exc}", flush=True)


NODE_CLASS_MAPPINGS = {
    "DataLoader": DataLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DataLoader": "Data Loader (Download / Sync)",
}

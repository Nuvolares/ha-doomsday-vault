"""Doomsday Vault web server (served to Home Assistant through Ingress)."""
import asyncio
import collections
import gzip
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import time
import webbrowser
import zlib
from pathlib import Path

import aiohttp
from aiohttp import web

import exporter as exporter_mod
import settings
from armory import KINDS as ARMORY_KINDS, Armory
from docindex import DocIndex
from inventory import DEFAULT_CATEGORIES, Inventory
from kiwix import LIBRARY_PREFIX, Kiwix
from manager import Manager
from sources import SourceStore
from state import State

log = logging.getLogger("doomsday")
mimetypes.add_type("text/javascript", ".mjs")  # PDF.js ships ES modules
LOG_BUFFER: collections.deque = collections.deque(maxlen=400)

BUSY = ("queued", "checking", "downloading", "verifying", "indexing")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host"}


class BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append({"t": record.created, "level": record.levelname, "msg": self.format(record)})


def setup_logging(level: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in (logging.StreamHandler(), BufferHandler()):
        handler.setFormatter(fmt if isinstance(handler, logging.StreamHandler) else logging.Formatter("%(message)s"))
        root.addHandler(handler)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


BACKGROUND: set = set()


def background(coro) -> None:
    """Run a coroutine without waiting for it (and keep a reference so it is not garbage collected)."""
    task = asyncio.create_task(coro)
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)


# --- Ingress ------------------------------------------------------------------

async def detect_ingress_root(session: aiohttp.ClientSession) -> str:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return LIBRARY_PREFIX
    try:
        async with session.get("http://supervisor/addons/self/info",
                               headers={"Authorization": f"Bearer {token}"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = (await resp.json())["data"]
        return data["ingress_url"].rstrip("/") + LIBRARY_PREFIX
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as err:
        log.warning("Could not read ingress URL from Supervisor (%s); will learn it from the first request", err)
        return LIBRARY_PREFIX


ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1"}  # Supervisor ingress proxy + local health checks


@web.middleware
async def ingress_middleware(request: web.Request, handler):
    # Home Assistant handles authentication; only its ingress proxy may talk to us.
    if not os.environ.get("DV_DEV") and request.remote not in ALLOWED_CLIENTS:
        raise web.HTTPForbidden(text="Doomsday Vault is only reachable through Home Assistant.")
    if settings.PORTABLE and request.method != "GET" and request.path.startswith("/api/"):
        return json_error(403, "This is a read-only offline copy. Make changes in Home Assistant.")
    # kiwix-serve writes absolute links, so it must know the public ingress path.
    ingress_path = request.headers.get("X-Ingress-Path")
    if ingress_path:
        await request.app["kiwix"].set_root(ingress_path.rstrip("/") + LIBRARY_PREFIX)
    return await handler(request)


# --- Views ----------------------------------------------------------------------

def source_view(app: web.Application, src: dict) -> dict:
    manager: Manager = app["manager"]
    kiwix: Kiwix = app["kiwix"]
    st = app["state"].source(src["id"])
    view = dict(src)
    view.update({
        "status": st.get("status", "empty"),
        "error": st.get("error"),
        "version": st.get("version"),
        "remote_version": st.get("remote_version"),
        "last_checked": st.get("last_checked"),
        "updated_at": st.get("updated_at"),
        "installed_bytes": manager.installed_bytes(src["id"]),
        "active": manager.current == src["id"],
        "progress": manager.progress.get(src["id"]),
        "held": bool(st.get("held")),
        "open_url": None,
        "files": [],
    })
    for path in st.get("files", []):
        if path.endswith(".zim"):
            book = kiwix.books.get(path)
            if book and src["enabled"]:
                view["open_url"] = kiwix.content_url(book)
                view["articles"] = book["articles"]
        else:
            name = Path(path).name
            view["files"].append({"name": name, "url": f"files/{src['id']}/{name}"})
            view["open_url"] = view["open_url"] or f"files/{src['id']}/{name}"
    return view


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(settings.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_overview(request: web.Request) -> web.Response:
    app = request.app
    manager: Manager = app["manager"]
    storage = manager.storage
    disk = shutil.disk_usage(storage)
    items = [source_view(app, s) for s in app["sources"].list()]
    return web.json_response({
        "sources": items,
        "initiated": bool(app["state"].data.get("initiated")),
        "current": manager.current,
        "pending": sorted(manager.pending),
        "kiwix_running": app["kiwix"].running,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free,
                 "library": sum(i["installed_bytes"] for i in items), "reserve": int(app["options"]["min_free_gb"]) * 1024 ** 3},
        "options": {k: app["options"][k] for k in ("storage_path", "update_schedule", "update_hour",
                                                     "max_download_mbit", "min_free_gb")},
        "portable": settings.PORTABLE,
        "export": app["manifest"] if settings.PORTABLE else app["exporter"].status,
        "now": time.time(),
    })


# --- Export to drive ----------------------------------------------------------------

async def api_export_drives(request: web.Request) -> web.Response:
    manager: Manager = request.app["manager"]
    drives = await asyncio.to_thread(exporter_mod.list_drives, manager.storage)
    return web.json_response({"drives": drives})


async def api_export_plan(request: web.Request) -> web.Response:
    try:
        plan = await asyncio.to_thread(request.app["exporter"].plan, request.query.get("path", ""))
    except ValueError as err:
        return json_error(400, str(err))
    return web.json_response(plan)


async def api_export_start(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        request.app["exporter"].start(str(body.get("path", "")), list(body.get("sources") or []))
    except (ValueError, RuntimeError) as err:
        return json_error(400, str(err))
    return web.json_response({"ok": True})


async def api_export_cancel(request: web.Request) -> web.Response:
    request.app["exporter"].cancel()
    return web.json_response({"ok": True})


async def api_search(request: web.Request) -> web.Response:
    app = request.app
    query = request.query.get("q", "").strip()[:200]
    if len(query) < 2:
        return web.json_response({"query": query, "groups": []})
    per_book = min(int(request.query.get("per", 6)), 25)
    only = request.query.get("source")

    kiwix: Kiwix = app["kiwix"]
    src_by_path, books = {}, []
    for src in app["sources"].list():
        if not src["enabled"] or (only and src["id"] != only):
            continue
        for path in app["state"].source(src["id"]).get("files", []):
            if path in kiwix.books:
                src_by_path[path] = src
                books.append(kiwix.books[path])

    kiwix_task = kiwix.search(app["http_local"], query, books, per_book)
    doc_task = asyncio.to_thread(app["docs"].search, query, 40)
    kiwix_hits, doc_hits = await asyncio.gather(kiwix_task, doc_task)

    groups: dict[str, dict] = {}

    def group(src):
        return groups.setdefault(src["id"], {"source_id": src["id"], "source": src["name"],
                                             "category": src["category"], "results": []})

    for path, hits in kiwix_hits.items():
        if hits:
            group(src_by_path[path])["results"].extend(hits)
    for hit in doc_hits:
        src = app["sources"].get(hit["source_id"])
        if src and src["enabled"] and (not only or src["id"] == only):
            name = Path(hit["path"]).name
            is_pdf = name.lower().endswith(".pdf")
            group(src)["results"].append({"title": f"{hit['title']} - {name}" + (f" · page {hit['page']}" if is_pdf else ""),
                                          "link": f"files/{src['id']}/{name}",
                                          "page": hit["page"] if is_pdf else None,
                                          "snippet": hit["snippet"]})
    order = {s["id"]: i for i, s in enumerate(app["sources"].list())}
    result = sorted(groups.values(), key=lambda g: order.get(g["source_id"], 0))
    return web.json_response({"query": query, "groups": result})


async def api_save_source(request: web.Request) -> web.Response:
    app = request.app
    manager: Manager = app["manager"]
    body = await request.json()
    existing_id = request.match_info.get("sid")
    before = app["sources"].get(existing_id) if existing_id else None
    if existing_id and not before:
        return json_error(404, "Unknown source")
    try:
        src = app["sources"].put(body, existing_id)
    except ValueError as err:
        return json_error(400, str(err))

    target_changed = before is None or any(before.get(k) != src.get(k) for k in ("type", "folder", "book", "url"))
    if before and before["enabled"] != src["enabled"]:
        await manager.refresh_library()
    if src["enabled"] and app["state"].data.get("initiated"):
        installed = bool(app["state"].source(src["id"]).get("files"))
        if target_changed or not installed:
            manager.enqueue(src["id"])
    app["state"].save()
    return web.json_response(source_view(app, src))


async def api_delete_source(request: web.Request) -> web.Response:
    app = request.app
    sid = request.match_info["sid"]
    if not app["sources"].get(sid):
        return json_error(404, "Unknown source")
    try:
        await app["manager"].purge(sid)
    except RuntimeError as err:
        return json_error(409, str(err))
    app["sources"].delete(sid)
    app["state"].drop(sid)
    app["state"].save()
    return web.json_response({"ok": True})


async def api_purge_source(request: web.Request) -> web.Response:
    sid = request.match_info["sid"]
    if not request.app["sources"].get(sid):
        return json_error(404, "Unknown source")
    try:
        await request.app["manager"].purge(sid)
    except RuntimeError as err:
        return json_error(409, str(err))
    return web.json_response({"ok": True})


async def api_check_source(request: web.Request) -> web.Response:
    sid = request.match_info["sid"]
    if not request.app["sources"].get(sid):
        return json_error(404, "Unknown source")
    queued = request.app["manager"].enqueue(sid)
    request.app["state"].save()
    return web.json_response({"queued": queued})


async def api_cancel_source(request: web.Request) -> web.Response:
    sid = request.match_info["sid"]
    if not request.app["sources"].get(sid):
        return json_error(404, "Unknown source")
    return web.json_response({"cancelled": request.app["manager"].cancel(sid)})


async def api_cancel_all(request: web.Request) -> web.Response:
    return web.json_response({"cancelled": request.app["manager"].cancel_all()})


async def api_initiate(request: web.Request) -> web.Response:
    count = request.app["manager"].initiate()
    return web.json_response({"queued": count})


async def api_update_all(request: web.Request) -> web.Response:
    request.app["state"].data["initiated"] = True
    count = request.app["manager"].enqueue_due(force=True)
    return web.json_response({"queued": count})


async def api_log(request: web.Request) -> web.Response:
    return web.json_response(list(LOG_BUFFER)[-200:])


async def serve_file(request: web.Request) -> web.StreamResponse:
    files_dir = request.app["manager"].files_dir.resolve()
    target = (files_dir / request.match_info["sid"] / request.match_info["name"]).resolve()
    if files_dir not in target.parents or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target)


def serve_from(directory: Path, name: str) -> web.FileResponse:
    target = (directory / name).resolve()
    if target.parent != directory.resolve() or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target, headers={"Cache-Control": "private, max-age=86400"})


# --- Inventory ------------------------------------------------------------------------

async def api_inventory(request: web.Request) -> web.Response:
    items = await asyncio.to_thread(request.app["inventory"].list)
    used = sorted({i["category"] for i in items} - set(DEFAULT_CATEGORIES))
    return web.json_response({"items": items, "categories": DEFAULT_CATEGORIES + used, "today": time.strftime("%Y-%m-%d")})


async def api_inventory_save(request: web.Request) -> web.Response:
    try:
        item = request.app["inventory"].put(await request.json(), request.match_info.get("iid"))
    except ValueError as err:
        return json_error(400, str(err))
    except KeyError:
        return json_error(404, "Unknown item")
    return web.json_response(item)


async def api_inventory_delete(request: web.Request) -> web.Response:
    try:
        request.app["inventory"].delete(request.match_info["iid"])
    except KeyError:
        return json_error(404, "Unknown item")
    return web.json_response({"ok": True})


async def api_inventory_photo_add(request: web.Request) -> web.Response:
    try:
        item = request.app["inventory"].add_photo(request.match_info["iid"], await request.read())
    except ValueError as err:
        return json_error(400, str(err))
    except KeyError:
        return json_error(404, "Unknown item")
    return web.json_response(item)


async def api_inventory_photo_remove(request: web.Request) -> web.Response:
    try:
        item = request.app["inventory"].remove_photo(request.match_info["iid"], request.match_info["name"])
    except KeyError:
        return json_error(404, "Unknown photo")
    return web.json_response(item)


async def inventory_photo(request: web.Request) -> web.FileResponse:
    return serve_from(settings.INVENTORY_PHOTOS, request.match_info["name"])


# --- Armory (WeaponLog) ----------------------------------------------------------------

async def api_armory(request: web.Request) -> web.Response:
    return web.json_response(request.app["armory"].overview())


async def api_armory_config(request: web.Request) -> web.Response:
    body = await request.json()
    armory: Armory = request.app["armory"]
    try:
        armory.configure(body.get("url"), body.get("token"), body.get("interval"))
    except (TypeError, ValueError):
        return json_error(400, "The sync interval must be a number of minutes")
    if armory.configured:
        background(armory.sync())
    return web.json_response(armory.overview())


async def api_armory_sync(request: web.Request) -> web.Response:
    armory: Armory = request.app["armory"]
    if not armory.configured:
        return json_error(400, "Connect WeaponLog first")
    background(armory.sync())
    return web.json_response({"started": True})


async def api_armory_save(request: web.Request) -> web.Response:
    kind = request.match_info["kind"]
    if kind not in ARMORY_KINDS:
        return json_error(404, "Unknown list")
    try:
        item = request.app["armory"].edit(kind, request.match_info.get("key"), await request.json())
    except ValueError as err:
        return json_error(400, str(err))
    except KeyError:
        return json_error(404, "Unknown item")
    background(request.app["armory"].sync())  # send it right away when WeaponLog is reachable
    return web.json_response(item)


async def api_armory_delete(request: web.Request) -> web.Response:
    kind = request.match_info["kind"]
    try:
        request.app["armory"].remove(kind, request.match_info["key"])
    except KeyError:
        return json_error(404, "Unknown item")
    background(request.app["armory"].sync())
    return web.json_response({"ok": True})


async def armory_photo(request: web.Request) -> web.FileResponse:
    return serve_from(settings.ARMORY_PHOTOS, request.match_info["name"])


def restyle_enabled(app: web.Application, stem: str) -> bool:
    """Whether pages of the ZIM with this file stem get the library design."""
    for path in app["kiwix"].books:
        if Path(path).stem != stem:
            continue
        for src in app["sources"].list():
            if path in app["state"].source(src["id"]).get("files", []):
                return src.get("restyle", True)
    return True


def _inflate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:  # some servers send raw deflate without the zlib header
        return zlib.decompress(data, -zlib.MAX_WBITS)


# Content-Encodings we can undo before restyling; anything else is passed through untouched.
DECODERS = {"identity": lambda data: data, "": lambda data: data, "gzip": gzip.decompress, "deflate": _inflate}


def inject_theme(html: str, base: str, restyle: bool = True) -> str:
    # reader.js (in-app navigation, search highlighting) goes into every page; the design only where wanted.
    tags = f'<script src="{base}/static/reader.js"></script>'
    # Converted PDFs place every line at an exact position over the page image; they keep their look.
    if restyle and "pdf2htmlEX" not in html[:20000]:
        tags = f'<link rel="stylesheet" href="{base}/static/reader.css">' + tags
    match = re.search(r"</head\s*>", html, re.I)
    if match:
        return html[:match.start()] + tags + html[match.start():]
    return tags + html


async def kiwix_proxy(request: web.Request) -> web.StreamResponse:
    kiwix: Kiwix = request.app["kiwix"]
    if not kiwix.running:
        return web.Response(status=503, text="The library is empty or still starting.")
    tail = request.match_info["tail"]
    url = f"{kiwix.base}{kiwix.root}/{tail}"
    if request.query_string:
        url += "?" + request.query_string
    parts = tail.split("/")
    book_stem = parts[1] if len(parts) > 2 and parts[0] == "content" else None
    # Ask for uncompressed bodies so HTML pages can be restyled on the way through.
    # It must be set explicitly: aiohttp adds "Accept-Encoding: gzip, deflate" when the header is absent.
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP and k.lower() != "accept-encoding"
               and not k.lower().startswith("x-")}
    headers["Accept-Encoding"] = "identity"
    session: aiohttp.ClientSession = request.app["http_local"]
    try:
        async with session.request(request.method, url, headers=headers, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as upstream:
            if (book_stem and upstream.status == 200
                    and upstream.headers.get("Content-Type", "").startswith("text/html")
                    and upstream.headers.get("Content-Encoding", "identity").lower() in DECODERS):
                body = DECODERS[upstream.headers.get("Content-Encoding", "identity").lower()](await upstream.read())
                html = body.decode(upstream.charset or "utf-8", errors="replace")
                html = inject_theme(html, kiwix.root[:-len(LIBRARY_PREFIX)], restyle_enabled(request.app, book_stem))
                # The page now depends on our design and decoding, not just on the ZIM, so never let
                # browsers keep it: drop kiwix-serve's long-lived cache headers and validators.
                dropped = HOP_BY_HOP | {"content-length", "content-encoding", "content-type",
                                        "cache-control", "expires", "etag", "last-modified"}
                out = {k: v for k, v in upstream.headers.items() if k.lower() not in dropped}
                out["Cache-Control"] = "no-store"
                return web.Response(status=200, text=html, content_type="text/html", headers=out)

            resp = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    resp.headers.add(key, value)
            await resp.prepare(request)
            async for chunk in upstream.content.iter_chunked(65536):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except aiohttp.ClientError as err:
        return web.Response(status=502, text=f"Library server error: {err}")


# --- App lifecycle ---------------------------------------------------------------

def migrate_old_storage(storage: Path) -> None:
    """Doomsday Vault used to be Doomsday Vault: move its downloaded content into place once."""
    old = Path(settings.OLD_STORAGE_PATH)
    if settings.PORTABLE or storage.exists() or not old.is_dir():
        return
    try:
        old.rename(storage)  # same disk: instant, nothing is copied
        log.info("Moved the content of %s to %s", old, storage)
    except OSError as err:
        log.warning("Could not move %s to %s (%s); downloading into the new folder instead", old, storage, err)


async def on_startup(app: web.Application) -> None:
    options = app["options"]
    migrate_old_storage(Path(options["storage_path"]))
    Path(options["storage_path"]).mkdir(parents=True, exist_ok=True)

    app["http"] = aiohttp.ClientSession(headers={"User-Agent": "DoomsdayVault/0.5 (Home Assistant app)"})
    app["http_local"] = aiohttp.ClientSession(auto_decompress=False)

    state = State(settings.STATE_FILE)
    # Anything that was mid-flight when the add-on stopped starts over cleanly.
    for st in state.data["sources"].values():
        if st.get("status") in BUSY:
            st["status"] = "ready" if st.get("files") else "empty"
    if settings.PORTABLE:
        # Exported paths are relative to the library folder; the drive letter differs per PC.
        storage = Path(options["storage_path"])
        for st in state.data["sources"].values():
            st["files"] = [os.path.normpath(storage / f) for f in st.get("files", [])]
        state.readonly = True
    sources = SourceStore(settings.SOURCES_FILE, settings.DEFAULT_SOURCES, state)
    sources.load()

    kiwix = Kiwix(settings.LIBRARY_FILE)
    kiwix.root = await detect_ingress_root(app["http"])
    docs = DocIndex(settings.DOCS_DB)
    if not docs.available:
        log.error("SQLite FTS5 is unavailable: PDFs and other files will download but not be searchable")
    manager = Manager(options, sources, state, kiwix, docs, app["http"])
    inventory = Inventory(settings.INVENTORY_DB, settings.INVENTORY_PHOTOS, readonly=settings.PORTABLE)
    armory = Armory(settings.ARMORY_FILE, settings.ARMORY_PHOTOS, app["http"], readonly=settings.PORTABLE)
    app.update(state=state, sources=sources, kiwix=kiwix, docs=docs, manager=manager,
               inventory=inventory, armory=armory)

    adopted = 0 if settings.PORTABLE else manager.adopt_existing()
    await manager.refresh_library()

    if settings.PORTABLE:
        try:
            app["manifest"] = json.loads((settings.DATA_DIR / "manifest.json").read_text())
        except (OSError, ValueError):
            app["manifest"] = None
        log.info("Offline copy ready: %d archives", len(kiwix.books))
        return  # no downloads, no schedule, no exports from a portable copy

    app["exporter"] = exporter_mod.Exporter(manager)
    if docs.needs_reindex or adopted:
        log.info("Rebuilding the document index page by page")
        app["reindex_task"] = asyncio.create_task(manager.reindex_documents())

    if options["auto_start_downloads"] and not state.data.get("initiated"):
        log.info("auto_start_downloads is on: stocking the vault")
        manager.initiate()
    elif state.data.get("initiated"):
        manager.enqueue_due()  # resume interrupted or never-finished downloads
    else:
        log.info("Vault not stocked yet. Open the side panel and press INITIATE DOWNLOAD.")

    app["tasks"] = [asyncio.create_task(manager.worker()), asyncio.create_task(manager.scheduler()),
                    asyncio.create_task(armory.scheduler())]


async def on_cleanup(app: web.Application) -> None:
    for task in app.get("tasks", []):
        task.cancel()
    await app["kiwix"].stop()
    await app["http"].close()
    await app["http_local"].close()


async def no_cache_static(request: web.Request, response: web.StreamResponse) -> None:
    # Revalidate UI files so an app update never shows a stale page.
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


def create_app() -> web.Application:
    options = settings.load_options()
    setup_logging(options["log_level"])
    # 12 MB: inventory photos are uploaded as the request body.
    app = web.Application(middlewares=[ingress_middleware], client_max_size=12 * 1024 ** 2)
    app["options"] = options
    app.router.add_get("/", index)
    app.router.add_static("/static/", settings.STATIC_DIR)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/log", api_log)
    app.router.add_post("/api/initiate", api_initiate)
    app.router.add_post("/api/update-all", api_update_all)
    app.router.add_post("/api/sources", api_save_source)
    app.router.add_put("/api/sources/{sid}", api_save_source)
    app.router.add_delete("/api/sources/{sid}", api_delete_source)
    app.router.add_post("/api/sources/{sid}/check", api_check_source)
    app.router.add_post("/api/sources/{sid}/purge", api_purge_source)
    app.router.add_post("/api/sources/{sid}/cancel", api_cancel_source)
    app.router.add_post("/api/cancel-all", api_cancel_all)
    app.router.add_get("/api/export/drives", api_export_drives)
    app.router.add_get("/api/export/plan", api_export_plan)
    app.router.add_post("/api/export/start", api_export_start)
    app.router.add_post("/api/export/cancel", api_export_cancel)
    app.router.add_get("/files/{sid}/{name}", serve_file)
    app.router.add_get("/api/inventory", api_inventory)
    app.router.add_post("/api/inventory/items", api_inventory_save)
    app.router.add_put("/api/inventory/items/{iid}", api_inventory_save)
    app.router.add_delete("/api/inventory/items/{iid}", api_inventory_delete)
    app.router.add_post("/api/inventory/items/{iid}/photos", api_inventory_photo_add)
    app.router.add_delete("/api/inventory/items/{iid}/photos/{name}", api_inventory_photo_remove)
    app.router.add_get("/inventory/photos/{name}", inventory_photo)
    app.router.add_get("/api/armory", api_armory)
    app.router.add_put("/api/armory/config", api_armory_config)
    app.router.add_post("/api/armory/sync", api_armory_sync)
    app.router.add_post("/api/armory/{kind}", api_armory_save)
    app.router.add_put("/api/armory/{kind}/{key}", api_armory_save)
    app.router.add_delete("/api/armory/{kind}/{key}", api_armory_delete)
    app.router.add_get("/armory/photos/{name}", armory_photo)
    app.router.add_route("*", LIBRARY_PREFIX + "/{tail:.*}", kiwix_proxy)
    app.on_response_prepare.append(no_cache_static)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def kill_children_on_exit() -> None:
    """Windows: put this process in a job object so kiwix-serve.exe dies when the window is closed."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    try:
        job = kernel32.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            print(f"Note: kiwix-serve may keep running after closing (error {ctypes.get_last_error()})")
        globals()["_job_handle"] = job  # keep the handle open for the life of the process
    except (OSError, ctypes.ArgumentError) as err:
        print(f"Note: kiwix-serve may keep running after closing ({err})")


def main() -> None:
    host = os.environ.get("DV_HOST", "0.0.0.0")
    port = int(os.environ.get("DV_PORT", 8099))
    url = f"http://127.0.0.1:{port}/"
    if settings.PORTABLE:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                print("Doomsday Vault is already running - opening it in your browser.")
                webbrowser.open(url)
                return
        if os.name == "nt":
            kill_children_on_exit()
    app = create_app()
    if os.environ.get("DV_OPEN_BROWSER"):
        async def open_browser(_app):
            asyncio.get_running_loop().call_later(1.0, webbrowser.open, url)
        app.on_startup.append(open_browser)
    print(f"Doomsday Vault is running at {url}")
    web.run_app(app, host=host, port=port, access_log=None, print=None)


if __name__ == "__main__":
    main()

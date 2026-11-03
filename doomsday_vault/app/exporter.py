"""Export the library and supply inventory to an external drive as a portable Windows app.

Drive layout (everything under <drive>/DoomsdayVault/):
    Start Doomsday Vault.bat     double-click to start (Windows)
    START HERE.html              instructions + direct links to documents
    runtime/                     portable Python, kiwix-serve.exe, the app code
    library/                     ZIM archives, files, sources.yaml
    data/                        state.json, documents.db, inventory.db, inventory_photos/,
                                 manifest.json (library.xml is built on start)
The Armory is never exported.

Re-exporting to the same drive only copies files that changed and removes
files that are no longer selected.
"""
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

import settings

log = logging.getLogger(__name__)

EXPORT_DIR = "DoomsdayVault"
OLD_EXPORT_DIR = "DoomsdayLibrary"   # copies made before the rename; renamed on the next export
LAUNCHER = "Start Doomsday Vault.bat"
PORTABLE_RUNTIME = Path(os.environ.get("DV_PORTABLE_RUNTIME", "/opt/portable"))
PORTABLE_PORT = 47800
RESERVE = 512 * 1024 ** 2          # always leave this much free on the drive
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1
WINDOWS_READABLE = {"vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "cifs", "smb3"}
NETWORK = {"cifs", "smb3", "nfs", "nfs4"}
CHUNK = 8 * 1024 * 1024


class ExportCancelled(Exception):
    pass


def mount_types() -> dict[str, str]:
    """Mount point -> file system type, from /proc/mounts."""
    table = {}
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                table[parts[1].replace("\\040", " ")] = parts[2]
    except OSError:
        pass
    return table


def export_root(drive: Path, move: bool = False) -> Path:
    """The copy's folder on the drive. A copy from before the rename is reused (and renamed when move)."""
    root, old = drive / EXPORT_DIR, drive / OLD_EXPORT_DIR
    if not root.exists() and old.is_dir():
        if not move:
            return old
        old.rename(root)
    return root


def read_manifest(drive: Path) -> dict | None:
    try:
        return json.loads((export_root(drive) / "data" / "manifest.json").read_text())
    except (OSError, ValueError):
        return None


def list_drives(exclude: Path) -> list[dict]:
    """Separately mounted disks/shares under /media and /share (not the one holding the library)."""
    types = mount_types()
    try:
        exclude_dev = exclude.stat().st_dev
    except OSError:
        exclude_dev = None
    drives = []
    for root in (Path("/media"), Path("/share")):
        try:
            root_dev = root.stat().st_dev
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            try:
                dev = child.stat().st_dev
                usage = shutil.disk_usage(child)
            except OSError:
                continue
            if dev in (root_dev, exclude_dev):
                continue  # a plain folder, or the disk the library itself lives on
            fstype = types.get(str(child), "unknown")
            drives.append({
                "path": str(child),
                "name": child.name,
                "fstype": fstype,
                "total": usage.total,
                "free": usage.free,
                "writable": os.access(child, os.W_OK),
                "windows_readable": fstype in WINDOWS_READABLE,
                "fat32": fstype == "vfat",
                "network": fstype in NETWORK,
                "export": read_manifest(child),
            })
    return drives


def _app_files() -> list[tuple[Path, str]]:
    files = []
    for path in sorted(settings.APP_DIR.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files.append((path, "runtime/app/" + path.relative_to(settings.APP_DIR).as_posix()))
    return files


def _runtime_files() -> list[tuple[Path, str]]:
    files = []
    if PORTABLE_RUNTIME.is_dir():
        for path in sorted(PORTABLE_RUNTIME.rglob("*")):
            if path.is_file():
                files.append((path, "runtime/" + path.relative_to(PORTABLE_RUNTIME).as_posix()))
    return files + _app_files()


def _same(src: Path, dst: Path) -> bool:
    try:
        a, b = src.stat(), dst.stat()
    except OSError:
        return False
    # FAT/exFAT store times with 2 s resolution.
    return a.st_size == b.st_size and abs(a.st_mtime - b.st_mtime) < 2.5


class Exporter:
    def __init__(self, manager):
        self.manager = manager
        self.status: dict = {"state": "idle"}
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- planning -------------------------------------------------------------

    def items(self) -> list[dict]:
        """Every source with downloaded content, and the files it would put on a drive."""
        out = []
        for src in self.manager.sources.list():
            files = []
            for path in self.manager.state.source(src["id"]).get("files", []):
                p = Path(path)
                if not p.exists():
                    continue
                rel = f"library/zim/{p.name}" if p.suffix == ".zim" else f"library/files/{src['id']}/{p.name}"
                files.append((p, rel, p.stat().st_size))
            if files:
                out.append({"id": src["id"], "name": src["name"], "category": src["category"],
                            "enabled": src["enabled"], "files": files})
        return out

    def plan(self, drive_path: str) -> dict:
        drive = self._drive(drive_path)
        root = export_root(Path(drive["path"]))
        items, wanted = [], set()
        for item in self.items():
            size = sum(f[2] for f in item["files"])
            on_drive = sum(f[2] for f in item["files"] if _same(f[0], root / f[1]))
            wanted.update(f[1] for f in item["files"])
            items.append({
                "id": item["id"], "name": item["name"], "category": item["category"],
                "enabled": item["enabled"], "bytes": size, "on_drive": on_drive,
                "too_big_for_fat32": drive["fat32"] and any(f[2] > FAT32_MAX_FILE for f in item["files"]),
            })
        runtime = _runtime_files()
        runtime_new = sum(p.stat().st_size for p, rel in runtime if not _same(p, root / rel))
        # Files left over from older exports (old releases, removed sources) are deleted first.
        obsolete = 0
        library = root / "library"
        if library.is_dir():
            for path in library.rglob("*"):
                if path.is_file() and path.relative_to(root).as_posix() not in wanted:
                    obsolete += path.stat().st_size
        return {"drive": drive, "items": items, "runtime_new": runtime_new,
                "obsolete": obsolete, "reserve": RESERVE,
                "runtime_ready": PORTABLE_RUNTIME.is_dir()}

    def _drive(self, drive_path: str) -> dict:
        for drive in list_drives(self.manager.storage):
            if drive["path"] == drive_path:
                return drive
        raise ValueError("That drive is not connected (any more). Refresh the list.")

    # --- running ----------------------------------------------------------------

    def start(self, drive_path: str, source_ids: list[str]) -> None:
        if self.running:
            raise RuntimeError("An export is already running")
        drive = self._drive(drive_path)
        if not drive["writable"]:
            raise ValueError("This drive is read-only. In Home Assistant, edit the storage and turn off read-only.")
        if not PORTABLE_RUNTIME.is_dir():
            raise RuntimeError("The Windows runtime is missing from this app build")
        chosen = [i for i in self.items() if i["id"] in set(source_ids)]
        if not chosen:
            raise ValueError("Select at least one source")
        if drive["fat32"] and any(f[2] > FAT32_MAX_FILE for i in chosen for f in i["files"]):
            raise ValueError("FAT32 drives cannot hold files over 4 GB. Deselect the large archives or use exFAT/NTFS.")
        self._cancel.clear()
        self.status = {"state": "preparing", "drive": drive["path"], "drive_name": drive["name"],
                       "done": 0, "total": 0, "current": None, "started": time.time(), "error": None}
        self._thread = threading.Thread(target=self._run, args=(Path(drive["path"]), chosen),
                                        name="export", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self, drive: Path, items: list[dict]) -> None:
        try:
            root = export_root(drive, move=True)
            self._export(root, items)
            self.status.update(state="done", finished=time.time(), current=None)
            log.info("Export to %s finished", drive)
        except ExportCancelled:
            self.status.update(state="cancelled", finished=time.time(), current=None)
            log.info("Export to %s cancelled", drive)
        except Exception as err:  # report anything to the UI
            self.status.update(state="error", error=str(err), finished=time.time(), current=None)
            log.exception("Export to %s failed", drive)

    def _export(self, root: Path, items: list[dict]) -> None:
        wanted: dict[str, Path] = {}
        for item in items:
            for src, rel, _size in item["files"]:
                wanted[rel] = src
        for src, rel in _runtime_files():
            wanted[rel] = src

        # 1. Remove what is no longer wanted (old releases, deselected sources) to free space first.
        for sub in ("library", "runtime"):
            base = root / sub
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*"), reverse=True):
                rel = path.relative_to(root).as_posix()
                if path.is_file() and (rel not in wanted or path.name.endswith(".part")):
                    path.unlink()
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()

        # 2. Copy everything that is missing or changed.
        todo = [(src, root / rel) for rel, src in wanted.items() if not _same(src, root / rel)]
        total = sum(src.stat().st_size for src, _ in todo)
        free = shutil.disk_usage(root.parent).free
        if total > free - RESERVE:
            raise IOError(f"Not enough space: {total / 1e9:.1f} GB to copy, {free / 1e9:.1f} GB free")
        self.status.update(state="copying", total=total, done=0)
        for src, dst in todo:
            self._copy(src, dst)

        # 3. Metadata for the portable app.
        self.status.update(current="library index")
        self._write_metadata(root, items)
        if hasattr(os, "sync"):
            os.sync()  # make sure everything is on the drive before the user unplugs it

    def _copy(self, src: Path, dst: Path) -> None:
        self.status["current"] = src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + ".part")
        with open(src, "rb") as fin, open(part, "wb") as fout:
            while block := fin.read(CHUNK):
                if self._cancel.is_set():
                    fout.close()
                    part.unlink(missing_ok=True)
                    raise ExportCancelled()
                fout.write(block)
                self.status["done"] += len(block)
        os.replace(part, dst)
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))

    def _write_metadata(self, root: Path, items: list[dict]) -> None:
        manager = self.manager
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (root / "library").mkdir(parents=True, exist_ok=True)

        exported = []
        for item in items:
            src = dict(manager.sources.get(item["id"]))
            src["enabled"] = True
            exported.append(src)
        (root / "library" / "sources.yaml").write_text(
            "# Sources in this offline copy (written by the Home Assistant app).\n"
            + yaml.safe_dump({"sources": exported}, sort_keys=False, allow_unicode=True))

        # Paths are stored relative to library/, because the drive letter changes between computers.
        state = {"sources": {}, "known_defaults": manager.state.data.get("known_defaults", []),
                 "initiated": False}
        for item in items:
            st = manager.state.source(item["id"])
            state["sources"][item["id"]] = {
                "files": [rel[len("library/"):] for _src, rel, _size in item["files"]],
                "status": "ready", "version": st.get("version"), "updated_at": st.get("updated_at"),
                "last_checked": st.get("last_checked"),
            }
        (data_dir / "state.json").write_text(json.dumps(state, indent=2))

        # Consistent copy of the document index, even while the app is using it.
        target_db = data_dir / "documents.db"
        target_db.unlink(missing_ok=True)
        if settings.DOCS_DB.exists():
            with sqlite3.connect(settings.DOCS_DB) as source, sqlite3.connect(target_db) as dest:
                source.backup(dest)

        # The supply inventory travels with the copy (read-only there). The Armory never leaves
        # the Home Assistant server: it holds serial numbers.
        self.status.update(current="inventory")
        target_inv = data_dir / "inventory.db"
        target_inv.unlink(missing_ok=True)
        if settings.INVENTORY_DB.exists():
            with sqlite3.connect(settings.INVENTORY_DB) as source, sqlite3.connect(target_inv) as dest:
                source.backup(dest)
        photos_dst = data_dir / "inventory_photos"
        photos_dst.mkdir(exist_ok=True)
        wanted = {p.name: p for p in settings.INVENTORY_PHOTOS.glob("*")} if settings.INVENTORY_PHOTOS.is_dir() else {}
        for old in photos_dst.iterdir():
            if old.name not in wanted:
                old.unlink()
        for name, src in wanted.items():
            if not _same(src, photos_dst / name):
                shutil.copy2(src, photos_dst / name)
        for leftover in ("armory.json", "armory_photos"):  # never on a drive, even from older versions
            path = data_dir / leftover
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

        manifest = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "app_version": os.environ.get("BUILD_VERSION", ""),
            "sources": [{"id": i["id"], "name": i["name"], "bytes": sum(f[2] for f in i["files"])} for i in items],
            "bytes": sum(f[2] for i in items for f in i["files"]),
        }
        (data_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        launcher = START_BAT.replace("{port}", str(PORTABLE_PORT))
        (root / "Start Doomsday Library.bat").unlink(missing_ok=True)  # launcher name before the rename
        (root / LAUNCHER).write_bytes(launcher.replace("\n", "\r\n").encode("ascii"))
        (root / "START HERE.html").write_text(start_page(items, manifest), encoding="utf-8")


START_BAT = """@echo off
setlocal
title Doomsday Vault
set "ROOT=%~dp0"
set "DV_PORTABLE=1"
set "DV_STORAGE=%ROOT%library"
set "DV_DATA_DIR=%ROOT%data"
set "DV_CONFIG_DIR=%ROOT%library"
set "DV_HOST=127.0.0.1"
set "DV_PORT={port}"
set "DV_OPEN_BROWSER=1"
set "PATH=%ROOT%runtime\\kiwix;%PATH%"
echo.
echo   DOOMSDAY VAULT
echo   --------------
echo   Starting the offline vault. Your browser opens in a moment.
echo   Keep this window open while you use it; close it to stop.
echo.
"%ROOT%runtime\\python\\python.exe" "%ROOT%runtime\\app\\main.py"
if errorlevel 1 pause
"""


def start_page(items: list[dict], manifest: dict) -> str:
    from html import escape
    try:
        logo = (settings.STATIC_DIR / "logo.svg").read_text()
    except OSError:
        logo = ""
    rows = "".join(
        f"<li>{escape(i['name'])} <small>{sum(f[2] for f in i['files']) / 1e9:.1f} GB</small></li>" for i in items)
    docs = "".join(
        f'<li><a href="{escape(rel)}">{escape(Path(rel).name)}</a></li>'
        for i in items for _src, rel, _size in i["files"] if not rel.endswith(".zim"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Doomsday Vault - Start here</title>
<style>
body {{ margin: 0; background: #050905; color: #b9f2c6; font: 16px/1.6 Consolas, ui-monospace, monospace; }}
main {{ max-width: 760px; margin: 0 auto; padding: 32px 18px 60px; }}
.logo {{ width: 180px; display: block; margin: 0 auto 10px; }}
h1, h2 {{ color: #e1ffe8; font-weight: 400; letter-spacing: .06em; text-transform: uppercase; }}
h1 {{ text-align: center; font-size: 30px; margin: 0 0 24px; }}
.step {{ border: 1px solid #41ff74; padding: 14px 18px; margin: 18px 0; background: #0c160d; }}
code {{ color: #41ff74; font-size: 17px; }}
a {{ color: #41ff74; }}
small {{ color: #6aa776; }}
</style></head>
<body><main>
<div class="logo">{logo}</div>
<h1>Doomsday Vault</h1>
<div class="step">
<h2>Start the vault</h2>
<p>Double-click <code>Start Doomsday Vault.bat</code> in this folder. A small window opens and your
browser shows the library and your supply inventory a moment later. Everything runs from this drive and needs no internet.</p>
<p>Keep the small window open while reading. Close it when you are done, then eject the drive.</p>
<p><small>If Windows shows "Windows protected your PC", choose <b>More info &rarr; Run anyway</b>.
It only listens on this computer (127.0.0.1).</small></p>
</div>
<h2>On this drive</h2>
<p><small>Exported {escape(manifest['exported_at'].replace('T', ' '))} &middot; {manifest['bytes'] / 1e9:.1f} GB</small></p>
<ul>{rows}</ul>
{f'<h2>Documents</h2><p><small>These also open directly, without starting the library:</small></p><ul>{docs}</ul>' if docs else ''}
</main></body></html>
"""

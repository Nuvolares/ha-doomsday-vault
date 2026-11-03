"""Download queue, scheduled delta updates and library bookkeeping."""
import asyncio
import itertools
import logging
import re
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import aiohttp

from docindex import DocIndex
from downloader import Downloader, InsufficientSpace, sha256_file
from kiwix import Kiwix, resolve_latest
from sources import SourceStore, parse_size
from state import State

log = logging.getLogger(__name__)

INTERVAL_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}
SLACK = 3 * 3600  # run a little early so a daily check at 03:00 doesn't drift to the next day


def filename_from_url(url: str) -> str:
    name = unquote(PurePosixPath(urlparse(url).path).name)
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name or "download"


class Manager:
    def __init__(self, options: dict, sources: SourceStore, state: State,
                 kiwix: Kiwix, docs: DocIndex, session: aiohttp.ClientSession):
        self.options = options
        self.sources = sources
        self.state = state
        self.kiwix = kiwix
        self.docs = docs
        self.session = session
        self.downloader = Downloader(session, options)
        self.storage = Path(options["storage_path"])
        self.zim_dir = self.storage / "zim"
        self.files_dir = self.storage / "files"
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.pending: set[str] = set()
        self.current: str | None = None
        self._task: asyncio.Task | None = None
        self.progress: dict[str, dict] = {}
        self._seq = itertools.count()

    # --- bookkeeping ----------------------------------------------------

    def zim_paths(self) -> list[str]:
        paths = []
        for src in self.sources.list():
            if not src["enabled"]:
                continue
            for path in self.state.source(src["id"]).get("files", []):
                if path.endswith(".zim") and Path(path).exists():
                    paths.append(path)
        return paths

    async def refresh_library(self) -> None:
        await self.kiwix.rebuild(self.zim_paths())

    def installed_bytes(self, sid: str) -> int:
        total = 0
        for path in self.state.source(sid).get("files", []):
            try:
                total += Path(path).stat().st_size
            except OSError:
                pass
        return total

    # --- queue ------------------------------------------------------------

    def enqueue(self, sid: str) -> bool:
        src = self.sources.get(sid)
        if not src or sid in self.pending or sid == self.current:
            return False
        # Small archives first, so the vault becomes useful long before Wikipedia finishes.
        size = parse_size(src.get("size_hint")) or 10 * 1024 ** 3
        self.pending.add(sid)
        self.queue.put_nowait((size, next(self._seq), sid))
        self.state.source(sid).update(status="queued", held=False)
        return True

    def cancel(self, sid: str) -> bool:
        """Stop a queued or running download. The source is then left alone by scheduled
        updates until the user asks for it again (Update, Update all or Initiate)."""
        st = self.state.source(sid)
        if sid in self.pending:
            self.pending.discard(sid)  # the worker skips queue entries that are no longer pending
            st.update(status="ready" if st.get("files") else "empty", held=True, last_result="cancelled")
        elif sid == self.current and self._task:
            self._task.cancel()
        else:
            return False
        self.state.save()
        return True

    def cancel_all(self) -> int:
        count = 0
        for sid in list(self.pending) + ([self.current] if self.current else []):
            count += self.cancel(sid)
        return count

    def enqueue_due(self, force: bool = False) -> int:
        now, count = time.time(), 0
        for src in self.sources.list():
            if not src["enabled"]:
                continue
            schedule = src["schedule"] if src["schedule"] != "default" else self.options["update_schedule"]
            last = self.state.source(src["id"]).get("last_checked")
            installed = bool(self.state.source(src["id"]).get("files"))
            if force:
                due = True
            elif self.state.source(src["id"]).get("held"):
                due = False  # cancelled by the user
            elif not installed:
                due = True
            elif schedule == "manual":
                due = False
            else:
                due = last is None or now - last >= INTERVAL_DAYS[schedule] * 86400 - SLACK
            if due and self.enqueue(src["id"]):
                count += 1
        self.state.save()
        return count

    def initiate(self) -> int:
        """First-run 'stock the vault': download everything enabled."""
        self.state.data["initiated"] = True
        return self.enqueue_due(force=True)

    async def worker(self) -> None:
        while True:
            _, _, sid = await self.queue.get()
            if sid not in self.pending:  # cancelled while queued
                continue
            self.pending.discard(sid)
            if not self.sources.get(sid):
                continue
            self.current = sid
            # A separate task, so a cancel stops this download without stopping the worker.
            self._task = asyncio.create_task(self.update_source(sid))
            try:
                await asyncio.wait({self._task})
                if not self._task.cancelled() and self._task.exception():
                    log.error("Unexpected error while updating %s", sid, exc_info=self._task.exception())
            except asyncio.CancelledError:  # app shutting down
                self._task.cancel()
                raise
            finally:
                self.current = None
                self._task = None
                self.progress.pop(sid, None)

    async def scheduler(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.kiwix.ensure_running()
            if not self.state.data.get("initiated"):
                continue
            now = datetime.now()
            today = now.date().isoformat()
            if now.hour == int(self.options["update_hour"]) and self.state.data.get("last_cycle") != today:
                self.state.data["last_cycle"] = today
                queued = self.enqueue_due()
                log.info("Scheduled update check: %d sources queued", queued)

    # --- updating -----------------------------------------------------------

    def _progress_cb(self, sid: str):
        info = {"done": 0, "total": None, "started": time.time(), "bytes_at_start": None}
        self.progress[sid] = info

        def cb(done: int, total: int | None) -> None:
            if info["bytes_at_start"] is None:
                info["bytes_at_start"] = done
            info["done"], info["total"] = done, total
        return cb

    async def update_source(self, sid: str) -> None:
        src = self.sources.get(sid)
        st = self.state.source(sid)
        st.update(status="checking", error=None)
        changed = False
        try:
            if src["type"] == "kiwix":
                changed = await self._update_kiwix(src, st)
            else:
                changed = await self._update_url(src, st)
            if changed:
                # Reload the library before reporting "ready", so the source can be opened right away.
                await self.refresh_library()
                changed = False
            st["status"] = "ready" if st.get("files") else "empty"
            st["last_checked"] = time.time()
            st["last_result"] = "updated" if changed else "up to date"
        except asyncio.CancelledError:
            # What was downloaded so far stays in the .part file, so Update resumes it.
            st.update(status="ready" if st.get("files") else "empty", held=True, last_result="cancelled")
            log.info("%s: download cancelled", src["name"])
            raise
        except InsufficientSpace as err:
            st.update(status="error", error=str(err))
            log.error("%s: %s", src["name"], err)
        except Exception as err:
            st.update(status="error", error=f"{err}")
            log.error("%s: update failed: %s", src["name"], err)
        finally:
            self.state.save()
        if changed:
            await self.refresh_library()

    async def _update_kiwix(self, src: dict, st: dict) -> bool:
        filename, url = await resolve_latest(self.session, src["folder"], src["book"])
        st["remote_version"] = filename
        target = self.zim_dir / filename
        current = st.get("files") or []
        if current == [str(target)] and target.exists():
            log.info("%s is up to date (%s)", src["name"], filename)
            return False

        st["status"] = "downloading"
        await self.downloader.fetch(url, target, progress=self._progress_cb(src["id"]))

        if self.options["verify_checksums"]:
            expected = await self.downloader.expected_sha256(url)
            if expected:
                st["status"] = "verifying"
                actual = await asyncio.to_thread(sha256_file, target)
                if actual != expected:
                    target.unlink(missing_ok=True)
                    raise IOError(f"Checksum mismatch for {filename}, file discarded")

        st.update(files=[str(target)], version=filename, updated_at=time.time())
        for old in current:
            if old != str(target):
                Path(old).unlink(missing_ok=True)
                log.info("Removed old release %s", Path(old).name)
        return True

    async def _update_url(self, src: dict, st: dict) -> bool:
        dest = self.files_dir / src["id"] / filename_from_url(src["url"])
        st["status"] = "downloading"
        result = await self.downloader.fetch(
            src["url"], dest, progress=self._progress_cb(src["id"]),
            etag=st.get("etag"), last_modified=st.get("last_modified"),
        )
        if result["status"] == "not_modified" and dest.exists():
            return False
        for old in st.get("files") or []:
            if old != str(dest):
                Path(old).unlink(missing_ok=True)
        st.update(files=[str(dest)], etag=result.get("etag"), last_modified=result.get("last_modified"),
                  version=result.get("last_modified") or datetime.now().strftime("%Y-%m-%d"),
                  updated_at=time.time())
        if dest.suffix.lower() == ".zim":
            return True
        st["status"] = "indexing"
        await asyncio.to_thread(self.docs.index, src["id"], dest, src["name"])
        return False

    def adopt_existing(self) -> int:
        """Pick up archives that are already on disk but unknown to the state, e.g. content moved
        over from the app's earlier installation (before the rename), so it is not downloaded again."""
        adopted = 0
        for src in self.sources.list():
            st = self.state.source(src["id"])
            if st.get("files"):
                continue
            if src["type"] == "kiwix":
                pattern = re.compile(re.escape(src["book"]) + r"_\d{4}-\d{2}[a-z]?\.zim$")
                found = sorted(p for p in self.zim_dir.glob(f"{src['book']}_*.zim") if pattern.match(p.name))
                if not found:
                    continue
                path = found[-1]  # newest release; an update check replaces it when a newer one exists
                st.update(remote_version=path.name, version=path.name)
            else:
                path = self.files_dir / src["id"] / filename_from_url(src["url"])
                if not path.is_file():
                    continue
                st["version"] = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
            st.update(files=[str(path)], status="ready", updated_at=path.stat().st_mtime, error=None)
            adopted += 1
            log.info("Found %s on disk, using it", path.name)
        if adopted:
            self.state.save()
        return adopted

    async def reindex_documents(self) -> None:
        """Re-extract and index every downloaded non-ZIM file (after an index format change)."""
        for src in self.sources.list():
            for path in self.state.source(src["id"]).get("files", []):
                if not path.endswith(".zim") and Path(path).exists():
                    await asyncio.to_thread(self.docs.index, src["id"], Path(path), src["name"])

    async def purge(self, sid: str) -> None:
        if sid == self.current:
            raise RuntimeError("This source is downloading right now. Cancel the download first")
        st = self.state.source(sid)
        had_zim = any(p.endswith(".zim") for p in st.get("files", []))
        leftovers = list(st.get("files", []))
        if st.get("remote_version"):
            leftovers.append(str(self.zim_dir / (st["remote_version"] + ".part")))
        src = self.sources.get(sid)
        if src and src["type"] == "url":
            leftovers.append(str(self.files_dir / sid / filename_from_url(src["url"])))
        for path in leftovers:
            Path(path).unlink(missing_ok=True)
            Path(path + ".part").unlink(missing_ok=True)
        await asyncio.to_thread(self.docs.remove_source, sid)
        st.update(files=[], status="empty", version=None, etag=None, last_modified=None)
        self.state.save()
        if had_zim:
            await self.refresh_library()

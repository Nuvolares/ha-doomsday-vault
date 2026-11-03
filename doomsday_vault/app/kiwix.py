"""kiwix-serve process management, ZIM release lookup and search."""
import asyncio
import logging
import os
import re
import socket
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

KIWIX_MIRROR = "https://download.kiwix.org/zim/"
KIWIX_PORT = 8181
# Public path of the archive pages. Changing it gives every page a new URL, which also
# discards anything browsers cached under the old one (0.2.0-0.2.2 cached garbled pages).
LIBRARY_PREFIX = "/vault"


async def resolve_latest(session: aiohttp.ClientSession, folder: str, book: str) -> tuple[str, str]:
    """Return (file name, download URL) of the newest dated release of a ZIM book."""
    listing_url = f"{KIWIX_MIRROR}{folder}/"
    async with session.get(listing_url) as resp:
        resp.raise_for_status()
        html = await resp.text()
    pattern = re.compile(r'href="(' + re.escape(book) + r'_(\d{4}-\d{2}[a-z]?)\.zim)"')
    releases = {m.group(1): m.group(2) for m in pattern.finditer(html)}
    if not releases:
        raise LookupError(f"No '{book}' release found in {listing_url}")
    filename = max(releases, key=releases.get)
    return filename, listing_url + filename


async def _run(*cmd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


def _free_port(preferred: int) -> int:
    """The preferred port if nothing listens on it, otherwise any free one (portable PCs vary)."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


class Kiwix:
    def __init__(self, library_file: Path):
        self.library = library_file
        self.root = LIBRARY_PREFIX
        self.port = _free_port(KIWIX_PORT)
        self.books: dict[str, dict] = {}  # absolute ZIM path -> book info
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def content_url(self, book: dict) -> str:
        return f"{self.root}/content/{Path(book['path']).stem}/"

    async def set_root(self, root: str) -> None:
        if root != self.root:
            log.info("kiwix-serve URL root is now %s", root)
            self.root = root
            async with self._lock:
                await self._restart()

    async def rebuild(self, zim_paths: list[str]) -> None:
        """Recreate library.xml from the given ZIM files and restart kiwix-serve."""
        async with self._lock:
            tmp = self.library.with_name("library.new.xml")
            tmp.unlink(missing_ok=True)
            for path in zim_paths:
                code, out = await _run("kiwix-manage", str(tmp), "add", path)
                if code:
                    log.error("kiwix-manage could not add %s: %s", path, out)
            if tmp.exists():
                os.replace(tmp, self.library)
            else:
                self.library.unlink(missing_ok=True)
            self.books = self._parse_library()
            log.info("Library holds %d ZIM archives", len(self.books))
            await self._restart()

    def _parse_library(self) -> dict[str, dict]:
        if not self.library.exists():
            return {}
        books = {}
        for node in ET.parse(self.library).getroot().iter("book"):
            path = os.path.normpath(os.path.join(self.library.parent, node.get("path", "")))
            books[path] = {
                "id": node.get("id"),
                "path": path,
                "title": node.get("title") or Path(path).stem,
                "description": node.get("description", ""),
                "articles": int(node.get("articleCount") or 0),
                "media": int(node.get("mediaCount") or 0),
            }
        return books

    async def ensure_running(self) -> None:
        if self.books and not self.running:
            async with self._lock:
                if not self.running:
                    log.warning("kiwix-serve is not running, restarting")
                    await self._restart()

    async def stop(self) -> None:
        if not self.running:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), 10)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()

    async def _restart(self) -> None:
        await self.stop()
        self.proc = None
        if not self.books:
            return
        self.proc = await asyncio.create_subprocess_exec(
            "kiwix-serve",
            "--address", "127.0.0.1",
            "--port", str(self.port),
            "--urlRootLocation", self.root,
            "--skipInvalid",
            "--library", str(self.library),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._pipe_output(self.proc))
        async with aiohttp.ClientSession() as session:
            for _ in range(50):
                if not self.running:
                    log.error("kiwix-serve exited with code %s", self.proc.returncode)
                    return
                try:
                    async with session.get(f"{self.base}{self.root}/", timeout=aiohttp.ClientTimeout(total=2)):
                        log.info("kiwix-serve is up")
                        return
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
        log.warning("kiwix-serve did not answer within 10 seconds")

    @staticmethod
    async def _pipe_output(proc: asyncio.subprocess.Process) -> None:
        async for line in proc.stdout:
            log.debug("kiwix-serve: %s", line.decode(errors="replace").rstrip())

    # --- search ---------------------------------------------------------

    async def search(self, session: aiohttp.ClientSession, query: str, books: list[dict], per_book: int) -> dict:
        """Full-text search each book, falling back to title suggestions. Returns {path: [hits]}."""
        if not self.running:
            return {}
        sem = asyncio.Semaphore(6)

        async def one(book):
            async with sem:
                for method in (self._fulltext, self._suggest):
                    try:
                        hits = await method(session, query, book, per_book)
                    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError, ValueError, RuntimeError) as err:
                        log.debug("%s failed for %s: %s", method.__name__, book["title"], err)
                        continue
                    if hits:
                        return book["path"], hits
                return book["path"], []

        return dict(await asyncio.gather(*(one(b) for b in books)))

    async def _fulltext(self, session, query, book, limit):
        params = {"books.id": book["id"], "pattern": query, "format": "xml", "pageLength": str(limit)}
        async with session.get(f"{self.base}{self.root}/search", params=params,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            text = await resp.text()
        hits = []
        for item in ET.fromstring(text).iter("item"):
            description = re.sub(r"<[^>]+>", "", item.findtext("description") or "")
            hits.append({
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "snippet": re.sub(r"\s+", " ", description).strip()[:320],
            })
        return hits

    async def _suggest(self, session, query, book, limit):
        params = {"books.id": book["id"], "term": query, "count": str(limit)}
        async with session.get(f"{self.base}{self.root}/suggest", params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
        prefix = self.content_url(book)
        return [
            {"title": s.get("label") or s.get("value", ""), "link": prefix + s["path"], "snippet": ""}
            for s in data
            if s.get("path") and s.get("kind") != "pattern"
        ]

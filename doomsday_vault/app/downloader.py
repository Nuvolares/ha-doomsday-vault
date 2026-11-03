"""Resumable, throttled, space-aware HTTP downloads."""
import asyncio
import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable

import aiohttp

log = logging.getLogger(__name__)

CHUNK = 1 << 20
TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)


class InsufficientSpace(Exception):
    pass


def human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return str(num)


class Downloader:
    def __init__(self, session: aiohttp.ClientSession, options: dict):
        self.session = session
        self.reserve = int(options["min_free_gb"]) * 1024 ** 3
        mbit = int(options["max_download_mbit"])
        self.max_bps = mbit * 125_000 if mbit > 0 else 0

    def check_space(self, directory: Path, needed: int) -> None:
        free = shutil.disk_usage(directory).free
        if free - needed < self.reserve:
            raise InsufficientSpace(
                f"Needs {human(needed)} but only {human(free)} is free "
                f"(keeping a {human(self.reserve)} reserve)"
            )

    async def fetch(
        self,
        url: str,
        dest: Path,
        progress: Callable[[int, int | None], None] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> dict:
        """Download url to dest. Resumes a previous .part file; skips unchanged files via ETag/Last-Modified."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0

        headers = {}
        if have:
            headers["Range"] = f"bytes={have}-"
        elif dest.exists():
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        async with self.session.get(url, headers=headers, timeout=TIMEOUT) as resp:
            meta = {"etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified")}
            if resp.status == 304:
                return {"status": "not_modified", **meta}
            if resp.status == 416 and have:
                # The .part file already holds the whole file.
                total = resp.headers.get("Content-Range", "").rpartition("/")[2]
                if total.isdigit() and int(total) == have:
                    os.replace(part, dest)
                    return {"status": "downloaded", "size": have, **meta}
                part.unlink()
                raise RuntimeError("Partial download did not match the server copy, restarting next run")
            if resp.status not in (200, 206):
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            if resp.status == 200:
                have = 0
            remaining = resp.content_length
            total = have + remaining if remaining is not None else None
            self.check_space(dest.parent, remaining or 0)

            if have:
                log.info("Resuming %s at %s", dest.name, human(have))
            else:
                log.info("Downloading %s (%s)", dest.name, human(total) if total else "unknown size")

            done, started, start_bytes = have, time.monotonic(), have
            with open(part, "ab" if have else "wb") as fh:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
                    if self.max_bps:
                        ahead = (done - start_bytes) / self.max_bps - (time.monotonic() - started)
                        if ahead > 0:
                            await asyncio.sleep(ahead)

        if total is not None and done != total:
            raise IOError(f"Connection dropped at {human(done)} of {human(total)}; will resume next run")
        os.replace(part, dest)
        return {"status": "downloaded", "size": done, **meta}

    async def expected_sha256(self, url: str) -> str | None:
        try:
            async with self.session.get(url + ".sha256", timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return None
                return (await resp.text()).split()[0].lower()
        except (aiohttp.ClientError, asyncio.TimeoutError, IndexError):
            return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(8 * CHUNK):
            digest.update(block)
    return digest.hexdigest()

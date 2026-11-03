"""Full-text index for plain files (PDF, TXT, HTML) using SQLite FTS5, one row per page."""
import html
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MAX_PAGE_TEXT = 200_000   # characters kept per page
MAX_PAGES = 5_000         # pages kept per document

SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
    "source_id UNINDEXED, path UNINDEXED, page UNINDEXED, title, body, tokenize='porter unicode61')"
)


def extract_pages(path: Path) -> list[str]:
    """Text of a file split into pages (PDFs: real pages; everything else: one page)."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            result = subprocess.run(
                ["pdftotext", "-q", "-enc", "UTF-8", str(path), "-"],
                capture_output=True, timeout=900, check=False,
            )
            # pdftotext ends every page with a form feed.
            pages = result.stdout.decode(errors="replace").split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
            return [p[:MAX_PAGE_TEXT] for p in pages[:MAX_PAGES]]
        if suffix in (".txt", ".md", ".csv"):
            return [path.read_text(errors="replace")[:MAX_PAGE_TEXT]]
        if suffix in (".html", ".htm"):
            raw = path.read_text(errors="replace")
            raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
            return [html.unescape(re.sub(r"<[^>]+>", " ", raw))[:MAX_PAGE_TEXT]]
    except (OSError, subprocess.SubprocessError) as err:
        log.warning("Could not extract text from %s: %s", path, err)
    return []


class DocIndex:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.available = True
        self.needs_reindex = False
        try:
            with self._connect() as conn:
                columns = []
                try:
                    columns = [d[0] for d in conn.execute("SELECT * FROM docs LIMIT 0").description]
                except sqlite3.OperationalError:
                    pass  # table doesn't exist yet
                if columns and "page" not in columns:
                    # Index from 0.2.x stored whole documents; rebuild it page by page.
                    conn.execute("DROP TABLE docs")
                    self.needs_reindex = True
                conn.execute(SCHEMA)
        except sqlite3.OperationalError as err:
            log.error("Could not create the document index: %s", err)
            self.available = False

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def index(self, source_id: str, path: Path, title: str) -> None:
        if not self.available:
            return
        pages = extract_pages(path)
        rows = [(source_id, str(path), n, title, text) for n, text in enumerate(pages, 1) if text.strip()]
        with self._connect() as conn:
            conn.execute("DELETE FROM docs WHERE path = ?", (str(path),))
            conn.executemany("INSERT INTO docs VALUES (?, ?, ?, ?, ?)", rows)
        log.info("Indexed %s (%d pages with text)", path.name, len(rows))

    def remove_source(self, source_id: str) -> None:
        if not self.available:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM docs WHERE source_id = ?", (source_id,))

    def search(self, query: str, limit: int = 40, per_document: int = 5) -> list[dict]:
        terms = re.findall(r"\w+", query)
        if not terms or not self.available:
            return []
        match = " ".join(f'"{t}"' for t in terms)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_id, path, page, title, snippet(docs, 4, '«', '»', '…', 18) "
                "FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
                (match, limit * 3),
            ).fetchall()
        hits, per_doc = [], {}
        for source_id, path, page, title, snippet in rows:
            per_doc[path] = per_doc.get(path, 0) + 1
            if per_doc[path] > per_document:
                continue
            hits.append({"source_id": source_id, "path": path, "page": page, "title": title, "snippet": snippet})
            if len(hits) >= limit:
                break
        return hits

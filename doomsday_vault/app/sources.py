"""The user-editable source catalog (sources.yaml in the add-on config folder)."""
import logging
import re
from pathlib import Path

import yaml

from state import State

log = logging.getLogger(__name__)

TYPES = ("kiwix", "url")
SCHEDULES = ("default", "daily", "weekly", "monthly", "manual")
FIELDS = ("id", "name", "category", "type", "folder", "book", "url",
          "enabled", "restyle", "schedule", "size_hint", "description")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SIZE = re.compile(r"^\s*~?\s*([\d.]+)\s*([KMGT]?)i?B?\s*$", re.I)
_UNITS = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}

HEADER = """\
# Doomsday Vault library sources.
# Edit here (then restart the app) or in the Archive tab of the side panel.
# type: kiwix -> folder + book from https://download.kiwix.org/zim/
# type: url   -> direct link to any file (PDF, TXT, HTML, ZIM ...)
# schedule: default | daily | weekly | monthly | manual
"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "source"


def parse_size(text) -> int | None:
    match = _SIZE.match(str(text or ""))
    if not match:
        return None
    return int(float(match.group(1)) * 1024 ** _UNITS[match.group(2).upper()])


def normalize(raw: dict) -> dict:
    """Validate a source definition and return a clean copy (raises ValueError)."""
    src = {k: raw[k] for k in FIELDS if raw.get(k) not in (None, "")}
    name = str(src.get("name", "")).strip()
    if not name:
        raise ValueError("A name is required")
    src["name"] = name
    src["id"] = slugify(src.get("id") or name)
    src["type"] = src.get("type", "url")
    if src["type"] not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    if src["type"] == "kiwix":
        for key in ("folder", "book"):
            value = str(src.get(key, "")).strip().strip("/")
            if not _SAFE_NAME.match(value):
                raise ValueError(f"'{key}' is required and may only contain letters, digits, . _ -")
            src[key] = value
        src.pop("url", None)
    else:
        url = str(src.get("url", "")).strip()
        if not re.match(r"^https?://", url):
            raise ValueError("url must start with http:// or https://")
        src["url"] = url
        src.pop("folder", None)
        src.pop("book", None)
    src["enabled"] = bool(raw.get("enabled", True))
    # restyle: show pages in the library's own design (off for maps and other interactive content)
    src["restyle"] = bool(raw.get("restyle", True))
    src["schedule"] = src.get("schedule", "default")
    if src["schedule"] not in SCHEDULES:
        raise ValueError(f"schedule must be one of {', '.join(SCHEDULES)}")
    src["category"] = str(src.get("category") or "Uncategorized").strip()
    for key in ("size_hint", "description"):
        if key in src:
            src[key] = str(src[key]).strip()
    return src


class SourceStore:
    def __init__(self, path: Path, defaults_path: Path, state: State):
        self.path = path
        self.defaults_path = defaults_path
        self.state = state
        self.items: dict[str, dict] = {}

    def load(self) -> None:
        defaults = (yaml.safe_load(self.defaults_path.read_text()) or {}).get("sources", [])
        first_run = not self.path.exists()
        raw_items = [] if first_run else (yaml.safe_load(self.path.read_text()) or {}).get("sources") or []

        self.items = {}
        defaults_by_id = {d["id"]: d for d in defaults}
        for raw in raw_items:
            # Settings introduced after the user's sources.yaml was written inherit the built-in value.
            if isinstance(raw, dict) and raw.get("id") in defaults_by_id and "restyle" not in raw:
                raw = {**raw, "restyle": defaults_by_id[raw["id"]].get("restyle", True)}
            try:
                src = normalize(raw)
            except (ValueError, AttributeError) as err:
                log.warning("Skipping invalid source %r: %s", raw, err)
                continue
            self.items[src["id"]] = src

        # Defaults added in a newer add-on version show up automatically, but
        # defaults the user deleted stay deleted.
        known = set() if first_run else set(self.state.data.get("known_defaults", []))
        added = [d for d in defaults if d["id"] not in known and d["id"] not in self.items]
        for raw in added:
            src = normalize(raw)
            self.items[src["id"]] = src
        if added and not first_run:
            log.info("Added %d new built-in sources: %s", len(added), ", ".join(d["id"] for d in added))

        self.state.data["known_defaults"] = sorted(known | {d["id"] for d in defaults})
        self.state.save()
        if added or first_run:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump({"sources": list(self.items.values())}, sort_keys=False, allow_unicode=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(HEADER + "\n" + body)
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        return list(self.items.values())

    def get(self, sid: str) -> dict | None:
        return self.items.get(sid)

    def put(self, raw: dict, existing_id: str | None = None) -> dict:
        """Create (existing_id None) or replace a source. Returns the stored copy."""
        src = normalize(raw)
        if existing_id:
            if existing_id not in self.items:
                raise KeyError(existing_id)
            src["id"] = existing_id
        else:
            base, n = src["id"], 2
            while src["id"] in self.items:
                src["id"] = f"{base}-{n}"
                n += 1
        self.items[src["id"]] = src
        self.save()
        return src

    def delete(self, sid: str) -> None:
        self.items.pop(sid, None)
        self.save()

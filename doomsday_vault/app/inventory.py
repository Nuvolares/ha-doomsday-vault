"""Supply inventory: food, water, medical supplies and equipment, with expiration dates and photos."""
from __future__ import annotations  # the list() method below would shadow list[...] annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CATEGORIES = [
    "Food", "Water", "Medical", "Hygiene", "Energy & Fuel", "Tools", "Equipment",
    "Communication", "Clothing & Shelter", "Documents",
]
TEXT_FIELDS = ("name", "category", "unit", "location", "notes")
DATE_FIELDS = ("purchased", "expires")
MAX_PHOTOS = 6
# Magic bytes of the image types we accept (the panel converts photos to JPEG before upload).
IMAGE_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}


def image_ext(data: bytes) -> str | None:
    for magic, ext in IMAGE_TYPES.items():
        if data.startswith(magic) and (ext != ".webp" or data[8:12] == b"WEBP"):
            return ext
    return None


class Inventory:
    def __init__(self, db_path: Path, photo_dir: Path, readonly: bool = False):
        self.db_path = db_path
        self.photo_dir = photo_dir
        self.readonly = readonly
        if not readonly:
            photo_dir.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                    quantity REAL, unit TEXT, location TEXT, purchased TEXT, expires TEXT,
                    notes TEXT, photos TEXT NOT NULL DEFAULT '[]', created REAL, updated REAL)""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["photos"] = json.loads(item["photos"] or "[]")
        return item

    def list(self) -> list[dict]:
        if not self.db_path.exists():
            return []
        with self._db() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM items ORDER BY category, name COLLATE NOCASE")]

    def get(self, item_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row(row) if row else None

    def put(self, data: dict, item_id: str | None = None) -> dict:
        item = {f: str(data.get(f) or "").strip() for f in TEXT_FIELDS}
        if not item["name"]:
            raise ValueError("Name is required")
        item["category"] = item["category"] or "Equipment"
        for field in DATE_FIELDS:
            value = str(data.get(field) or "").strip()
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValueError(f"{field}: use the format YYYY-MM-DD")
            item[field] = value or None
        quantity = data.get("quantity")
        try:
            item["quantity"] = float(quantity) if quantity not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("Quantity must be a number")

        now = time.time()
        with self._db() as db:
            if item_id:
                if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                    raise KeyError(item_id)
                db.execute("""UPDATE items SET name=:name, category=:category, quantity=:quantity, unit=:unit,
                              location=:location, purchased=:purchased, expires=:expires, notes=:notes,
                              updated=:updated WHERE id=:id""", {**item, "id": item_id, "updated": now})
            else:
                item_id = uuid.uuid4().hex
                db.execute("""INSERT INTO items (id, name, category, quantity, unit, location, purchased,
                              expires, notes, photos, created, updated)
                              VALUES (:id, :name, :category, :quantity, :unit, :location, :purchased,
                              :expires, :notes, '[]', :updated, :updated)""", {**item, "id": item_id, "updated": now})
        return self.get(item_id)

    def delete(self, item_id: str) -> None:
        item = self.get(item_id)
        if not item:
            raise KeyError(item_id)
        for name in item["photos"]:
            (self.photo_dir / name).unlink(missing_ok=True)
        with self._db() as db:
            db.execute("DELETE FROM items WHERE id = ?", (item_id,))

    def _set_photos(self, item_id: str, photos: list[str]) -> dict:
        with self._db() as db:
            db.execute("UPDATE items SET photos = ?, updated = ? WHERE id = ?", (json.dumps(photos), time.time(), item_id))
        return self.get(item_id)

    def add_photo(self, item_id: str, data: bytes) -> dict:
        item = self.get(item_id)
        if not item:
            raise KeyError(item_id)
        ext = image_ext(data)
        if not ext:
            raise ValueError("Only JPEG, PNG or WebP photos can be added")
        if len(item["photos"]) >= MAX_PHOTOS:
            raise ValueError(f"An item can have at most {MAX_PHOTOS} photos")
        name = f"{item_id}-{uuid.uuid4().hex[:8]}{ext}"
        (self.photo_dir / name).write_bytes(data)
        return self._set_photos(item_id, item["photos"] + [name])

    def remove_photo(self, item_id: str, name: str) -> dict:
        item = self.get(item_id)
        if not item or name not in item["photos"]:
            raise KeyError(name)
        (self.photo_dir / name).unlink(missing_ok=True)
        return self._set_photos(item_id, [p for p in item["photos"] if p != name])

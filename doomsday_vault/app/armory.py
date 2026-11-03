"""Armory: weapons, ammunition and accessories, kept in sync with WeaponLog.

The Armory always works from a local copy (armory.json), so it stays usable when WeaponLog
cannot be reached. Every record keeps
    base     the WeaponLog version from the last successful sync
    changed  fields edited here since then (not yet in WeaponLog)
and is shown as base + changed. A sync merges field by field:
  - a field changed only in WeaponLog takes the WeaponLog value,
  - a field changed here is sent to WeaponLog, even if it changed there too (Home Assistant
    wins; the overwritten WeaponLog value is written to the sync log),
  - ammo counts merge as differences, so rounds used here and in WeaponLog both count.
WeaponLog records carry no modification time, which is why the last synced copy is kept.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1/"
TIMEOUT = aiohttp.ClientTimeout(total=60, sock_connect=15)
LOG_SIZE = 60

# Fields the Armory can edit. Others (rounds fired, price per round, photos ...) are shown as WeaponLog has them.
KINDS = {
    "weapons": {
        "path": "weapons/",
        "fields": ["name", "caliber", "weapon_type", "serial_number", "purchase_date", "purchase_price",
                   "rental_cost", "barrel_length", "barrel_length_unit", "twist_rate", "twist_direction",
                   "rounds_fired_base"],
        "photo": "photo",
    },
    "ammo": {
        "path": "ammo/",
        "fields": ["name", "caliber", "grain", "style", "quantity_on_hand", "ballistic_coefficient",
                   "bc_standard", "muzzle_velocity", "bullet_length", "bullet_length_unit", "test_barrel_length"],
        "photo": "box_photo",
    },
    "accessories": {
        "path": "accessories/",
        "fields": ["name", "category", "type", "accessory_type", "purchase_date", "price", "sight_height",
                   "sight_height_unit", "adjustment_type", "adjustment_per_click", "zero_distance",
                   "zero_distance_unit"],
        "photo": "photo",
    },
}
# Fields WeaponLog stores as numbers or dates: an empty form value is sent as null.
NULLABLE = {"purchase_date", "purchase_price", "rental_cost", "barrel_length", "rounds_fired_base", "grain",
            "ballistic_coefficient", "muzzle_velocity", "bullet_length", "test_barrel_length", "price",
            "sight_height", "adjustment_per_click", "zero_distance"}
REQUIRED = {"weapons": ("name", "caliber"), "ammo": ("name", "caliber", "grain", "style"),
            "accessories": ("name", "category")}
COUNT_FIELD = "quantity_on_hand"


class SyncError(Exception):
    pass


def norm(value):
    """Comparable form of a value: WeaponLog sends decimals as strings ("12.50"), forms send "12.5"."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        return str(Decimal(str(value)).normalize())
    except (InvalidOperation, ValueError):
        return str(value).strip()


def as_int(value) -> int:
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return 0


class Armory:
    def __init__(self, path: Path, photo_dir: Path, session: aiohttp.ClientSession, readonly: bool = False):
        self.path = path
        self.photo_dir = photo_dir
        self.session = session
        self.readonly = readonly
        self.lock = asyncio.Lock()
        self.data = {"config": {"url": "", "token": "", "interval": 60}, "categories": [],
                     "sync": {"last_success": None, "last_attempt": None, "error": None, "running": False,
                              "photos": None, "log": []}}
        for kind in KINDS:
            self.data[kind] = {}
        try:
            loaded = json.loads(path.read_text())
            for key, value in loaded.items():
                if isinstance(value, dict) and isinstance(self.data.get(key), dict):
                    self.data[key].update(value)
                else:
                    self.data[key] = value
        except FileNotFoundError:
            pass
        except ValueError as err:
            log.error("Armory data unreadable (%s); starting empty", err)
        self.data["sync"]["running"] = False

    # --- storage ------------------------------------------------------------

    def save(self) -> None:
        if self.readonly:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str))
        os.replace(tmp, self.path)

    def _log(self, msg: str, level: int = logging.INFO) -> None:
        log.log(level, "WeaponLog sync: %s", msg)
        entries = self.data["sync"]["log"]
        entries.append({"t": time.time(), "msg": msg, "level": logging.getLevelName(level)})
        del entries[:-LOG_SIZE]

    @property
    def config(self) -> dict:
        return self.data["config"]

    @property
    def configured(self) -> bool:
        return bool(self.config.get("url") and self.config.get("token"))

    # --- views ----------------------------------------------------------------

    @staticmethod
    def view_record(kind: str, key: str, rec: dict) -> dict:
        item = {**(rec.get("base") or {}), **rec.get("changed", {})}
        item.update(key=key, remote_id=rec.get("id"), pending=bool(rec.get("changed") or rec.get("deleted")
                                                                    or rec.get("id") is None),
                    changed_fields=sorted(rec.get("changed", {})), push_error=rec.get("push_error"),
                    local_photo=rec.get("local_photo"))
        return item

    def overview(self) -> dict:
        items = {kind: [self.view_record(kind, key, rec) for key, rec in self.data[kind].items()
                        if not rec.get("deleted")] for kind in KINDS}
        pending = sum(1 for kind in KINDS for rec in self.data[kind].values()
                      if rec.get("changed") or rec.get("deleted") or rec.get("id") is None)
        cfg = self.config
        return {"configured": self.configured, "url": cfg.get("url", ""), "token_set": bool(cfg.get("token")),
                "interval": cfg.get("interval", 60), "sync": self.data["sync"], "pending": pending,
                "categories": [c["name"] for c in self.data["categories"]], "items": items,
                "readonly": self.readonly}

    # --- local edits (work with or without a connection) ------------------------

    def edit(self, kind: str, key: str | None, values: dict) -> dict:
        if kind not in KINDS:
            raise KeyError(kind)
        fields = KINDS[kind]["fields"]
        clean = {f: values[f] for f in fields if f in values}
        for f in clean:
            if isinstance(clean[f], str):
                clean[f] = clean[f].strip()
        if key is None:
            missing = [f for f in REQUIRED[kind] if norm(clean.get(f)) is None]
            if missing:
                raise ValueError("Required: " + ", ".join(missing))
            key = "l" + uuid.uuid4().hex[:12]
            self.data[kind][key] = {"id": None, "base": None, "changed": clean}
        else:
            rec = self.data[kind].get(key)
            if not rec or rec.get("deleted"):
                raise KeyError(key)
            base = rec.get("base") or {}
            for f, value in clean.items():
                if f in REQUIRED[kind] and norm(value) is None:
                    raise ValueError(f"{f} cannot be empty")
                if rec["id"] is not None and norm(value) == norm(base.get(f)):
                    rec["changed"].pop(f, None)  # back to the WeaponLog value
                else:
                    rec["changed"][f] = value
            rec.pop("push_error", None)
        self.save()
        return self.view_record(kind, key, self.data[kind][key])

    def remove(self, kind: str, key: str) -> None:
        rec = self.data[kind].get(key)
        if not rec:
            raise KeyError(key)
        if rec["id"] is None:
            del self.data[kind][key]  # never reached WeaponLog
        else:
            rec["deleted"] = True
        self.save()

    def configure(self, url: str | None, token: str | None, interval) -> None:
        cfg = self.config
        if url is not None:
            url = url.strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                url = "https://" + url
            if url != cfg.get("url"):
                # Another WeaponLog server holds other records: start from its data, not ours.
                for kind in KINDS:
                    self.data[kind] = {}
                self.data["categories"] = []
                self.data["sync"].update(last_success=None, error=None, photos=None)
            cfg["url"] = url
        if token:
            cfg["token"] = token.strip()
        if interval not in (None, ""):
            cfg["interval"] = max(5, min(1440, int(interval)))
        self.save()

    # --- WeaponLog API ------------------------------------------------------------

    async def _request(self, method: str, path: str, payload: dict | None = None):
        url = self.config["url"] + API_PREFIX + path
        headers = {"Authorization": f"Token {self.config['token']}", "Accept": "application/json"}
        try:
            async with self.session.request(method, url, json=payload, headers=headers, timeout=TIMEOUT,
                                            allow_redirects=False) as resp:
                if resp.status in (401, 403):
                    raise SyncError("WeaponLog rejected the API token. Create a new one in WeaponLog under "
                                    "Settings and enter it here.")
                if resp.status == 404 and method == "DELETE":
                    return None
                if resp.status >= 500:
                    raise SyncError(f"WeaponLog is temporarily unavailable (HTTP {resp.status})")
                if resp.status in (301, 302, 307, 308):
                    raise SyncError("WeaponLog redirected the request; check the address (https:// and no extra path)")
                if resp.status >= 400:
                    body = await resp.text()
                    raise ValueError(f"HTTP {resp.status}: {body[:300]}")
                if resp.status == 204:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            detail = str(err) or err.__class__.__name__
            raise SyncError(f"WeaponLog is unreachable ({detail})")

    async def _fetch_all(self, path: str) -> list[dict]:
        results, page = [], 1
        while True:
            data = await self._request("GET", f"{path}?page={page}")
            if isinstance(data, list):  # unpaginated endpoint
                return data
            results.extend(data.get("results", []))
            if not data.get("next"):
                return results
            page += 1

    async def _category_id(self, name: str) -> int:
        for cat in self.data["categories"]:
            if cat["name"].lower() == name.lower():
                return cat["id"]
        cat = await self._request("POST", "accessory-categories/", {"name": name})
        self.data["categories"].append(cat)
        return cat["id"]

    async def _payload(self, kind: str, values: dict) -> dict:
        payload = {}
        for f, value in values.items():
            if f == "category" and kind == "accessories":
                payload["category_id"] = await self._category_id(str(value))
            elif f in NULLABLE and value in ("", None):
                payload[f] = None
            else:
                payload[f] = "" if value is None else value
        return payload

    # --- sync ---------------------------------------------------------------------

    async def sync(self) -> None:
        if self.readonly or not self.configured or self.lock.locked():
            return
        async with self.lock:
            status = self.data["sync"]
            status.update(running=True, last_attempt=time.time())
            try:
                self.data["categories"] = await self._request("GET", "accessory-categories/") or []
                for kind in KINDS:
                    await self._sync_kind(kind)
                await self._fetch_photos()
                status.update(last_success=time.time(), error=None)
            except SyncError as err:
                status["error"] = str(err)
                self._log(f"failed, keeping the local copy: {err}", logging.WARNING)
            except Exception as err:  # never lose the local copy over an unexpected answer
                status["error"] = f"Unexpected error: {err}"
                log.exception("WeaponLog sync failed")
            finally:
                status["running"] = False
                self.save()

    async def _sync_kind(self, kind: str) -> None:
        spec = KINDS[kind]
        records = self.data[kind]
        remote = {r["id"]: r for r in await self._fetch_all(spec["path"])}

        for key, rec in list(records.items()):
            name = (rec.get("base") or {}).get("name") or rec.get("changed", {}).get("name") or key
            try:
                if rec["id"] is None:
                    await self._create(kind, key, rec)
                    continue
                current = remote.pop(rec["id"], None)
                if current is None:
                    if rec.get("changed") and not rec.get("deleted"):
                        # Deleted in WeaponLog but edited here: Home Assistant wins, so it is created again.
                        rec["changed"] = {**{f: v for f, v in (rec["base"] or {}).items() if f in spec["fields"]},
                                          **rec["changed"]}
                        rec.update(id=None, base=None)
                        self._log(f"{name} was deleted in WeaponLog but changed here; adding it again")
                        await self._create(kind, key, rec)
                    else:
                        del records[key]
                        self._log(f"{name} was removed in WeaponLog")
                    continue
                if rec.get("deleted"):
                    await self._request("DELETE", f"{spec['path']}{rec['id']}/")
                    del records[key]
                    self._log(f"{name} deleted in WeaponLog")
                    continue
                if rec.get("changed"):
                    await self._push(kind, key, rec, current)
                else:
                    if rec.get("base") and self._photo_path(kind, rec["base"]) != self._photo_path(kind, current):
                        rec.pop("local_photo", None)
                    rec["base"] = current
            except ValueError as err:  # WeaponLog refused this record; keep it and carry on with the rest
                rec["push_error"] = str(err)
                self._log(f"{name}: WeaponLog did not accept the change ({err})", logging.WARNING)

        for rid, current in remote.items():  # new in WeaponLog
            records[f"r{rid}"] = {"id": rid, "base": current, "changed": {}}

    async def _create(self, kind: str, key: str, rec: dict) -> None:
        if rec.get("deleted"):
            del self.data[kind][key]
            return
        sent = dict(rec["changed"])
        created = await self._request("POST", KINDS[kind]["path"], await self._payload(kind, sent))
        self._settle(rec, sent, created)
        rec["id"] = created["id"]
        if self.data[kind].pop(key, None) is None:
            rec["deleted"] = True  # removed here while it was being added: the next sync deletes it again
        self.data[kind][f"r{created['id']}"] = rec
        self._log(f"{created.get('name', 'item')} added to WeaponLog")

    async def _push(self, kind: str, key: str, rec: dict, current: dict) -> None:
        base, sent = rec["base"] or {}, dict(rec["changed"])
        payload_values = {}
        for f, value in sent.items():
            if f == COUNT_FIELD:
                # Merge counts as differences: rounds used here and rounds used in WeaponLog both count.
                value = max(0, as_int(current.get(f)) + as_int(value) - as_int(base.get(f)))
            elif norm(current.get(f)) != norm(base.get(f)) and norm(current.get(f)) != norm(value):
                self._log(f"{current.get('name')}: '{f}' changed in both places; kept '{value}' from Home "
                          f"Assistant, WeaponLog had '{current.get(f)}'", logging.WARNING)
            payload_values[f] = value
        updated = await self._request("PATCH", f"{KINDS[kind]['path']}{rec['id']}/",
                                      await self._payload(kind, payload_values))
        self._settle(rec, sent, updated)
        self._log(f"{updated.get('name', 'item')} updated in WeaponLog ({', '.join(sorted(sent))})")

    @staticmethod
    def _settle(rec: dict, sent: dict, answer: dict) -> None:
        """WeaponLog accepted `sent`: it becomes the new base. Edits made while the request ran stay pending."""
        rec["base"] = answer
        for f, value in sent.items():
            if f in rec["changed"] and rec["changed"][f] == value:
                del rec["changed"][f]
        rec.pop("push_error", None)

    # --- photos (best effort: WeaponLog serves media to logged-in browsers) ---------

    @staticmethod
    def _photo_path(kind: str, record: dict) -> str | None:
        return (record or {}).get(KINDS[kind]["photo"])

    async def _fetch_photos(self) -> None:
        if self.data["sync"].get("photos") is False:
            return  # this WeaponLog does not hand photos to API tokens
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Token {self.config['token']}"}
        for kind in KINDS:
            for rec in self.data[kind].values():
                path = self._photo_path(kind, rec.get("base"))
                if not path or rec.get("local_photo"):
                    continue
                name = f"{kind}-{hashlib.sha1(path.encode()).hexdigest()[:16]}{Path(path).suffix.lower()[:5]}"
                try:
                    async with self.session.get(self.config["url"] + path, headers=headers, timeout=TIMEOUT,
                                                allow_redirects=False) as resp:
                        if resp.status != 200 or not resp.content_type.startswith("image/"):
                            self.data["sync"]["photos"] = False
                            self._log("photos are not available with an API token; items show without photos")
                            return
                        (self.photo_dir / name).write_bytes(await resp.read())
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return
                rec["local_photo"] = name
                self.data["sync"]["photos"] = True

    async def scheduler(self) -> None:
        await asyncio.sleep(20)
        while True:
            last = self.data["sync"].get("last_attempt") or 0
            if self.configured and time.time() - last >= self.config.get("interval", 60) * 60:
                await self.sync()
            await asyncio.sleep(60)

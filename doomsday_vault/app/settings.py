"""Add-on options and filesystem locations."""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Portable mode: running from an exported drive on a Windows PC (see exporter.py).
PORTABLE = bool(os.environ.get("DV_PORTABLE"))

DATA_DIR = Path(os.environ.get("DV_DATA_DIR", "/data"))
CONFIG_DIR = Path(os.environ.get("DV_CONFIG_DIR", "/config"))
APP_DIR = Path(__file__).resolve().parent

OPTIONS_FILE = DATA_DIR / "options.json"
STATE_FILE = DATA_DIR / "state.json"
LIBRARY_FILE = DATA_DIR / "library.xml"
DOCS_DB = DATA_DIR / "documents.db"
INVENTORY_DB = DATA_DIR / "inventory.db"
INVENTORY_PHOTOS = DATA_DIR / "inventory_photos"
ARMORY_FILE = DATA_DIR / "armory.json"
ARMORY_PHOTOS = DATA_DIR / "armory_photos"
SOURCES_FILE = CONFIG_DIR / "sources.yaml"
DEFAULT_SOURCES = APP_DIR / "default_sources.yaml"
STATIC_DIR = APP_DIR / "static"

DEFAULT_OPTIONS = {
    "storage_path": "/media/doomsday_vault",
    "update_schedule": "weekly",
    "update_hour": 3,
    "auto_start_downloads": False,
    "min_free_gb": 25,
    "max_download_mbit": 0,
    "verify_checksums": True,
    "log_level": "info",
}

ALLOWED_STORAGE_ROOTS = ("/media", "/share")
# Where the app kept its content when it was still called Doomsday Vault (moved on first start).
OLD_STORAGE_PATH = "/media/doomsday_library"


def load_options() -> dict:
    opts = dict(DEFAULT_OPTIONS)
    try:
        opts.update(json.loads(OPTIONS_FILE.read_text()))
    except FileNotFoundError:
        log.warning("%s not found, using defaults", OPTIONS_FILE)
    except ValueError as err:
        log.error("Could not parse %s: %s", OPTIONS_FILE, err)

    if os.environ.get("DV_STORAGE"):
        opts["storage_path"] = os.path.normpath(os.environ["DV_STORAGE"])
        return opts
    storage = os.path.normpath(str(opts["storage_path"]))
    if not os.environ.get("DV_DEV") and not any(
        storage == root or storage.startswith(root + "/") for root in ALLOWED_STORAGE_ROOTS
    ):
        log.error("storage_path %s is not under /media or /share, falling back to default", storage)
        storage = DEFAULT_OPTIONS["storage_path"]
    opts["storage_path"] = storage
    return opts

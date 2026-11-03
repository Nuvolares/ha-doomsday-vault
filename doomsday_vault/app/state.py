"""Persistent runtime state (what is installed, when it was checked)."""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: Path):
        self.path = path
        self.readonly = False  # portable copies never rewrite their state
        self.data = {"sources": {}, "known_defaults": [], "last_cycle": None, "initiated": False}
        try:
            self.data.update(json.loads(path.read_text()))
        except FileNotFoundError:
            pass
        except ValueError as err:
            log.error("State file %s is corrupt (%s), starting fresh", path, err)

    def source(self, sid: str) -> dict:
        return self.data["sources"].setdefault(sid, {})

    def drop(self, sid: str) -> None:
        self.data["sources"].pop(sid, None)

    def save(self) -> None:
        if self.readonly:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)

# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
# =============================================================================
"""
voxrail_cm.store
================
Per-project SQLite configuration graph.

Two linking layers, with the train state as the hub:

  Layer 1 (use case level)   UC ─performed_by→ Actor
                             UC ─valid_in / transitions_from / transitions_to→ TrainState
                             TC ─verifies→ UC, FM ─analyses→ UC, HZ ─derived_from→ UC|FM
  Layer 2 (logical level)    TrainState ─realised_by_electrical→ Interface   (from ED)
                             TrainState ─realised_by_software→ SW element    (from SD)
                             ED ─defines→ Interface, SD ─defines→ SW element,
                             ED ─realises→ UC, SD ─models_uc→ UC, CC ─implements→ UC|SD

Node keys: artefacts use their ref (UC-ABC-001); entities use "<kind>:<id>".
UC↔UC and State↔State couplings are derived at query time, never stored.
"""

import difflib
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime

ENTITY_KINDS = ("actor", "state", "interface", "sw_element")
LAYER = {
    "UC": 1, "TC": 1, "FM": 1, "HZ": 1, "actor": 1,
    "state": 0,                                   # hub: shown on both layers
    "ED": 2, "SD": 2, "CC": 2, "interface": 2, "sw_element": 2,
}
UC_STATE_LINKS = ("valid_in", "transitions_from", "transitions_to")
SIMILARITY_THRESHOLD = 0.8

SCHEMA = """
CREATE TABLE IF NOT EXISTS artefact (
    ref TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, version INTEGER NOT NULL,
    text_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Draft',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artefact_version (
    ref TEXT NOT NULL, version INTEGER NOT NULL, full_text TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (ref, version)
);
CREATE TABLE IF NOT EXISTS entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL,
    norm TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', attrs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, UNIQUE (kind, norm)
);
CREATE TABLE IF NOT EXISTS alias (
    kind TEXT NOT NULL, norm TEXT NOT NULL, entity_id INTEGER NOT NULL,
    PRIMARY KEY (kind, norm)
);
CREATE TABLE IF NOT EXISTS link (
    src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'auto', source_ref TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS link_dst ON link (dst);
CREATE TABLE IF NOT EXISTS suggestion (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    new_entity_id INTEGER NOT NULL, candidate_entity_id INTEGER NOT NULL,
    score REAL NOT NULL, artefact_ref TEXT, status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL, UNIQUE (new_entity_id, candidate_entity_id)
);
"""


def normalise(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[_\-/]+", " ", n)
    n = re.sub(r"[^\w ]+", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ekey(kind: str, eid: int) -> str:
    return f"{kind}:{eid}"


class ConfigStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ── Entities ──────────────────────────────────────────────────────────────
    def resolve(self, kind: str, name: str, artefact_ref: str | None = None,
                attrs: dict | None = None, scope: str | None = None) -> int:
        """Find or create an entity by name/alias. New names get a near-match suggestion."""
        norm = normalise(name)
        if scope:                                    # sw elements are owned by one SD
            norm = f"{scope.lower()}::{norm}"
        with self._lock:
            row = self.db.execute("SELECT entity_id FROM alias WHERE kind=? AND norm=?",
                                  (kind, norm)).fetchone()
            if row:
                return row["entity_id"]
            row = self.db.execute("SELECT id, attrs FROM entity WHERE kind=? AND norm=?",
                                  (kind, norm)).fetchone()
            if row:
                if attrs:
                    merged = {**json.loads(row["attrs"]), **{k: v for k, v in attrs.items()
                                                             if v and v != "Unspecified"}}
                    self.db.execute("UPDATE entity SET attrs=? WHERE id=?",
                                    (json.dumps(merged), row["id"]))
                return row["id"]
            cur = self.db.execute(
                "INSERT INTO entity (kind, name, norm, status, attrs, created_at) VALUES (?,?,?,?,?,?)",
                (kind, name.strip(), norm, "proposed", json.dumps(attrs or {}), _now()))
            new_id = cur.lastrowid
            if not scope:
                self._suggest(kind, new_id, norm, artefact_ref)
            return new_id

    def _suggest(self, kind: str, new_id: int, norm: str, artefact_ref: str | None) -> None:
        rows = self.db.execute("SELECT id, norm FROM entity WHERE kind=? AND id<>? AND norm NOT LIKE '%::%'",
                               (kind, new_id)).fetchall()
        for r in rows:
            score = difflib.SequenceMatcher(None, norm, r["norm"]).ratio()
            contained = norm in r["norm"] or r["norm"] in norm
            if score >= SIMILARITY_THRESHOLD or (contained and min(len(norm), len(r["norm"])) >= 4):
                self.db.execute(
                    "INSERT OR IGNORE INTO suggestion (kind, new_entity_id, candidate_entity_id, score,"
                    " artefact_ref, created_at) VALUES (?,?,?,?,?,?)",
                    (kind, new_id, r["id"], round(max(score, 0.8 if contained else 0), 2),
                     artefact_ref, _now()))

    def add_catalogue(self, kind: str, names: list[str], confirmed: bool = True) -> list[int]:
        ids = []
        with self._lock:
            for n in names:
                eid = self.resolve(kind, n)
                if confirmed:
                    self.db.execute("UPDATE entity SET status='confirmed' WHERE id=?", (eid,))
                ids.append(eid)
            self.db.commit()
        return ids

    def confirm_entity(self, eid: int) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE entity SET status='confirmed' WHERE id=?", (eid,))
            self.db.execute("UPDATE suggestion SET status='rejected' WHERE new_entity_id=? AND status='open'",
                            (eid,))
            self.db.commit()
            return cur.rowcount > 0

    def merge_entity(self, src_id: int, into_id: int) -> None:
        """Make src an alias of into and move its links across."""
        with self._lock:
            src = self.db.execute("SELECT * FROM entity WHERE id=?", (src_id,)).fetchone()
            dst = self.db.execute("SELECT * FROM entity WHERE id=?", (into_id,)).fetchone()
            if not src or not dst or src["kind"] != dst["kind"] or src_id == into_id:
                raise ValueError("cannot merge these entities")
            kind = src["kind"]
            old, new = ekey(kind, src_id), ekey(kind, into_id)
            for col in ("src", "dst"):
                for l in self.db.execute(f"SELECT * FROM link WHERE {col}=?", (old,)).fetchall():
                    s = new if col == "src" else l["src"]
                    d = new if col == "dst" else l["dst"]
                    self.db.execute("INSERT OR IGNORE INTO link (src,dst,kind,origin,source_ref,created_at)"
                                    " VALUES (?,?,?,?,?,?)",
                                    (s, d, l["kind"], l["origin"], l["source_ref"], l["created_at"]))
                self.db.execute(f"DELETE FROM link WHERE {col}=?", (old,))
            self.db.execute("UPDATE alias SET entity_id=? WHERE entity_id=?", (into_id, src_id))
            self.db.execute("INSERT OR REPLACE INTO alias (kind, norm, entity_id) VALUES (?,?,?)",
                            (kind, src["norm"], into_id))
            self.db.execute("DELETE FROM entity WHERE id=?", (src_id,))
            self.db.execute("UPDATE suggestion SET status='accepted' WHERE new_entity_id=? AND candidate_entity_id=?",
                            (src_id, into_id))
            self.db.execute("DELETE FROM suggestion WHERE (new_entity_id=? OR candidate_entity_id=?) AND status='open'",
                            (src_id, src_id))
            self.db.commit()

    def entities(self, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM entity", ()
        if kind:
            q, args = q + " WHERE kind=?", (kind,)
        rows = self.db.execute(q + " ORDER BY kind, name", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"])
            d["key"] = ekey(d["kind"], d["id"])
            d["aliases"] = [a["norm"] for a in self.db.execute(
                "SELECT norm FROM alias WHERE entity_id=?", (r["id"],)).fetchall()]
            out.append(d)
        return out

    def entity(self, eid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM entity WHERE id=?", (eid,)).fetchone()
        return dict(r) if r else None

    def find_entity(self, kind: str, name: str) -> dict | None:
        norm = normalise(name)
        r = self.db.execute("SELECT entity_id FROM alias WHERE kind=? AND norm=?", (kind, norm)).fetchone()
        if r:
            return self.entity(r["entity_id"])
        r = self.db.execute("SELECT * FROM entity WHERE kind=? AND norm=?", (kind, norm)).fetchone()
        return dict(r) if r else None

    def suggestions(self, status: str = "open") -> list[dict]:
        rows = self.db.execute(
            "SELECT s.*, n.name AS new_name, c.name AS candidate_name FROM suggestion s"
            " JOIN entity n ON n.id=s.new_entity_id JOIN entity c ON c.id=s.candidate_entity_id"
            " WHERE s.status=? ORDER BY s.id", (status,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_suggestion(self, sid: int, accept: bool) -> dict:
        s = self.db.execute("SELECT * FROM suggestion WHERE id=?", (sid,)).fetchone()
        if not s or s["status"] != "open":
            raise ValueError("no open suggestion with that id")
        if accept:
            self.merge_entity(s["new_entity_id"], s["candidate_entity_id"])
        else:
            with self._lock:
                self.db.execute("UPDATE suggestion SET status='rejected' WHERE id=?", (sid,))
                self.db.commit()
        return dict(s)

    # ── Artefacts ─────────────────────────────────────────────────────────────
    def upsert_artefact(self, type_: str, ref: str, title: str, text: str) -> int:
        """Store a new version if the text changed. Returns the current version."""
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            row = self.db.execute("SELECT version, text_hash FROM artefact WHERE ref=?", (ref,)).fetchone()
            if row and row["text_hash"] == h:
                return row["version"]
            version = (row["version"] + 1) if row else 1
            if row:
                self.db.execute("UPDATE artefact SET title=?, version=?, text_hash=?, updated_at=? WHERE ref=?",
                                (title, version, h, _now(), ref))
            else:
                self.db.execute("INSERT INTO artefact (ref,type,title,version,text_hash,created_at,updated_at)"
                                " VALUES (?,?,?,?,?,?,?)", (ref, type_, title, version, h, _now(), _now()))
            self.db.execute("INSERT INTO artefact_version (ref,version,full_text,created_at) VALUES (?,?,?,?)",
                            (ref, version, text, _now()))
            return version

    def clear_auto_links(self, source_ref: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM link WHERE source_ref=? AND origin='auto'", (source_ref,))

    def add_link(self, src: str, dst: str, kind: str, source_ref: str | None = None,
                 origin: str = "auto") -> None:
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO link (src,dst,kind,origin,source_ref,created_at)"
                            " VALUES (?,?,?,?,?,?)", (src, dst, kind, origin, source_ref, _now()))

    def remove_link(self, src: str, dst: str, kind: str) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM link WHERE src=? AND dst=? AND kind=?", (src, dst, kind))
            self.db.commit()
            return cur.rowcount > 0

    def commit(self) -> None:
        with self._lock:
            self.db.commit()

    def artefacts(self, type_: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM artefact", ()
        if type_:
            q, args = q + " WHERE type=?", (type_,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY ref", args).fetchall()]

    def artefact(self, ref: str, with_text: bool = False) -> dict | None:
        r = self.db.execute("SELECT * FROM artefact WHERE ref=?", (ref,)).fetchone()
        if not r:
            return None
        d = dict(r)
        if with_text:
            v = self.db.execute("SELECT full_text FROM artefact_version WHERE ref=? AND version=?",
                                (ref, d["version"])).fetchone()
            d["text"] = v["full_text"] if v else None
        d["links_out"] = [dict(l) for l in self.db.execute("SELECT * FROM link WHERE src=?", (ref,))]
        d["links_in"]  = [dict(l) for l in self.db.execute("SELECT * FROM link WHERE dst=?", (ref,))]
        return d

    def links(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM link ORDER BY src, kind, dst")]

    # ── Labels ────────────────────────────────────────────────────────────────
    def label(self, key: str) -> dict:
        """Node descriptor for an artefact ref or entity key."""
        if ":" in key:
            kind, eid = key.split(":", 1)
            e = self.entity(int(eid))
            if not e:
                return {"key": key, "kind": kind, "label": key, "layer": LAYER.get(kind), "missing": True}
            name = e["name"]
            return {"key": key, "kind": kind, "label": name, "layer": LAYER.get(kind),
                    "status": e["status"], "missing": False}
        a = self.db.execute("SELECT type, title, version FROM artefact WHERE ref=?", (key,)).fetchone()
        t = key.split("-")[0]
        if not a:
            return {"key": key, "kind": t, "label": key, "layer": LAYER.get(t), "missing": True}
        return {"key": key, "kind": a["type"], "label": key, "title": a["title"],
                "version": a["version"], "layer": LAYER.get(a["type"]), "missing": False}


# ── Store cache (one per project code) ────────────────────────────────────────
_STORES: dict[str, ConfigStore] = {}
_STORES_LOCK = threading.Lock()


def data_dir() -> str:
    return os.getenv("VOXRAIL_CM_DIR", "voxrail_cm_data")


def _safe_code(project_code: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", project_code) or "VR"


def store_exists(project_code: str) -> bool:
    code = _safe_code(project_code)
    return code in _STORES or os.path.exists(os.path.join(data_dir(), f"{code}.db"))


def get_store(project_code: str) -> ConfigStore:
    code = _safe_code(project_code)
    with _STORES_LOCK:
        if code not in _STORES:
            os.makedirs(data_dir(), exist_ok=True)
            _STORES[code] = ConfigStore(os.path.join(data_dir(), f"{code}.db"))
        return _STORES[code]


def reset_cache() -> None:
    with _STORES_LOCK:
        for s in _STORES.values():
            s.db.close()
        _STORES.clear()

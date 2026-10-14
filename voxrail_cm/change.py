# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
# =============================================================================
"""
voxrail_cm.change
=================
Change requests, change impact assessment and verification scope definition.

A change request (CR) names one or more changed items (targets):
  an artefact ref (UC-…, ED-…, SD-…, FM-…, HZ-…, TC-…, CC-…) or an entity
  ("state:Ready to Drive", "interface:Door Closed Loop", "actor:Driver").

Impact is derived across both linking layers, with the train state as hub:

  target ─► seeds: changed use cases (U0), train states (S0), interfaces / SW states (L0)
          ─► coupled use cases: other UCs in an S0 state or using an L0 interface
          ─► logical design of S0 states: interfaces / SW states / ED / SD realising them
          ─► dependents of every impacted UC: TC, FM, HZ, ED, SD, CC

Verification scope is derived from the impact: tests to re-run, tests missing,
FMEA / hazards to re-review, electrical / software design to re-check, and an
explicit out-of-scope list for the assessor.

Both come out in the matrix format of matrix.py, so the page, CSV and Excel
export work the same way.
"""

import json
import re
from collections import defaultdict
from datetime import datetime

from .store import ConfigStore, UC_STATE_LINKS, ekey

CR_STATUSES = ["Draft", "Open", "Assessed", "Approved", "Implemented", "Verified", "Closed", "Rejected"]
CHANGE_TYPES = ["modify", "add", "remove"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS change_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Open',
    origin TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cr_target (
    cr_id INTEGER NOT NULL, node_key TEXT NOT NULL, change_type TEXT NOT NULL DEFAULT 'modify',
    PRIMARY KEY (cr_id, node_key)
);
CREATE TABLE IF NOT EXISTS cr_assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cr_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    options TEXT NOT NULL, impact TEXT NOT NULL, scope TEXT NOT NULL, versions TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cr_history (
    cr_id INTEGER NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL
);
"""

# Impact categories, most to least direct
CATEGORIES = ["Changed", "Direct dependent", "Coupled via state", "Coupled via interface",
              "Coupled via actor", "Logical design", "Indirect dependent"]
DEPENDENT_LINKS = {"verifies": "TC", "analyses": "FM", "derived_from": "HZ", "realises": "ED",
                   "models_uc": "SD", "implements": "CC"}
HIGH_SEVERITY = ("catastrophic", "critical")
SEVERITY_RE = re.compile(r"^\*\*(?:Initial |Residual )?Severity\*\*\s*:\s*([A-Za-z]+)", re.MULTILINE | re.IGNORECASE)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_schema(store: ConfigStore) -> None:
    with store._lock:
        store.db.executescript(SCHEMA)
        store.db.commit()


# ── Target resolution ─────────────────────────────────────────────────────────
def resolve_target(store: ConfigStore, target: str) -> str:
    """'UC-MP-001' / 'state:Ready to Drive' / 'interface:3' -> node key. Raises ValueError."""
    t = target.strip()
    if ":" in t:
        kind, name = (x.strip() for x in t.split(":", 1))
        kind = {"sw": "sw_element", "software": "sw_element", "signal": "interface",
                "train state": "state"}.get(kind.lower(), kind.lower())
        if kind not in ("state", "interface", "actor", "sw_element"):
            raise ValueError(f"Unknown target kind '{kind}'")
        e = store.entity(int(name)) if name.isdigit() else store.find_entity(kind, name)
        if not e or e["kind"] != kind:
            raise ValueError(f"No {kind} named '{name}'")
        return ekey(kind, e["id"])
    ref = t.upper()
    if store.artefact(ref) or store.db.execute("SELECT 1 FROM link WHERE dst=?", (ref,)).fetchone():
        return ref
    raise ValueError(f"No artefact '{t}'")


# ── Change requests ───────────────────────────────────────────────────────────
def _code(store: ConfigStore, project_code: str) -> str:
    n = store.db.execute("SELECT COUNT(*) AS n FROM change_request").fetchone()["n"]
    return f"CR-{project_code}-{n + 1:03d}"


def create_cr(store: ConfigStore, project_code: str, title: str, description: str = "",
              targets: list[dict] | None = None, origin: str = "manual", status: str = "Open") -> dict:
    ensure_schema(store)
    with store._lock:
        code = _code(store, project_code)
        cur = store.db.execute(
            "INSERT INTO change_request (code,title,description,status,origin,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (code, title.strip(), description, status, origin, _now(), _now()))
        cr_id = cur.lastrowid
        store.db.execute("INSERT INTO cr_history VALUES (?,?,?)", (cr_id, _now(), f"Created ({origin})"))
        store.db.commit()
    for t in targets or []:
        add_target(store, code, t["target"], t.get("change_type", "modify"))
    return get_cr(store, code)


def _cr_row(store: ConfigStore, code: str):
    ensure_schema(store)
    r = store.db.execute("SELECT * FROM change_request WHERE code=?", (code.upper(),)).fetchone()
    if not r:
        raise KeyError(code)
    return r


def add_target(store: ConfigStore, code: str, target: str, change_type: str = "modify") -> dict:
    if change_type not in CHANGE_TYPES:
        raise ValueError(f"change_type must be one of {CHANGE_TYPES}")
    cr = _cr_row(store, code)
    key = resolve_target(store, target)
    with store._lock:
        store.db.execute("INSERT OR REPLACE INTO cr_target VALUES (?,?,?)", (cr["id"], key, change_type))
        store.db.execute("INSERT INTO cr_history VALUES (?,?,?)",
                         (cr["id"], _now(), f"Target {change_type}: {store.label(key)['label']}"))
        store.db.execute("UPDATE change_request SET updated_at=? WHERE id=?", (_now(), cr["id"]))
        store.db.commit()
    return get_cr(store, code)


def remove_target(store: ConfigStore, code: str, node_key: str) -> dict:
    cr = _cr_row(store, code)
    with store._lock:
        store.db.execute("DELETE FROM cr_target WHERE cr_id=? AND node_key=?", (cr["id"], node_key))
        store.db.commit()
    return get_cr(store, code)


def set_status(store: ConfigStore, code: str, status: str, note: str = "") -> dict:
    if status not in CR_STATUSES:
        raise ValueError(f"status must be one of {CR_STATUSES}")
    cr = _cr_row(store, code)
    with store._lock:
        store.db.execute("UPDATE change_request SET status=?, updated_at=? WHERE id=?", (status, _now(), cr["id"]))
        store.db.execute("INSERT INTO cr_history VALUES (?,?,?)",
                         (cr["id"], _now(), f"Status → {status}" + (f": {note}" if note else "")))
        store.db.commit()
    return get_cr(store, code)


def get_cr(store: ConfigStore, code: str) -> dict:
    cr = dict(_cr_row(store, code))
    cr["targets"] = [dict(store.label(t["node_key"]), change_type=t["change_type"])
                     for t in store.db.execute("SELECT * FROM cr_target WHERE cr_id=?", (cr["id"],))]
    cr["history"] = [dict(h) for h in store.db.execute(
        "SELECT at, event FROM cr_history WHERE cr_id=? ORDER BY rowid", (cr["id"],))]
    a = store.db.execute("SELECT id, created_at FROM cr_assessment WHERE cr_id=? ORDER BY id DESC LIMIT 1",
                         (cr["id"],)).fetchone()
    cr["last_assessment"] = dict(a) if a else None
    return cr


def list_crs(store: ConfigStore) -> list[dict]:
    ensure_schema(store)
    out = []
    for r in store.db.execute("SELECT code FROM change_request ORDER BY id"):
        c = get_cr(store, r["code"])
        c.pop("history")
        out.append(c)
    return out


# ── Impact engine ─────────────────────────────────────────────────────────────
class _G:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.out, self.inc = defaultdict(list), defaultdict(list)
        for l in store.links():
            self.out[l["src"]].append(l)
            self.inc[l["dst"]].append(l)

    def dsts(self, src, kinds):
        return [l["dst"] for l in self.out[src] if l["kind"] in kinds]

    def srcs(self, dst, kinds):
        return [l["src"] for l in self.inc[dst] if l["kind"] in kinds]


def _severity(store: ConfigStore, ref: str) -> str | None:
    a = store.artefact(ref, with_text=True)
    if not a or not a.get("text"):
        return None
    sev = [s.lower() for s in SEVERITY_RE.findall(a["text"])]
    for level in HIGH_SEVERITY:
        if level in sev:
            return level.capitalize()
    return sev[0].capitalize() if sev else None


def impact(store: ConfigStore, targets: list[str], include_actor_coupling: bool = False) -> dict:
    """Impact of changing `targets` (node keys). Returns {items: [...], seeds: {...}}."""
    g = _G(store)
    items: dict[str, dict] = {}

    def add(key: str, category: str, reason: str) -> None:
        cur = items.get(key)
        if cur and CATEGORIES.index(cur["category"]) <= CATEGORIES.index(category):
            if reason not in cur["reasons"]:
                cur["reasons"].append(reason)
            return
        lbl = store.label(key)
        items[key] = {"key": key, "label": lbl["label"], "kind": lbl["kind"], "title": lbl.get("title", ""),
                      "version": lbl.get("version", ""), "missing": lbl.get("missing", False),
                      "category": category, "reasons": [reason] + (cur["reasons"] if cur else [])}

    name = lambda k: store.label(k)["label"]
    U0, S0, L0, A0 = set(), set(), set(), set()

    # 1. Seeds
    for t in targets:
        add(t, "Changed", "named in the change request")
        kind = t.split(":")[0] if ":" in t else t.split("-")[0]
        if kind == "UC":
            U0.add(t)
            S0.update(g.dsts(t, UC_STATE_LINKS))
            L0.update(g.dsts(t, ("uses_interface",)))
            A0.update(g.dsts(t, ("performed_by",)))
        elif kind == "state":
            S0.add(t)
        elif kind in ("interface", "sw_element"):
            L0.add(t)
            for s in g.srcs(t, ("realised_by_electrical", "realised_by_software")):
                S0.add(s)
                add(s, "Direct dependent", f"realised by {name(t)}")
            for a in g.srcs(t, ("defines",)):
                add(a, "Direct dependent", f"defines {name(t)}")
        elif kind == "actor":
            A0.add(t)
        elif kind == "ED":
            L0.update(g.dsts(t, ("defines",)))
            S0.update(g.dsts(t, ("serves_state",)))
            U0.update(g.dsts(t, ("realises",)))
        elif kind == "SD":
            sw_states = {l["dst"] for ls in g.out.values() for l in ls if l["kind"] == "realised_by_software"}
            L0.update(k for k in g.dsts(t, ("defines",)) if k in sw_states)
            S0.update(g.dsts(t, ("models_state",)))
            U0.update(g.dsts(t, ("models_uc",)))
            for cc in g.srcs(t, ("implements",)):
                add(cc, "Direct dependent", f"implements {t}")
        elif kind == "FM":
            for hz in g.srcs(t, ("derived_from",)):
                add(hz, "Direct dependent", f"derived from {t}")
            U0.update(g.dsts(t, ("analyses",)))
        elif kind in ("HZ", "TC", "CC"):
            U0.update(u for u in g.dsts(t, ("derived_from", "verifies", "implements")) if u.startswith("UC-"))

    # Use cases owning a changed artefact are impacted directly (their own docs may change)
    for u in U0:
        if u not in targets:
            add(u, "Direct dependent", "owns a changed artefact")

    # 2. States reached through changed logic, and their use cases
    for s in S0:
        if s not in items:
            add(s, "Coupled via state", "train state of a changed item")
        for u in g.srcs(s, UC_STATE_LINKS):
            if u not in U0:
                add(u, "Coupled via state", f"occurs in train state {name(s)}")
    for l in L0:
        for u in g.srcs(l, ("uses_interface",)):
            if u not in U0:
                add(u, "Coupled via interface", f"uses {name(l)}")
    if include_actor_coupling or any(t.startswith("actor:") for t in targets):
        for a in A0:
            for u in g.srcs(a, ("performed_by",)):
                if u not in U0:
                    add(u, "Coupled via actor", f"performed by {name(a)}")

    # 3. Logical design of impacted states (to re-check it still supports the change)
    for s in S0:
        for k in g.dsts(s, ("realised_by_electrical", "realised_by_software")):
            add(k, "Logical design", f"realises train state {name(s)}")
        for a in g.srcs(s, ("serves_state", "models_state")):
            add(a, "Logical design", f"designs train state {name(s)}")

    # 4. Dependents of every impacted use case
    ucs = [k for k in list(items) if k.startswith("UC-")]
    for u in ucs:
        direct = items[u]["category"] in ("Changed", "Direct dependent")
        for kind, t in DEPENDENT_LINKS.items():
            for a in g.srcs(u, (kind,)):
                if a.startswith(t + "-"):
                    add(a, "Direct dependent" if direct else "Indirect dependent", f"{kind.replace('_', ' ')} {u}")
        for fm in [a for a in g.srcs(u, ("analyses",))]:
            for hz in g.srcs(fm, ("derived_from",)):
                add(hz, "Direct dependent" if direct else "Indirect dependent", f"derived from {fm}")

    # 5. Safety flags
    for it in items.values():
        it["severity"] = None
        it["safety"] = False
        if it["kind"] in ("FM", "HZ"):
            it["severity"] = _severity(store, it["key"])
            it["safety"] = it["kind"] == "HZ" or (it["severity"] or "").lower() in HIGH_SEVERITY
    for u in [k for k in items if k.startswith("UC-")]:
        safety_deps = [a for a in g.srcs(u, ("analyses", "derived_from"))
                       if a in items and items[a]["safety"]]
        if safety_deps:
            items[u]["safety"] = True
            items[u]["reasons"].append("safety-relevant: " + ", ".join(sorted(safety_deps)))

    ordered = sorted(items.values(), key=lambda i: (CATEGORIES.index(i["category"]), str(i["kind"]), i["label"]))
    for i in ordered:
        i["reason"] = "; ".join(i.pop("reasons"))
    return {"targets": [store.label(t) for t in targets], "items": ordered,
            "counts": {c: sum(1 for i in ordered if i["category"] == c) for c in CATEGORIES},
            "safety_items": [i["key"] for i in ordered if i["safety"]],
            "include_actor_coupling": include_actor_coupling}


# ── Verification scope ────────────────────────────────────────────────────────
SCOPE_COLS = [("rerun_tests", "Re-run tests"), ("new_test", "New test needed"),
              ("fmea", "Re-review FMEA"), ("hazard", "Re-review hazard (safety sign-off)"),
              ("electrical", "Re-check electrical design"), ("software", "Re-check software (SD / CC, static analysis)")]


def verification_scope(store: ConfigStore, imp: dict) -> dict:
    g = _G(store)
    by_key = {i["key"]: i for i in imp["items"]}
    rows = []
    for u in [i for i in imp["items"] if i["kind"] == "UC"]:
        k = u["key"]
        deps = defaultdict(list)
        for kind, t in DEPENDENT_LINKS.items():
            deps[t] += [a for a in g.srcs(k, (kind,)) if a.startswith(t + "-")]
        hz = set(deps["HZ"]) | {h for fm in deps["FM"] for h in g.srcs(fm, ("derived_from",))}
        cells = {}
        if deps["TC"]:
            cells["rerun_tests"] = ", ".join(sorted(deps["TC"]))
        else:
            cells["new_test"] = "Yes, no test verifies this use case"
        if deps["FM"]:
            cells["fmea"] = ", ".join(sorted(deps["FM"]))
        if hz:
            cells["hazard"] = ", ".join(sorted(hz))
        if deps["ED"]:
            cells["electrical"] = ", ".join(sorted(deps["ED"]))
        sw = deps["SD"] + deps["CC"]
        if sw:
            cells["software"] = ", ".join(sorted(sw))
        rows.append({"key": k, "label": k,
                     "fields": {"Title": u["title"], "Impact": u["category"], "Why": u["reason"],
                                "Safety": "Yes" if u["safety"] else ""},
                     "cells": cells, "gap": "new_test" in cells,
                     "gap_cols": ["new_test"] if "new_test" in cells else [],
                     "gap_note": "new test needed" if "new_test" in cells else ""})

    # Logical items not tied to an impacted UC still need a design check
    for i in imp["items"]:
        if i["kind"] in ("interface", "sw_element", "ED", "SD", "CC") and i["category"] in ("Changed", "Direct dependent", "Logical design"):
            col = "electrical" if i["kind"] in ("interface", "ED") else "software"
            rows.append({"key": i["key"], "label": i["label"],
                         "fields": {"Title": i["title"] or i["kind"], "Impact": i["category"], "Why": i["reason"],
                                    "Safety": ""},
                         "cells": {col: "Check"}, "gap": False, "gap_cols": [], "gap_note": ""})

    impacted = set(by_key)
    all_ucs = sorted({a["ref"] for a in store.artefacts("UC")})
    out_of_scope = [{"key": u, "label": u, "title": store.label(u).get("title", ""),
                     "justification": "no shared train state, interface or dependency with the changed items"}
                    for u in all_ucs if u not in impacted]
    tests_to_rerun = sorted({t for r in rows for t in r["cells"].get("rerun_tests", "").split(", ") if t})
    return {
        "name": "verification_scope", "title": "Verification scope",
        "legend": "One row per impacted use case and per logical design item. Cells name the artefacts to act on.",
        "row_fields": ["Title", "Impact", "Why", "Safety"],
        "columns": [{"key": k, "label": l, "group": "Verification activity"} for k, l in SCOPE_COLS],
        "rows": rows,
        "out_of_scope": out_of_scope,
        "summary": {
            "use_cases_in_scope": sum(1 for r in rows if r["key"].startswith("UC-")),
            "use_cases_out_of_scope": len(out_of_scope),
            "tests_to_rerun": tests_to_rerun,
            "new_tests_needed": [r["key"] for r in rows if "new_test" in r["cells"]],
            "safety_reviews": sorted({h for r in rows for c in ("fmea", "hazard")
                                      for h in r["cells"].get(c, "").split(", ") if h}),
        },
        "totals": {"rows": len(rows), "columns": len(SCOPE_COLS), "gaps": sum(1 for r in rows if r["gap"])},
    }


def impact_matrix(imp: dict) -> dict:
    """Impact list in matrix format (one row per impacted item, one column per category)."""
    rows = []
    for i in imp["items"]:
        rows.append({"key": i["key"], "label": i["label"],
                     "fields": {"Type": i["kind"], "Title": i["title"], "Version": i["version"],
                                "Severity": i["severity"] or "", "Safety": "Yes" if i["safety"] else ""},
                     "cells": {i["category"]: i["reason"]},
                     "gap": i["missing"], "gap_cols": [],
                     "gap_note": "referenced but not generated" if i["missing"] else ""})
    return {"name": "impact", "title": "Change impact",
            "legend": "Each item sits in the column of its most direct impact category, with the reason.",
            "row_fields": ["Type", "Title", "Version", "Severity", "Safety"],
            "columns": [{"key": c, "label": c, "group": "Impact category"} for c in CATEGORIES],
            "rows": rows,
            "totals": {"rows": len(rows), "columns": len(CATEGORIES),
                       "gaps": sum(1 for r in rows if r["gap"])}}


def out_of_scope_matrix(scope: dict) -> dict:
    return {"name": "out_of_scope", "title": "Out of scope (justified)", "legend": "Use cases not reached by the impact.",
            "row_fields": ["Title", "Justification"], "columns": [],
            "rows": [{"key": o["key"], "label": o["label"],
                      "fields": {"Title": o["title"], "Justification": o["justification"]},
                      "cells": {}, "gap": False, "gap_cols": [], "gap_note": ""} for o in scope["out_of_scope"]]}


def cr_targets(store: ConfigStore, code: str) -> list[str]:
    cr = _cr_row(store, code)
    return [t["node_key"] for t in store.db.execute("SELECT node_key FROM cr_target WHERE cr_id=?", (cr["id"],))]


def assess(store: ConfigStore, code: str, include_actor_coupling: bool = False) -> dict:
    """Run impact + scope for a CR and freeze the result (with artefact versions) for audit."""
    cr = _cr_row(store, code)
    targets = cr_targets(store, code)
    if not targets:
        raise ValueError("Add at least one target to the change request first")
    imp = impact(store, targets, include_actor_coupling)
    scope = verification_scope(store, imp)
    versions = {i["key"]: i["version"] for i in imp["items"] if i["version"] != ""}
    with store._lock:
        store.db.execute("INSERT INTO cr_assessment (cr_id, created_at, options, impact, scope, versions)"
                         " VALUES (?,?,?,?,?,?)",
                         (cr["id"], _now(), json.dumps({"include_actor_coupling": include_actor_coupling}),
                          json.dumps(imp), json.dumps(scope), json.dumps(versions)))
        if cr["status"] in ("Draft", "Open"):
            store.db.execute("UPDATE change_request SET status='Assessed', updated_at=? WHERE id=?", (_now(), cr["id"]))
        store.db.execute("INSERT INTO cr_history VALUES (?,?,?)",
                         (cr["id"], _now(), f"Assessed: {len(imp['items'])} impacted items, "
                                            f"{len(scope['summary']['tests_to_rerun'])} tests to re-run"))
        store.db.commit()
    return {"cr": get_cr(store, code), "impact": imp, "scope": scope}


def last_assessment(store: ConfigStore, code: str) -> dict | None:
    cr = _cr_row(store, code)
    a = store.db.execute("SELECT * FROM cr_assessment WHERE cr_id=? ORDER BY id DESC LIMIT 1", (cr["id"],)).fetchone()
    if not a:
        return None
    current = {r["ref"]: r["version"] for r in store.artefacts()}
    versions = json.loads(a["versions"])
    changed_since = sorted(k for k, v in versions.items() if k in current and current[k] != v)
    return {"created_at": a["created_at"], "options": json.loads(a["options"]),
            "impact": json.loads(a["impact"]), "scope": json.loads(a["scope"]),
            "artefacts_changed_since": changed_since}


# ── Variation orders → draft CRs ──────────────────────────────────────────────
VO_RE = re.compile(r"⚠️\s*VARIATION ORDER(?: DETECTED)?\s*:?\s*(.+)")


def capture_variation_order(store: ConfigStore, project_code: str, text: str, refs: list[str]) -> dict | None:
    """When VoxRail flags a variation order, open a Draft CR targeting the artefacts in that response."""
    m = VO_RE.search(text)
    if not m:
        return None
    title = m.group(1).strip()[:200] or "Variation order"
    cr = create_cr(store, project_code, title, description="Raised automatically from a VoxRail variation order flag.",
                   origin="variation_order", status="Draft")
    for r in refs:
        try:
            add_target(store, cr["code"], r, "add" if (store.artefact(r) or {}).get("version") == 1 else "modify")
        except ValueError:
            pass
    return get_cr(store, cr["code"])

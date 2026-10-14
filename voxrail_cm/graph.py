# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
# =============================================================================
"""
voxrail_cm.graph
================
Read-side views over the configuration graph: full graph per layer,
coupled use cases, train state view, Mermaid rendering, prompt context.
"""

import re
from collections import defaultdict

from .store import ConfigStore, LAYER, UC_STATE_LINKS, ekey


def graph(store: ConfigStore, layer: str = "both") -> dict:
    """Nodes and edges. layer: '1', '2' or 'both'. Train states appear on every layer."""
    want = {"1": {1, 0}, "2": {2, 0}}.get(str(layer), {0, 1, 2})
    edges = [l for l in store.links()]
    keys = {a["ref"] for a in store.artefacts()} | {e["key"] for e in store.entities()}
    for l in edges:
        keys.update((l["src"], l["dst"]))
    nodes = {k: store.label(k) for k in keys}
    nodes = {k: n for k, n in nodes.items() if n.get("layer") in want}
    edges = [{"src": l["src"], "dst": l["dst"], "kind": l["kind"], "origin": l["origin"]}
             for l in edges if l["src"] in nodes and l["dst"] in nodes]
    return {"layer": str(layer), "nodes": sorted(nodes.values(), key=lambda n: (str(n["kind"]), n["label"])),
            "edges": edges}


def _uc_hubs(store: ConfigStore, uc: str) -> dict[str, str]:
    """Shared-able neighbours of a UC: actor/state/interface keys -> link kind."""
    out = {}
    for l in store.db.execute("SELECT dst, kind FROM link WHERE src=?", (uc,)):
        if l["dst"].split(":")[0] in ("actor", "state", "interface"):
            out[l["dst"]] = l["kind"]
    return out


def coupled_use_cases(store: ConfigStore, uc: str) -> dict:
    """Use cases coupled to `uc` through a shared actor, train state or interface."""
    hubs = _uc_hubs(store, uc)
    coupled: dict[str, list[dict]] = defaultdict(list)
    for hub, own_kind in hubs.items():
        for l in store.db.execute("SELECT src, kind FROM link WHERE dst=? AND src LIKE 'UC-%'", (hub,)):
            if l["src"] == uc:
                continue
            coupled[l["src"]].append({"via": store.label(hub), "this_uc": own_kind, "other_uc": l["kind"]})
    return {
        "uc": store.label(uc),
        "hubs": [dict(store.label(h), link=k) for h, k in hubs.items()],
        "coupled": [{"uc": store.label(o), "through": via} for o, via in sorted(coupled.items())],
    }


def state_view(store: ConfigStore, state_key: str) -> dict:
    """Everything above (layer 1) and below (layer 2) one train state."""
    above, below, other = defaultdict(list), defaultdict(list), defaultdict(list)
    for l in store.db.execute("SELECT src, kind FROM link WHERE dst=?", (state_key,)):
        target = above if l["kind"] in UC_STATE_LINKS or l["kind"] == "related_state" else other
        target[l["kind"]].append(store.label(l["src"]))
    for l in store.db.execute("SELECT dst, kind FROM link WHERE src=?", (state_key,)):
        below[l["kind"]].append(store.label(l["dst"]))

    # States coupled at logical level through a shared interface / sw element
    logical = [l["dst"] for l in store.db.execute(
        "SELECT dst FROM link WHERE src=? AND kind IN ('realised_by_electrical','realised_by_software')",
        (state_key,))]
    coupled = defaultdict(list)
    for d in logical:
        for l in store.db.execute(
                "SELECT src FROM link WHERE dst=? AND kind IN ('realised_by_electrical','realised_by_software')",
                (d,)):
            if l["src"] != state_key:
                coupled[l["src"]].append(store.label(d))
    return {
        "state": store.label(state_key),
        "use_case_level": dict(above),
        "logical_level": dict(below),
        "designed_in": dict(other),                   # ED serves_state, SD models_state
        "coupled_states": [{"state": store.label(s), "through": v} for s, v in sorted(coupled.items())],
        "use_case_count": len({n["key"] for k in UC_STATE_LINKS for n in above.get(k, [])}),
    }


def states_overview(store: ConfigStore) -> list[dict]:
    out = []
    for e in store.entities("state"):
        v = state_view(store, e["key"])
        out.append({
            "key": e["key"], "name": e["name"], "status": e["status"], "aliases": e["aliases"],
            "use_cases": sorted({n["key"] for k in UC_STATE_LINKS for n in v["use_case_level"].get(k, [])}),
            "interfaces": [n["label"] for n in v["logical_level"].get("realised_by_electrical", [])],
            "sw_elements": [n["label"] for n in v["logical_level"].get("realised_by_software", [])],
        })
    return out


def gaps(store: ConfigStore) -> dict:
    """Phase 1 consistency checks: missing links and dangling references."""
    ucs = [a["ref"] for a in store.artefacts("UC")]
    def has_in(ref, kind):
        return store.db.execute("SELECT 1 FROM link WHERE dst=? AND kind=?", (ref, kind)).fetchone()
    def has_out(ref, kinds):
        q = "SELECT 1 FROM link WHERE src=? AND kind IN (%s)" % ",".join("?" * len(kinds))
        return store.db.execute(q, (ref, *kinds)).fetchone()
    known = {a["ref"] for a in store.artefacts()}
    dangling = [l for l in store.links() if ":" not in l["dst"] and l["dst"] not in known]
    states = store.entities("state")
    return {
        "uc_without_state":   [u for u in ucs if not has_out(u, UC_STATE_LINKS)],
        "uc_without_test":    [u for u in ucs if not has_in(u, "verifies")],
        "state_without_electrical": [s["name"] for s in states if not has_out(s["key"], ("realised_by_electrical",))],
        "state_without_software":   [s["name"] for s in states if not has_out(s["key"], ("realised_by_software",))],
        "dangling_refs": [{"from": l["src"], "to": l["dst"], "kind": l["kind"]} for l in dangling],
        "open_suggestions": len(store.suggestions()),
    }


def summary(store: ConfigStore) -> dict:
    counts = defaultdict(int)
    for a in store.artefacts():
        counts[a["type"]] += 1
    ents = defaultdict(int)
    for e in store.entities():
        ents[e["kind"]] += 1
    return {"artefacts": dict(counts), "entities": dict(ents), "links": len(store.links()),
            "open_suggestions": len(store.suggestions())}


# ── Mermaid ───────────────────────────────────────────────────────────────────
def _mid(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


def _mlabel(s: str) -> str:
    return s.replace('"', "'")[:60]


SHAPES = {"actor": ('(["', '"])'), "state": ('{{"', '"}}'), "interface": ('[/"', '"/]'),
          "sw_element": ('("', '")')}


def mermaid(store: ConfigStore, layer: str = "both") -> str:
    g = graph(store, layer)
    lines = ["flowchart LR"]
    groups = {1: [], 0: [], 2: []}
    for n in g["nodes"]:
        groups.setdefault(n["layer"], []).append(n)
    titles = {1: "Layer 1 - Use case level", 0: "Train states", 2: "Layer 2 - Logical level"}
    for lay in (1, 0, 2):
        if not groups.get(lay):
            continue
        lines.append(f'  subgraph L{lay}["{titles[lay]}"]')
        for n in groups[lay]:
            text = n["label"] if ":" in n["key"] else f'{n["key"]}' + (f'<br/>{n["title"][:40]}' if n.get("title") else "")
            o, c = SHAPES.get(n["kind"], ('["', '"]'))
            lines.append(f"    {_mid(n['key'])}{o}{_mlabel(text)}{c}")
        lines.append("  end")
    for e in g["edges"]:
        arrow = "-.->" if e["origin"] == "auto" and e["kind"].startswith("realised") else "-->"
        lines.append(f"  {_mid(e['src'])} {arrow}|{e['kind']}| {_mid(e['dst'])}")
    for n in g["nodes"]:
        if n.get("missing"):
            lines.append(f"  style {_mid(n['key'])} stroke-dasharray: 4 3")
    return "\n".join(lines)


# ── Prompt context (kept short: free-tier TPM is tight) ───────────────────────
def prompt_context(store: ConfigStore, max_items: int = 25) -> str:
    states = [e["name"] for e in store.entities("state")][:max_items]
    ifaces = [e["name"] for e in store.entities("interface")][:max_items]
    actors = [e["name"] for e in store.entities("actor")][:max_items]
    if not (states or ifaces or actors):
        return ""
    out = ["\n=== CONFIGURATION CATALOGUE (reuse these exact names) ==="]
    if states:
        out.append("Train States: " + "; ".join(states))
    if actors:
        out.append("Actors: " + "; ".join(actors))
    if ifaces:
        out.append("Logical Interfaces: " + "; ".join(ifaces))
    out.append("Introduce a new name only when none of these fits.\n=== END CATALOGUE ===\n")
    return "\n".join(out)

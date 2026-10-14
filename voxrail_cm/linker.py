# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
# =============================================================================
"""
voxrail_cm.linker
=================
Turns parsed artefacts into graph links (both layers) as users generate them.
"""

from .parser import ParsedArtefact, parse_response
from .store import ConfigStore, ekey


def ingest_response(store: ConfigStore, text: str) -> list[dict]:
    """Parse one LLM response and link every artefact in it. Returns a summary per artefact."""
    results = []
    for art in parse_response(text):
        results.append(ingest(store, art))
    store.commit()
    return results


def ingest(store: ConfigStore, art: ParsedArtefact) -> dict:
    version = store.upsert_artefact(art.type, art.ref, art.title, art.text)
    store.clear_auto_links(art.ref)
    ref = art.ref

    def state(name: str) -> str:
        return ekey("state", store.resolve("state", name, ref))

    def link(src: str, dst: str, kind: str) -> None:
        store.add_link(src, dst, kind, source_ref=ref)

    # ── Layer 1: use case level ───────────────────────────────────────────────
    for a in art.actors:
        link(ref, ekey("actor", store.resolve("actor", a, ref)), "performed_by")
    if art.type == "UC":
        for s in art.states:
            link(ref, state(s), "valid_in")
        for s in art.from_states:
            link(ref, state(s), "transitions_from")
        for s in art.to_states:
            link(ref, state(s), "transitions_to")
        for i in art.interfaces:
            iid = store.resolve("interface", i["name"], ref, attrs={"kind": i["kind"]})
            link(ref, ekey("interface", iid), "uses_interface")

    for kind, targets in art.refs.items():
        for t in targets:
            if t != ref:
                link(ref, t, kind)

    if art.type == "FM":
        for s in art.states:
            link(ref, state(s), "related_state")

    # ── Layer 2: logical level ────────────────────────────────────────────────
    if art.type == "ED":
        served = [state(s) for s in art.states]
        for i in art.interfaces:
            ikey = ekey("interface", store.resolve("interface", i["name"], ref, attrs={"kind": i["kind"]}))
            link(ref, ikey, "defines")
            for skey in served:
                link(skey, ikey, "realised_by_electrical")
        for skey in served:
            link(ref, skey, "serves_state")

    if art.type == "SD":
        modelled = [state(s) for s in art.states]
        for e in art.sw_elements:
            # An SD state named like a catalogue train state models that train state
            if e["kind"] == "state":
                hit = store.find_entity("state", e["name"])
                if hit:
                    k = ekey("state", hit["id"])
                    if k not in modelled:
                        modelled.append(k)
            eid = store.resolve("sw_element", e["name"], ref, attrs={"kind": e["kind"], "sd": ref}, scope=ref)
            skey = ekey("sw_element", eid)
            link(ref, skey, "defines")
            if e["kind"] == "state":             # events stay attached to the SD only
                for m in modelled:
                    link(m, skey, "realised_by_software")
        for m in modelled:
            link(ref, m, "models_state")

    return {"ref": ref, "type": art.type, "version": version}

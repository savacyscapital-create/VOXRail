# VoxRail Configuration Management Plugin — Feasibility & Design

Status: **Phase 1 built** (2026-10-14) in `voxrail_cm/`, `main.py`, `app.py`; not yet pushed to GitHub. Phases 2–3 proposed. Based on `app.py` / `main.py` v3.0 in the project files (same code as GitHub `savacyscapital-create/VOXRail`).

## 1. Verdict

Feasible, and VoxRail is already halfway there. Every artefact is generated from a fixed template with machine-readable fields, and most already carry a reference back to a use case:

| Artefact | Existing link field (main.py) |
|---|---|
| TC  | `**Use Case Ref**` |
| FM  | `**Related UC**`, `**Related State**` |
| HZ  | `**Related UC**`, `**Related FMEA Ref**` |
| ED  | `**Related UC**` (+ signals/buses inside the Mermaid flowchart) |
| CC  | `**Related UC**`, `**Related State Diagram**` |
| UC  | `**Primary Actors**` (no connector/interface field yet) |

What is missing today, and what the plugin has to add:

1. **No persistent model.** Session state lives in memory (`SESSION_STORE`) and only the *last* artefact of each type is kept. `voxrail_artefacts.json` is a flat append log with the raw text. Nothing survives a restart as structured data.
2. **No connector field on use cases.** Connectors/interfaces only appear inside ED diagrams.
3. **State diagrams have no ref.** `SD-…` is synthesised from the UC counter, so it can't be linked reliably.
4. **No plugin hook.** `update_from_response()` is the single mutation point and is the natural place to add one.
5. `log_artefact()` uses `elif`, so a response with two artefacts only logs one.

## 2. How it plugs in

```
chat() ──► session.update_from_response(text)
                │
                └─► plugin_registry.emit("artefact_created", session, type, ref, text)
                          │
                          └─► voxrail_cm.on_artefact()  → parse fields → upsert graph (SQLite)
app.py  ──► app.include_router(voxrail_cm.router, prefix="/cm")
```

- New package `voxrail_cm/` (parser, graph store, impact engine, FastAPI router).
- A tiny registry in `main.py` (`register_plugin`, `emit`) so CM can be switched off with an env var and other plugins can follow the same pattern later.
- Core VoxRail behaviour is unchanged when the plugin is disabled.

## 3. Two-layer model (agreed with Bobby, 2026-10-14)

"Connector" means a **logical connection**, either through a **train state** or through an **electrical/software interface**. Linking happens on two layers:

```
LAYER 1 — USE CASE LEVEL
   UC-001 ─┐                ┌─ UC-004
   UC-002 ─┼── Train State ─┤            (UC valid in 1 or many states;
   UC-003 ─┘   "Ready to    └─ UC-005     UCs sharing a state or an actor
               Drive"                      are coupled)
                  │ realised by
LAYER 2 — LOGICAL LEVEL
        ┌─────────┴──────────┐
  Electrical logic        Software logic
  (ED: signals, buses,    (SD states/transitions,
   control units)          CC state machine)
        └── shared interface (e.g. "Door Closed & Locked" loop, MVB) ──┘
```

- **Train State** is the hub. A **project state catalogue** (e.g. Stabled, Parked, Ready to Drive, Coupled, Degraded…) is created once and every artefact refers to it by name, so "Ready to Drive" in a UC and in an SD are the same object.
- A use case is linked to **one or more** states (its precondition/operating states, plus the Previous/New state it transitions between).
- Each state is **designed by** electrical logic (ED interfaces/signals) and software logic (SD transitions, CC code).
- Interfaces/signals can serve several states, which is how logical-level changes ripple upward.

## 4. Data model (SQLite, one DB per project code)

**Nodes**
- `artefact(ref PK, type UC|TC|SD|FM|HZ|ED|CC, title, version, text_hash, status Draft|Baselined|Superseded)` + `artefact_version(ref, version, full_text, created_at)`
- `actor(id, canonical_name)` + `actor_alias`
- `train_state(id, canonical_name, description)` + `state_alias` — the project state catalogue
- `interface(id, canonical_name, kind CAN|MVB|Ethernet|Discrete|Analogue, voltage_domain)` + `interface_alias` — logical signals/buses from ED
- `sw_element(id, sd_ref, kind state|transition|event, name)` — from SD / CC

**Layer 1 edges (use case level)**
- `UC —performed_by→ Actor`
- `UC —valid_in→ TrainState` (1..n) · `UC —transitions_from→ TrainState` · `UC —transitions_to→ TrainState`
- `TC —verifies→ UC`, `FM —analyses→ UC`, `HZ —derived_from→ FM|UC`

**Layer 2 edges (logical level)**
- `TrainState —realised_by_electrical→ Interface` (via ED)
- `TrainState —realised_by_software→ sw_element` (via SD / CC)
- `ED —defines→ Interface`, `SD —models→ TrainState`, `CC —implements→ SD`
- `FM —related_state→ TrainState` (FM already has a Related State field)

**Derived couplings (computed, never stored)**: UC↔UC when they share a state or actor (layer 1); TrainState↔TrainState when they share an interface or sw_element (layer 2).

All edges carry `origin auto|manual|confirmed`. Configuration control adds `baseline`, `baseline_item`, and `change_request` (can be pre-filled from VoxRail's existing `⚠️ VARIATION ORDER` detection).

## 5. Building links as users generate

1. **Template additions**:
   - UC: `**Applicable Train States**:` (list; one or many), alongside the existing Previous/New state fields, and `**Logical Interfaces**:` (optional).
   - SD: `**State Diagram Ref**: SD-…` and `**Related UC**:`.
   - ED: `**Train States Served**:`.
   - The system prompt is given the current state catalogue so the LLM reuses existing state names instead of inventing new ones.
2. **Deterministic parse** of these fields plus SD state names and ED arrow labels (signal names). No extra LLM call.
3. **Name matching**: unknown names are proposed as new catalogue entries or as aliases ("Ready-to-Drive" vs "Ready to Drive"), confirmed by the engineer. Only confirmed / exact links feed formal reports.

## 6. Change impact assessment & verification scope

The change can start on either layer:

| Change at | Traversal |
|---|---|
| **Use case** | UC → its states → (layer 1) other UCs in those states; (layer 2) the electrical/software logic of those states, to check it still supports the change |
| **Train state** | state → all UCs valid in / transitioning through it → their TC/FM/HZ; and down to its ED/SD/CC |
| **Interface / signal** (electrical) | interface → every state it realises → every UC in those states → their TC/FM/HZ |
| **Software element** (SD/CC) | sw_element → states → UCs → TC/FM/HZ |

Each impacted item is classed **Direct / via State / via Interface / Indirect**, with a **safety flag** when a Critical/Catastrophic FM or HZ is on the path. A UC valid in only one state is impacted by fewer logical changes than one spanning many; the report shows that state count to help judge it.

**Verification scope** from the impacted set:
- **Re-run**: TCs verifying impacted UCs, filtered to the impacted states where the TC is state-specific.
- **Gaps**: an impacted UC with no TC, or an impacted UC/state combination with no TC → new test needed.
- **Re-review**: impacted FM/HZ (safety sign-off), ED (interface check), SD/CC (re-run static analysis).
- **Out of scope (justified)**: everything in the baseline not reached, listed explicitly for the assessor.
- **Baseline diff** feeds the same engine for release-level scope.

Exports: UC × State matrix, State × Interface matrix, and the verification matrix (UC × TC × status) as CSV/XLSX.

## 6a. Matrix views (built 2026-10-14)

The graph diagram is only for small, focused views; matrices are the main tool on a real project. Page: `/cm/matrices`. JSON/CSV: `/cm/matrix/{name}`. Excel (all four sheets, AutoFilter, frozen headers, gaps highlighted): `/cm/matrix.xlsx`.

| Matrix | Rows × Columns | Cells |
|---|---|---|
| `uc_state` | Use case × Train state | V valid in · F from · T to |
| `state_logic` | Train state × Interface / SW state | E electrical · S software; gap if a state has no electrical or software logic |
| `uc_trace` | Use case × TC / FM / HZ / ED / SD / CC | linked refs; gap if no test, no state, or referenced but never generated |
| `uc_uc` | Use case × Use case | shared S:state · A:actor · I:interface |

Filters (same on page, CSV and Excel): `row`, `col`, `actor`, `state`, `gaps_only`, `hide_empty`. Phase 2 impact / verification scope reports will reuse this matrix format.

## 7. API / chat surface (`/cm`)

- `GET /cm/graph?layer=1|2|both` · `GET /cm/artefact/{ref}` · `GET /cm/uc/{ref}/coupled`
- `GET /cm/states` · `POST /cm/states` (state catalogue) · `GET /cm/states/{id}` (UCs above, logic below)
- `POST /cm/links/confirm` · `POST /cm/aliases`
- `POST /cm/baselines` · `GET /cm/baselines/{a}/diff/{b}`
- `POST /cm/change-requests` · `GET /cm/change-requests/{id}/impact` · `GET /cm/change-requests/{id}/verification-scope`
- Chat intents: "what does changing X impact?", "what do I need to re-test for CR-3?" answered from the graph, not the LLM's memory.

## 8. Phasing

1. **Phase 1** — plugin hook, SQLite store, train state catalogue, template additions, parser, layer 1 + layer 2 auto-linking, `/cm/graph` + state view.
2. **Phase 2** — change requests, impact engine, verification scope, exports.
3. **Phase 3** — baselines & diffs, alias confirmation UI, chat intents, graph visualisation in the web UI.

## 9. Open questions

- **State catalogue source**: default is that the engineer seeds it (or VoxRail proposes one from the first few use cases for approval). An existing train state list from the customer spec can be imported instead.
- **Persistence**: SQLite per project is the proposed default.

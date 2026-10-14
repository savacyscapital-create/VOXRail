# VoxRail
### AI-Powered Railway Systems Engineering Assistant

> **Developer:** [Savacys Capital](https://github.com/SavacysCapital)  
> **Status:** Active Development  
> **Open Source:** [AGPL-3.0](./LICENSE) (Free for Personal & Open Research)  
> **Commercial:** [Enterprise License Available](./Commercial-Licensing.md)

---

VoxRail is a domain-specific AI assistant purpose-built for railway systems engineering. It transforms natural language requirements into structured engineering artefacts while maintaining **100% traceability** across the lifecycle.

## 🚀 Core Capabilities
* **Artefact Generation:** Automated creation of Concept of Operations (ConOps), System Requirements, and Interface Specifications.
* **Stateful Continuity:** Maintains a single "engineering state" across sessions, ensuring data consistency.
* **Regulatory Alignment:** Designed to assist with standards like EN 50126/128/129.

## ⚖️ Licensing
VoxRail is dual-licensed to support both the open-source community and professional engineering firms:
* **Community Edition:** Free under the GNU AGPLv3. Ideal for students, hobbyists, and open-source contributors.
* **Commercial Edition:** Required for use within a commercial entity. Provides proprietary integration rights and removes AGPL "share-alike" requirements. [Learn more about Commercial Licensing](./Commercial-Licensing.md).

---

## Artefact Chain

```
Natural Language Input
        ↓
   Use Case (UC)
        ↓
Test Specification (TC)  ←→  State Transition Diagram (SD)
        ↓
     FMEA (FM)
        ↓
  Hazard Log (HZ)
```

All artefacts are cross-referenced with auto-incrementing project codes
(e.g. UC-EL2-001 → TC-EL2-001 → FM-EL2-001 → HZ-EL2-001).

---

## Key Features

- **Use Case generation** — strict railway engineering template with state transitions, degraded modes, trigger types, and feedback classification
- **Test Specification generation** — tabular format with car-type applicability columns (Cab Car / Motor Car / Trailer Car)
- **State Transition Diagrams** — Mermaid v11 stateDiagram-v2 with live browser rendering and 2× retina PNG export
- **FMEA** — per-failure-mode analysis with Severity, Probability, Risk Level, Detection Method and Mitigation aligned to EN50126 RAMS practice
- **Hazard Log** — per-hazard entries with initial and residual risk assessment, acceptance criteria, and status tracking
- **Session memory** — last UC, TC, SD, FMEA, and Hazard injected into every prompt so the LLM has full context for the next artefact
- **Variation Order / Scope Creep detection** — flagged and logged automatically across the session
- **Dual audit logging** — full interaction log + structured artefact register (JSON), ready for RAG ingestion and fine-tuning
- **Cookie-based session management** — one independent session per browser tab, 24-hour TTL with automatic eviction
- **Runs locally** — no cloud dependency beyond the LLM API, suitable for sensitive project documentation

---

## Standards Alignment

| Standard | Relevance |
|----------|-----------|
| CENELEC EN50126 | RAMS — Hazard Log and risk acceptance criteria |
| CENELEC EN50128 | Software V&V — FMEA as verification evidence |
| CENELEC EN50657 | Rolling stock software — Use Case and Test Spec structure |

---

## Architecture

```
voxrail/
├── main.py              # VoxRailSession, system prompt, LLM chat, logging
├── app.py               # FastAPI server, session store, cookie management
├── voxrail_cm/          # Configuration management plugin (links, matrices, /cm routes)
├── tests/               # pytest suite for the plugin (LLM stubbed)
├── docs/                # Design notes
├── templates/
│   └── index.html       # Web UI — Mermaid rendering, artefact badges, PNG export
├── .env.example         # Environment template
├── voxrail_interactions.json   # Generated at runtime — gitignored
├── voxrail_artefacts.json      # Generated at runtime — gitignored
└── voxrail_cm_data/            # Configuration graphs, one SQLite DB per project — gitignored
```

**Stack:**
- Python 3.11+
- FastAPI + Uvicorn
- LangChain + Groq (Llama 3.3 70B)
- Mermaid.js v11
- Vanilla JS / HTML / CSS — no frontend framework

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/SavacysCapital/voxrail.git
cd voxrail

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env and add your Groq API key from console.groq.com

# 4. Run
python app.py

# 5. Open browser
# http://127.0.0.1:8000

# Dev mode (hot reload — note: resets in-memory sessions on file save)
VOXRAIL_DEV=true python app.py
```

---

## Debug Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Server status and active session count |
| `/session/info` | GET | Current session artefact counters and memory state |
| `/session/reset` | DELETE | Clear current session (testing) |

---

## Configuration Management Plugin

Every artefact VoxRail generates is linked into a per-project configuration graph
(`voxrail_cm_data/<PROJECT_CODE>.db`), on two layers with the **train state** as the hub:

- **Layer 1 — use case level:** use cases ↔ train states (one or many) and actors;
  TC / FMEA / Hazard ↔ use cases.
- **Layer 2 — logical level:** train states ↔ electrical interfaces (from ED) and
  software states (from SD).

Existing state / actor / interface names are fed back into the prompt so they are reused;
near-duplicate names are suggested for merging, never merged silently.

| Endpoint | Purpose |
|----------|---------|
| `/cm/matrices` | Filterable matrices: UC × State, State × Logic, Traceability, UC × UC |
| `/cm/matrix/{name}` | Matrix as JSON, or `?format=csv` |
| `/cm/matrix.xlsx` | All matrices in one Excel workbook (needs `openpyxl`) |
| `/cm/view` | Graph diagram (best for small projects / focused views) |
| `/cm/states`, `/cm/states/{name}` | Train state catalogue and per-state view |
| `/cm/uc/{ref}/coupled` | Use cases coupled through shared states / actors / interfaces |
| `/cm/gaps`, `/cm/suggestions` | Consistency gaps and name-merge suggestions |

The project is taken from the session cookie, or `?project_code=`. Disable with
`VOXRAIL_PLUGINS=""`. Tests: `GROQ_API_KEY=dummy python -m pytest tests -q`.
Design: [`docs/config-management-plugin.md`](docs/config-management-plugin.md).

---

## Audit Logs

Two JSON files are written at runtime (gitignored — stay local):

**`voxrail_interactions.json`** — every message in sequence. Full audit trail including conversational turns.

**`voxrail_artefacts.json`** — structured artefact register. One entry per UC / TC / SD / FMEA / HZ generated, with type, reference, project code, all counters, and full response. Queryable by artefact type, project, and timestamp. Designed for future RAG ingestion and fine-tuning dataset construction.

---

## Safety Notice

FMEA and Hazard Log entries generated by VoxRail are AI-assisted first drafts.
They must be reviewed and validated by a qualified Safety Engineer before use
in any formal safety case, RAMS submission, or regulatory filing.
VoxRail is a productivity tool, not a substitute for professional safety
engineering judgement.

---

## Roadmap

- [ ] Word / Excel export (artefacts into the formal document chain)
- [ ] Document ingestion + RAG (PDF/DOCX reference project library)
- [ ] Fine-tuning on railway project artefacts
- [ ] IBM DOORS / Polarion ALM integration
- [ ] SIL classification tagging on Use Cases

---

## Licence

VoxRail is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

See [LICENSE](LICENSE) for full terms.

For commercial licensing enquiries contact: [savacyscapital@gmail.com]

© 2026 Savacys Capital

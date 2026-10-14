# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
#
# Commercial licensing available — contact Savacys Capital for enquiries.
# =============================================================================
"""
VoxRail — main.py  (v3.0)
==========================
Core brain: session management, system prompt, LLM invocation, logging.

Artefact types supported:
  UC   — Use Case
  TC   — Test Specification
  SD   — State Diagram (Mermaid stateDiagram-v2)
  FM   — FMEA (Failure Mode & Effects Analysis)
  HZ   — Hazard Log entry
  ED   — Electrical Interface Block Diagram (Mermaid flowchart)
  CC   — C Code (MISRA-C:2012 state machine skeleton)

Logging:
  voxrail_interactions.json  — every message (full audit trail)
  voxrail_artefacts.json     — structured artefact register (all types)

Plugins:
  Loaded from VOXRAIL_PLUGINS (comma-separated module names, default
  "voxrail_cm"; set to "" to disable). A plugin module exposes `plugin`
  with optional hooks:
    on_response(session, text)  — after every LLM response
    prompt_context(session)     — extra text for the system prompt
"""

import os
import re
import json
import logging
import importlib
from datetime import datetime
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

load_dotenv()

# ── LLM ────────────────────────────────────────────────────────────────────────
llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.25,
    max_tokens=4096,  # Free-tier TPM for gpt-oss-120b is 8K/min; input (~2.4K)
                      # + max_tokens must stay under that or Groq returns 413.
                      # Raise this once on the Groq Developer tier (~10x limits).
    # gpt-oss models emit a separate reasoning trace. "parsed" keeps it out of
    # message.content so it doesn't corrupt artefact pattern-matching / logging.
    # Use "hidden" to drop it entirely, or "raw" to inline it (not recommended here).
    reasoning_format="parsed",
)

# ── Constants ──────────────────────────────────────────────────────────────────
LOG_FILE             = "voxrail_interactions.json"
ARTEFACT_LOG_FILE    = "voxrail_artefacts.json"
# Kept small on purpose: the last use case / diagram / FMEA / etc. are already
# re-injected into the system prompt via SESSION MEMORY (see build_system_prompt),
# so a long raw transcript would send those bulky artefacts twice and blow past
# the free-tier 8K TPM ceiling on gpt-oss-120b. 6 keeps recent turns for continuity
# without duplicating the big artefacts. Raise this on the Groq Developer tier.
MAX_HISTORY_MESSAGES = 6

# Artefact detection — anchored to line start to prevent false positives
UC_PATTERN   = re.compile(r"^\*\*Use Case No\*\*",            re.MULTILINE)
TC_PATTERN   = re.compile(r"^\*\*Test Case Ref\*\*",           re.MULTILINE)
FMEA_PATTERN = re.compile(r"^\*\*FMEA Ref\*\*",               re.MULTILINE)
HZ_PATTERN   = re.compile(r"^\*\*Hazard Ref\*\*",             re.MULTILINE)
ELEC_PATTERN = re.compile(r"^\*\*Electrical Diagram Ref\*\*",  re.MULTILINE)
CODE_PATTERN = re.compile(r"^\*\*Code Ref\*\*",               re.MULTILINE)
SD_PATTERN   = re.compile(r"^\*\*State Diagram Ref\*\*",      re.MULTILINE)


# ── Plugins ────────────────────────────────────────────────────────────────────
plugin_logger = logging.getLogger("voxrail.plugins")
PLUGINS: list = []


def register_plugin(plugin) -> None:
    if plugin not in PLUGINS:
        PLUGINS.append(plugin)


def load_plugins() -> None:
    names = os.getenv("VOXRAIL_PLUGINS", "voxrail_cm")
    for name in filter(None, (n.strip() for n in names.split(","))):
        try:
            register_plugin(importlib.import_module(name).plugin)
            plugin_logger.info("Plugin loaded: %s", name)
        except Exception as e:
            plugin_logger.warning("Plugin %s not loaded: %s", name, e)


def emit(hook: str, *args) -> list:
    """Call `hook` on every plugin. A failing plugin never breaks the chat."""
    results = []
    for p in PLUGINS:
        fn = getattr(p, hook, None)
        if fn is None:
            continue
        try:
            results.append(fn(*args))
        except Exception:
            plugin_logger.exception("Plugin %s failed in %s", getattr(p, "name", p), hook)
    return results


load_plugins()


# ── Session ────────────────────────────────────────────────────────────────────
class VoxRailSession:
    """
    Holds all state for a single engineering session.
    One session = one browser tab = one project.
    """
    def __init__(self, project_name: str):
        self.project_name  = project_name
        self.project_code  = self._make_code(project_name)
        self.train_type: str | None = None

        # ── Artefact memory ──────────────────────────────────
        self.last_use_case:      str | None = None
        self.last_state_diagram: str | None = None
        self.last_fmea:          str | None = None
        self.last_hazard:        str | None = None
        self.last_electrical:    str | None = None
        self.last_code:          str | None = None

        # ── Counters (private) ───────────────────────────────
        self._uc_counter:   int = 0
        self._tc_counter:   int = 0
        self._fmea_counter: int = 0
        self._hz_counter:   int = 0
        self._elec_counter: int = 0
        self._code_counter: int = 0
        self._sd_counter:   int = 0

        # ── Conversation history ─────────────────────────────
        self.history: list = []

        # ── Audit logs ───────────────────────────────────────
        self.scope_creep_log: list[dict] = []
        self.hazard_log:      list[dict] = []

    @staticmethod
    def _make_code(name: str) -> str:
        words = name.strip().split()
        return "".join(w[0].upper() for w in words if w) or "VR"

    # ── Read-only properties ──────────────────────────────────────────────────
    @property
    def uc_counter(self)   -> int: return self._uc_counter
    @property
    def tc_counter(self)   -> int: return self._tc_counter
    @property
    def fmea_counter(self) -> int: return self._fmea_counter
    @property
    def hz_counter(self)   -> int: return self._hz_counter
    @property
    def elec_counter(self) -> int: return self._elec_counter
    @property
    def code_counter(self) -> int: return self._code_counter
    @property
    def sd_counter(self)   -> int: return self._sd_counter

    # ── Next-ref peek (no side effects) ──────────────────────────────────────
    def peek_uc_ref(self)   -> str:
        return f"UC-{self.project_code}-{self._uc_counter   + 1:03d}"
    def peek_tc_ref(self)   -> str:
        return f"TC-{self.project_code}-{self._tc_counter   + 1:03d}"
    def peek_fmea_ref(self) -> str:
        return f"FM-{self.project_code}-{self._fmea_counter + 1:03d}"
    def peek_hz_ref(self)   -> str:
        return f"HZ-{self.project_code}-{self._hz_counter   + 1:03d}"
    def peek_elec_ref(self) -> str:
        return f"ED-{self.project_code}-{self._elec_counter + 1:03d}"
    def peek_code_ref(self) -> str:
        return f"CC-{self.project_code}-{self._code_counter + 1:03d}"
    def peek_sd_ref(self)   -> str:
        return f"SD-{self.project_code}-{self._sd_counter   + 1:03d}"

    # ── Single mutation point ─────────────────────────────────────────────────
    def update_from_response(self, response: str) -> None:
        if UC_PATTERN.search(response):
            self._uc_counter += 1
            self.last_use_case = response

        if TC_PATTERN.search(response):
            self._tc_counter += 1

        if "```mermaid" in response:
            # Distinguish state diagram vs electrical diagram
            if ELEC_PATTERN.search(response):
                self.last_electrical = response
            else:
                self.last_state_diagram = response
                self._sd_counter += 1

        if FMEA_PATTERN.search(response):
            self._fmea_counter += 1
            self.last_fmea = response

        if HZ_PATTERN.search(response):
            self._hz_counter += 1
            self.last_hazard = response
            self.hazard_log.append({
                "timestamp": datetime.now().isoformat(),
                "hz_ref":    f"HZ-{self.project_code}-{self._hz_counter:03d}",
                "response":  response
            })

        if ELEC_PATTERN.search(response):
            self._elec_counter += 1
            self.last_electrical = response

        if CODE_PATTERN.search(response):
            self._code_counter += 1
            self.last_code = response

        for line in response.splitlines():
            if line.strip().startswith("⚠️ VARIATION ORDER"):
                self.scope_creep_log.append({
                    "timestamp": datetime.now().isoformat(),
                    "detail":    line.strip()
                })

    # ── Train type detection ──────────────────────────────────────────────────
    def detect_train_type(self, text: str) -> None:
        t = text.lower()
        if any(k in t for k in ["regional", "6-car", "6 car"]):
            self.train_type = "Regional Train (6-car, extendable to 10-car)"
        elif any(k in t for k in ["metro", "suburban", "emu"]):
            self.train_type = "Metro / Suburban EMU"
        elif any(k in t for k in ["high speed", "hst", "intercity"]):
            self.train_type = "High Speed / Intercity"
        elif any(k in t for k in ["freight", "cargo"]):
            self.train_type = "Freight"

    # ── History trimming ──────────────────────────────────────────────────────
    def trimmed_history(self) -> list:
        h = self.history
        if len(h) > MAX_HISTORY_MESSAGES:
            overflow  = len(h) - MAX_HISTORY_MESSAGES
            overflow += overflow % 2
            h = h[overflow:]
        return h

    # ── Session summary ───────────────────────────────────────────────────────
    def summary(self) -> dict:
        return {
            "project":        self.project_name,
            "project_code":   self.project_code,
            "train_type":     self.train_type,
            "uc_counter":     self.uc_counter,
            "tc_counter":     self.tc_counter,
            "fmea_counter":   self.fmea_counter,
            "hz_counter":     self.hz_counter,
            "elec_counter":   self.elec_counter,
            "code_counter":   self.code_counter,
            "sd_counter":     self.sd_counter,
            "history_len":    len(self.history),
            "has_last_uc":    self.last_use_case      is not None,
            "has_last_sd":    self.last_state_diagram  is not None,
            "has_last_fmea":  self.last_fmea           is not None,
            "has_last_hz":    self.last_hazard         is not None,
            "has_last_elec":  self.last_electrical     is not None,
            "has_last_code":  self.last_code           is not None,
            "scope_flags":    len(self.scope_creep_log),
            "hazard_count":   len(self.hazard_log),
        }


# ── System prompt ──────────────────────────────────────────────────────────────
def build_system_prompt(session: VoxRailSession) -> str:

    memory = ""

    if session.last_use_case:
        memory += (
            "\n=== SESSION MEMORY: LAST USE CASE ===\n"
            "Use when user says 'this use case', 'the last use case', asks for a\n"
            "test case, FMEA, hazard, electrical diagram, or C code without specifying one.\n"
            f"{session.last_use_case}\n"
            "=== END LAST USE CASE ===\n"
        )

    if session.last_state_diagram:
        memory += (
            "\n=== SESSION MEMORY: LAST STATE DIAGRAM ===\n"
            "Use when user says 'modify the diagram', 'update the diagram',\n"
            "or asks for C code based on the state machine.\n"
            f"{session.last_state_diagram}\n"
            "=== END LAST STATE DIAGRAM ===\n"
        )

    if session.last_fmea:
        memory += (
            "\n=== SESSION MEMORY: LAST FMEA ===\n"
            "Use when user asks to update the FMEA or generate a Hazard Log.\n"
            f"{session.last_fmea}\n"
            "=== END LAST FMEA ===\n"
        )

    if session.last_hazard:
        memory += (
            "\n=== SESSION MEMORY: LAST HAZARD LOG ===\n"
            "Use when user asks to update or extend the hazard log.\n"
            f"{session.last_hazard}\n"
            "=== END LAST HAZARD LOG ===\n"
        )

    if session.last_electrical:
        memory += (
            "\n=== SESSION MEMORY: LAST ELECTRICAL DIAGRAM ===\n"
            "Use when user asks to modify or extend the electrical diagram.\n"
            f"{session.last_electrical}\n"
            "=== END LAST ELECTRICAL DIAGRAM ===\n"
        )

    if session.last_code:
        memory += (
            "\n=== SESSION MEMORY: LAST C CODE ===\n"
            "Use when user asks to modify or extend the generated C code.\n"
            f"{session.last_code}\n"
            "=== END LAST C CODE ===\n"
        )

    plugin_context = "".join(c for c in emit("prompt_context", session) if c)

    history_note = ""
    if len(session.history) > MAX_HISTORY_MESSAGES // 2:
        history_note = (
            f"\nNote: {len(session.history)} messages in history. "
            f"Oldest trimmed to stay within context limits.\n"
        )

    return f"""You are VoxRail, a senior Railway Systems Engineer AI assistant.
Specialisms: rolling stock systems, CENELEC EN50126/50128/50657, RAMS, FMEA,
Hazard Analysis, electrical interface design, embedded C software architecture.

Project : {session.project_name} (Code: {session.project_code})
Train   : {session.train_type or 'Not yet specified — ask if relevant'}
Next UC : {session.peek_uc_ref()}
Next TC : {session.peek_tc_ref()}
Next FM : {session.peek_fmea_ref()}
Next HZ : {session.peek_hz_ref()}
Next ED : {session.peek_elec_ref()}
Next CC : {session.peek_code_ref()}
Next SD : {session.peek_sd_ref()}
{history_note}{memory}{plugin_context}

════════════════════════════════════════════════════════
USE CASE TEMPLATE  (reproduce exactly — no deviations)
════════════════════════════════════════════════════════
**Use Case No**: {session.peek_uc_ref()}
**Use Case Title**:
**Use Case Description**:
**Previous Train / System State**:
**New Train / System State**:
**Applicable Train States**: [comma-separated; every train state this use case can occur in — one or more]
**State Transition Expected Time**:
**Primary Actors**: [comma-separated]
**Logical Interfaces**: [comma-separated signals / buses this use case relies on, if known]
**Trigger**:
**Trigger Type**: [Automatic | Manual | Timed | Conditional]
**Preconditions**:
**Postconditions**:
**Feedback Type**: [Visual | Audible | Haptic | Combined]
**Expected Results**:
-
**Degraded Modes**:
- Mode 1:
- Mode 2:

════════════════════════════════════════════════════════
TEST SPECIFICATION TEMPLATE  (reproduce exactly)
════════════════════════════════════════════════════════
**Test Case Ref**: {session.peek_tc_ref()}
**Use Case Ref**:
**Use Case Title**:
**Test Description**:
**Steps**:

The Cab Car, Motor Car, and Trailer Car columns indicate APPLICABILITY ONLY.
Leave them blank. Mark N/A only if a step explicitly does not apply to that car type.
Do NOT populate with states, values, or expected results.

| Step | Action | Expected Result | Cab Car | Motor Car | Trailer Car |
|------|--------|-----------------|---------|-----------|-------------|
| 1    |        |                 |         |           |             |

════════════════════════════════════════════════════════
STATE DIAGRAM
════════════════════════════════════════════════════════
**State Diagram Ref**: {session.peek_sd_ref()}
**Related UC**:
**Train States Modelled**: [comma-separated train states this diagram designs]

Generate valid Mermaid v10+ stateDiagram-v2 syntax in triple backticks.
Use logical railway state names. Include all degraded modes from the Use Case.

════════════════════════════════════════════════════════
FMEA TEMPLATE  (reproduce exactly)
════════════════════════════════════════════════════════
⚠️ SAFETY NOTICE: This FMEA is AI-generated. It must be reviewed and validated
by a qualified Safety Engineer before use in any formal safety case or submission.

**FMEA Ref**: {session.peek_fmea_ref()}
**Related UC**:
**Related State**:
**Component / Function**:
**Failure Mode**:
**Local Effect**:
**System Effect**:
**Effect on Safety**:
**Severity**: [Catastrophic | Critical | Marginal | Negligible]
**Probability**: [Frequent | Probable | Occasional | Remote | Improbable]
**Risk Level**: [Intolerable | ALARP | Tolerable]
**Detection Method**:
**Mitigation / Control Measure**:
**Residual Risk**:
**Remarks**:

════════════════════════════════════════════════════════
HAZARD LOG TEMPLATE  (reproduce exactly)
════════════════════════════════════════════════════════
⚠️ SAFETY NOTICE: This Hazard Log entry is AI-generated. It must be reviewed
and validated by a qualified Safety Engineer before use in any formal safety
case, RAMS submission, or regulatory filing.

**Hazard Ref**: {session.peek_hz_ref()}
**Related UC**:
**Related FMEA Ref**:
**Hazard Description**:
**Hazard Category**: [Loss of Traction | Loss of Braking | Door Failure |
                      Electrical Fault | Software Failure | Human Factors |
                      Fire | Derailment | Collision | Environmental]
**Hazardous Event**:
**Worst Case Consequence**:
**Initial Severity**: [Catastrophic | Critical | Marginal | Negligible]
**Initial Likelihood**: [Frequent | Probable | Occasional | Remote | Improbable]
**Initial Risk Level**: [Intolerable | ALARP | Tolerable]
**Existing Controls**:
**Additional Mitigation Required**:
**Residual Severity**: [Catastrophic | Critical | Marginal | Negligible]
**Residual Likelihood**: [Frequent | Probable | Occasional | Remote | Improbable]
**Residual Risk Level**: [Intolerable | ALARP | Tolerable]
**Acceptance Criteria**: [Tolerable | ALARP | Intolerable]
**Responsible Engineer**:
**Status**: [Open | Under Review | Closed]

════════════════════════════════════════════════════════
ELECTRICAL INTERFACE DIAGRAM TEMPLATE  (reproduce exactly)
════════════════════════════════════════════════════════
⚠️ DESIGN NOTICE: This electrical interface diagram is AI-generated from the
Use Case context. It shows logical signal interfaces only — NOT a certified
wiring diagram. All interface definitions must be verified against the actual
hardware specification before use in design or testing.

Generate when user asks for: wiring diagram, electrical diagram, circuit diagram,
electrical interface, or signal interface related to a Use Case or system function.

Use Mermaid flowchart LR syntax. Show: power sources, control units, actuators,
sensors, signal paths with type and voltage level where known from the Use Case.
Label each arrow with: signal name, type (Digital/Analogue/CAN/MVB/Ethernet),
and voltage or protocol where applicable.

**Electrical Diagram Ref**: {session.peek_elec_ref()}
**Related UC**:
**System / Function**:
**Train States Served**: [comma-separated train states this logic realises]
**Logical Interfaces**: [semicolon-separated "Signal name, Type, Voltage/Protocol"; same names as the arrow labels]
**Voltage Domain**: [24V DC | 110V DC | 750V DC | 25kV AC | Mixed]
**Interface Standard**: [CAN | MVB | Ethernet | Discrete | Analogue | Mixed]
**Diagram Notes**:

```mermaid
flowchart LR
  %% Replace with actual system elements from Use Case
  PS[Power Supply] -->|24V DC| CU[Control Unit]
  CU -->|Digital Out 24V| ACT[Actuator]
  ACT -->|Feedback Signal| CU
  CU -->|CAN Bus| VCU[Vehicle Control Unit]
```

════════════════════════════════════════════════════════
C CODE GENERATION TEMPLATE  (reproduce exactly)
════════════════════════════════════════════════════════
⚠️ SOFTWARE NOTICE: This C code skeleton is AI-generated from the Use Case
and State Diagram. It is a DESIGN AID ONLY — NOT production code.
It must be reviewed, completed, tested, and formally verified by a qualified
Software Engineer before use in any SIL-rated system under EN50128.
MISRA-C:2012 compliance must be validated by a certified static analysis tool.

Generate when user asks for: C code, software implementation, state machine code,
embedded code, or software design from a Use Case or State Diagram.

Base on Last Use Case AND Last State Diagram in session memory.
Follow MISRA-C:2012 guidelines. Use transition table pattern.
Generate: file header, typedefs, state enum, event enum,
transition table, state machine handler, and stub callbacks.

**Code Ref**: {session.peek_code_ref()}
**Related UC**:
**Related State Diagram**:
**Target Standard**: EN50128 / MISRA-C:2012
**SIL Level**: [To be determined by Safety Engineer]
**Language**: C (C99)

```c
/* ============================================================
 * VoxRail Generated Code Skeleton
 * Code Ref   : CC-[PROJECT]-XXX
 * Related UC : UC-[PROJECT]-XXX
 * Standard   : EN50128 / MISRA-C:2012
 * SIL Level  : [TO BE DETERMINED BY SAFETY ENGINEER]
 * Generated  : [DATE]
 *
 * WARNING: This is an AI-generated skeleton for design purposes.
 * NOT for use in production without full engineering review,
 * formal verification, and static analysis (MISRA-C:2012).
 * ============================================================ */

#ifndef [MODULE]_SM_H
#define [MODULE]_SM_H

#include <stdint.h>

/* State definitions */
typedef enum {{
    STATE_[NAME] = 0,
    /* ... add states from State Diagram ... */
    STATE_COUNT
}} [Module]State_t;

/* Event definitions */
typedef enum {{
    EVENT_[NAME] = 0,
    /* ... add events from Use Case triggers ... */
    EVENT_COUNT
}} [Module]Event_t;

/* Transition entry */
typedef struct {{
    [Module]State_t  current_state;
    [Module]Event_t  event;
    [Module]State_t  next_state;
    void             (*action)(void);
}} [Module]Transition_t;

/* Action stubs — implement in application layer */
/* ... */

/* Transition table */
static const [Module]Transition_t transitions[] = {{
    /* {{current_state, event, next_state, action_fn}} */
}};

/* State machine handler */
[Module]State_t SM_[Module]_Process(
    [Module]State_t current_state,
    [Module]Event_t event
);

#endif /* [MODULE]_SM_H */
```

════════════════════════════════════════════════════════
RULES  (apply to every response without exception)
════════════════════════════════════════════════════════
1. Generate ONLY what was explicitly requested — nothing more.
   Never bundle multiple artefact types unless the user explicitly
   requests more than one in the same message.

2. VARIATION ORDER / SCOPE CREEP: Begin response with:
   ⚠️ VARIATION ORDER DETECTED: [one-line description]
   if the request is outside current agreed scope. Then continue.

3. Always use the exact reference shown above (Next UC/TC/SD/FM/HZ/ED/CC).
   Never write XXX or any placeholder.

4. Memory references:
   "this use case" / "the last use case"      → SESSION MEMORY: LAST USE CASE
   "this diagram" / "the state machine"        → SESSION MEMORY: LAST STATE DIAGRAM
   "this FMEA" / "the last FMEA"              → SESSION MEMORY: LAST FMEA
   "generate hazards" (unspecified)            → last UC + last FMEA
   "electrical diagram" / "wiring" (no spec)  → last UC + last SD
   "C code" / "generate code" (no spec)       → last UC + last SD

5. For FMEA, Hazard Log, Electrical Diagram, and C Code:
   Always include the relevant ⚠️ notice at the top. Never omit it.

6. Flag any safety-critical ambiguities before proceeding.

7. FMEA and Hazard entries must be grounded in specific system states
   from the Use Case or State Diagram — not generic failure modes.

8. Electrical diagrams show LOGICAL INTERFACES only — not certified
   wiring. Use Mermaid flowchart LR syntax with labelled arrows.

9. C code must follow MISRA-C:2012 transition table pattern.
   All action functions declared as stubs with clear comments.
   Include EN50128 file header with SIL level placeholder.

10. Train state, actor and interface names: reuse the exact names from the
    CONFIGURATION CATALOGUE when one is shown. They link artefacts together.
"""


# ── Core chat ──────────────────────────────────────────────────────────────────
def chat(session: VoxRailSession, user_input: str) -> str:
    session.detect_train_type(user_input)
    session.history.append(HumanMessage(content=user_input))

    system_msg = SystemMessage(content=build_system_prompt(session))
    messages   = [system_msg] + session.trimmed_history()

    response_msg  = llm.invoke(messages)
    response_text = response_msg.content

    session.history.append(AIMessage(content=response_text))
    session.update_from_response(response_text)
    emit("on_response", session, response_text)

    return response_text


# ── Artefact logging ───────────────────────────────────────────────────────────
def log_artefact(session: VoxRailSession, response: str) -> None:
    artefact_type = None
    ref           = None

    if UC_PATTERN.search(response):
        artefact_type = "USE_CASE"
        m = re.search(r"\*\*Use Case No\*\*:\s*(.+)", response)
        ref = m.group(1).strip() if m else f"UC-{session.project_code}-{session.uc_counter:03d}"

    elif TC_PATTERN.search(response):
        artefact_type = "TEST_SPEC"
        m = re.search(r"\*\*Test Case Ref\*\*:\s*(.+)", response)
        ref = m.group(1).strip() if m else f"TC-{session.project_code}-{session.tc_counter:03d}"

    elif FMEA_PATTERN.search(response):
        artefact_type = "FMEA"
        m = re.search(r"\*\*FMEA Ref\*\*:\s*(.+)", response)
        ref = m.group(1).strip() if m else f"FM-{session.project_code}-{session.fmea_counter:03d}"

    elif HZ_PATTERN.search(response):
        artefact_type = "HAZARD"
        m = re.search(r"\*\*Hazard Ref\*\*:\s*(.+)", response)
        ref = m.group(1).strip() if m else f"HZ-{session.project_code}-{session.hz_counter:03d}"

    elif ELEC_PATTERN.search(response):
        artefact_type = "ELECTRICAL_DIAGRAM"
        m = re.search(r"\*\*Electrical Diagram Ref\*\*:\s*(.+)", response)
        ref = m.group(1).strip() if m else f"ED-{session.project_code}-{session.elec_counter:03d}"

    elif CODE_PATTERN.search(response):
        artefact_type = "C_CODE"
        m = re.search(r"\*\*Code Ref\*\*:\s*(.+)", response)
        ref = m.group(1).strip() if m else f"CC-{session.project_code}-{session.code_counter:03d}"

    elif SD_PATTERN.search(response) or "```mermaid" in response:
        artefact_type = "STATE_DIAGRAM"
        m = re.search(r"\*\*State Diagram Ref\*\*:\s*(.+)", response)
        ref = m.group(1).strip() if m else f"SD-{session.project_code}-{session.sd_counter:03d}"

    if not artefact_type:
        return

    entry = {
        "timestamp":      datetime.now().isoformat(),
        "artefact_type":  artefact_type,
        "ref":            ref,
        "project":        session.project_name,
        "project_code":   session.project_code,
        "train_type":     session.train_type,
        "uc_counter":     session.uc_counter,
        "tc_counter":     session.tc_counter,
        "fmea_counter":   session.fmea_counter,
        "hz_counter":     session.hz_counter,
        "elec_counter":   session.elec_counter,
        "code_counter":   session.code_counter,
        "full_response":  response
    }

    try:
        data: list = []
        if os.path.exists(ARTEFACT_LOG_FILE):
            with open(ARTEFACT_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data.append(entry)
        with open(ARTEFACT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Artefact log warning] {e}")


# ── Interaction logging ────────────────────────────────────────────────────────
def log_interaction(session: VoxRailSession, user_input: str, response: str) -> None:
    entry = {
        "timestamp":    datetime.now().isoformat(),
        "project":      session.project_name,
        "train_type":   session.train_type,
        "uc_counter":   session.uc_counter,
        "tc_counter":   session.tc_counter,
        "fmea_counter": session.fmea_counter,
        "hz_counter":   session.hz_counter,
        "elec_counter": session.elec_counter,
        "code_counter": session.code_counter,
        "user_input":   user_input,
        "response":     response
    }
    try:
        data: list = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data.append(entry)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Log warning] {e}")

    log_artefact(session, response)


# ── CLI entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("✅ VoxRail v3.0 — Railway Systems Engineer AI")
    print("   Artefacts: UC · TC · SD · FM · HZ · ED · CC")
    print(f"   Logs: {LOG_FILE}  |  {ARTEFACT_LOG_FILE}\n")

    project_name = input("Enter project name: ").strip() or "VoxRail Project"
    session = VoxRailSession(project_name)

    print(f"\nProject code : {session.project_code}")
    print(f"History limit: {MAX_HISTORY_MESSAGES} messages")
    print("Commands: /memory  /scope  /hazards  /cm  exit\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Session ended.")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "bye", "q"}:
            print("👋 Session ended.")
            break

        if user_input.lower() == "/memory":
            print("\n📋 Session Memory")
            for k, v in session.summary().items():
                print(f"  {k:<20}: {v}")
            print()
            continue

        if user_input.lower() == "/scope":
            if session.scope_creep_log:
                print(f"\n⚠️  {len(session.scope_creep_log)} Variation Order(s):")
                for i, s in enumerate(session.scope_creep_log, 1):
                    print(f"  {i}. [{s['timestamp']}] {s['detail']}")
            else:
                print("\n✅ No scope creep flagged this session.")
            print()
            continue

        if user_input.lower() == "/hazards":
            if session.hazard_log:
                print(f"\n🔴 {len(session.hazard_log)} Hazard(s) logged:")
                for i, h in enumerate(session.hazard_log, 1):
                    print(f"  {i}. [{h['timestamp']}] {h['hz_ref']}")
            else:
                print("\n✅ No hazards logged this session.")
            print()
            continue

        if user_input.lower() == "/cm":
            try:
                from voxrail_cm import graph as cm_graph
                from voxrail_cm.store import get_store
                store = get_store(session.project_code)
                print("\n🔗 Configuration graph")
                print(json.dumps(cm_graph.summary(store), indent=2))
                print("Gaps:", json.dumps(cm_graph.gaps(store), indent=2))
            except Exception as e:
                print(f"\nConfiguration management plugin unavailable: {e}")
            print()
            continue

        response = chat(session, user_input)
        print(f"\nVoxRail:\n{response}\n")
        log_interaction(session, user_input, response)
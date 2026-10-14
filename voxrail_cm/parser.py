# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
# =============================================================================
"""
voxrail_cm.parser
=================
Deterministic extraction of artefacts and their link fields from a VoxRail
LLM response. No LLM calls: everything comes from the fixed templates in
main.build_system_prompt().
"""

import re
from dataclasses import dataclass, field

# Ref field per artefact type (first match wins for type detection order)
REF_FIELDS = [
    ("UC", "Use Case No"),
    ("TC", "Test Case Ref"),
    ("SD", "State Diagram Ref"),
    ("FM", "FMEA Ref"),
    ("HZ", "Hazard Ref"),
    ("ED", "Electrical Diagram Ref"),
    ("CC", "Code Ref"),
]

TITLE_FIELDS = {
    "UC": "Use Case Title",
    "TC": "Test Description",
    "SD": "Related UC",
    "FM": "Failure Mode",
    "HZ": "Hazard Description",
    "ED": "System / Function",
    "CC": "Related UC",
}

REF_RE   = re.compile(r"\b(UC|TC|SD|FM|HZ|ED|CC)-[A-Z0-9]+-\d{3}\b")
FIELD_RE = re.compile(r"^\*\*(?P<name>[^*]+?)\*\*\s*:\s*(?P<value>.*)$", re.MULTILINE)
ARROW_LABEL_RE = re.compile(r"--+>?\|([^|]+)\||-- *([^->|][^->]*?) *-->")
SD_TRANSITION_RE = re.compile(
    r"^\s*(\[\*\]|[\w\"]+)\s*-->\s*(\[\*\]|[\w\"]+)\s*(?::\s*(.+))?$", re.MULTILINE
)
SD_STATE_ALIAS_RE = re.compile(r'^\s*state\s+"([^"]+)"\s+as\s+(\w+)', re.MULTILINE)

INTERFACE_KINDS = ["CAN", "MVB", "WTB", "Ethernet", "Discrete", "Digital", "Analogue",
                   "Analog", "RS485", "PWM", "Relay"]
PLACEHOLDER_RE = re.compile(r"^\[.*\]$|^(n/?a|none|tbd|-|—)$", re.IGNORECASE)


@dataclass
class ParsedArtefact:
    type:        str
    ref:         str
    title:       str = ""
    text:        str = ""
    actors:      list[str] = field(default_factory=list)
    states:      list[str] = field(default_factory=list)          # applicable / served / modelled
    from_states: list[str] = field(default_factory=list)          # UC previous state
    to_states:   list[str] = field(default_factory=list)          # UC new state
    interfaces:  list[dict] = field(default_factory=list)         # {name, kind}
    sw_elements: list[dict] = field(default_factory=list)         # {name, kind}
    refs:        dict[str, list[str]] = field(default_factory=dict)  # link kind -> target refs


# ── Field helpers ─────────────────────────────────────────────────────────────
def _fields(text: str) -> dict[str, str]:
    """Map '**Name**: value' to value, including '- item' continuation lines."""
    out: dict[str, str] = {}
    matches = list(FIELD_RE.finditer(text))
    for i, m in enumerate(matches):
        name  = m.group("name").strip()
        value = m.group("value").strip()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        extra = []
        for line in text[m.end():end].splitlines():
            s = line.strip()
            if s.startswith(("-", "•", "*")) and not s.startswith("**"):
                extra.append(s.lstrip("-•* ").strip())
            elif s and extra:
                break
            elif s.startswith(("```", "|", "═", "⚠")):
                break
        if extra:
            value = ", ".join([v for v in [value] + extra if v])
        out.setdefault(name.lower(), value)
    return out


def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", value)   # drop "(notes)" and "[placeholders]"
    parts = re.split(r"[,;\n]| / ", value)
    out = []
    for p in parts:
        p = p.strip().strip(".").strip("`").strip()
        if p and not PLACEHOLDER_RE.match(p) and len(p) <= 80:
            out.append(p)
    return out


def _detect_kind(label: str) -> str:
    low = label.lower()
    for k in INTERFACE_KINDS:
        if k.lower() in low:
            return {"Digital": "Discrete", "Analog": "Analogue"}.get(k, k)
    return "Unspecified"


VOLTAGE_RE = re.compile(r"^\d+(\.\d+)?\s*k?V\b|^(AC|DC)$", re.IGNORECASE)


def _is_attribute(token: str) -> bool:
    t = token.strip()
    return bool(VOLTAGE_RE.match(t)) or any(t.lower() == k.lower() for k in INTERFACE_KINDS) \
        or t.lower() in ("digital out", "digital in", "bus")


def parse_interface_list(value: str | None) -> list[dict]:
    """'Door Closed Loop, Discrete, 110V DC; Door Release Command, MVB' -> 2 interfaces.
    Works with ';' or ',' separators: type/voltage tokens attach to the preceding signal."""
    if not value:
        return []
    value = re.sub(r"\[[^\]]*\]", "", value)
    groups: list[list[str]] = []
    for part in re.split(r"[;\n]", value):
        for tok in part.split(","):
            tok = tok.strip().strip(".").strip()
            if not tok or PLACEHOLDER_RE.match(tok):
                continue
            if groups and _is_attribute(tok):
                groups[-1].append(tok)
            else:
                groups.append([tok])
        # a ';' always closes the current interface
        if groups:
            groups.append([])
    out = []
    for g in groups:
        if g:
            i = parse_interface(", ".join(g))
            if i:
                out.append(i)
    return _dedupe(out)


def parse_interface(raw: str) -> dict | None:
    """'Door Closed Loop, Discrete, 110V DC' -> {name: 'Door Closed Loop', kind: 'Discrete'}"""
    raw = raw.strip().strip('"')
    if not raw:
        return None
    name = raw.split(",")[0].strip()
    if not name or PLACEHOLDER_RE.match(name):
        return None
    return {"name": name, "kind": _detect_kind(raw)}


def _mermaid_blocks(text: str) -> list[str]:
    return re.findall(r"```mermaid\s*\n(.*?)```", text, re.DOTALL)


# ── Main entry point ──────────────────────────────────────────────────────────
def detect_types(text: str) -> list[str]:
    return [t for t, f in REF_FIELDS if re.search(rf"^\*\*{re.escape(f)}\*\*", text, re.MULTILINE)]


def parse_response(text: str) -> list[ParsedArtefact]:
    """Return every artefact found in one LLM response."""
    found: list[ParsedArtefact] = []
    # Split the response on ref-field lines so a two-artefact answer parses as two
    starts = []
    for t, f in REF_FIELDS:
        for m in re.finditer(rf"^\*\*{re.escape(f)}\*\*", text, re.MULTILINE):
            starts.append((m.start(), t))
    starts.sort()
    for i, (pos, t) in enumerate(starts):
        # Include the preamble (notices) for the first artefact; each chunk runs to the next ref
        chunk_start = 0 if i == 0 else pos
        chunk_end   = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        art = _parse_one(t, text[chunk_start:chunk_end])
        if art:
            found.append(art)
    return found


def _first_ref(value: str | None, prefix: str | None = None) -> list[str]:
    if not value:
        return []
    refs = [m.group(0) for m in REF_RE.finditer(value)]
    if prefix:
        refs = [r for r in refs if r.startswith(prefix + "-")]
    return refs


def _parse_one(t: str, chunk: str) -> ParsedArtefact | None:
    f = _fields(chunk)
    ref_field = dict(REF_FIELDS)[t].lower()
    refs = _first_ref(f.get(ref_field), t)
    if not refs:
        return None
    art = ParsedArtefact(type=t, ref=refs[0], text=chunk)
    art.title = (f.get(TITLE_FIELDS[t].lower()) or "")[:200]

    related_uc = _first_ref(f.get("related uc") or f.get("use case ref"), "UC")

    if t == "UC":
        art.actors      = split_list(f.get("primary actors"))
        art.states      = split_list(f.get("applicable train states"))
        art.from_states = split_list(f.get("previous train / system state"))
        art.to_states   = split_list(f.get("new train / system state"))
        art.interfaces  = parse_interface_list(f.get("logical interfaces"))

    elif t == "TC":
        art.refs["verifies"] = related_uc

    elif t == "FM":
        art.refs["analyses"] = related_uc
        art.states = split_list(f.get("related state"))

    elif t == "HZ":
        art.refs["derived_from"] = related_uc + _first_ref(f.get("related fmea ref"), "FM")

    elif t == "ED":
        art.refs["realises"] = related_uc
        art.states = split_list(f.get("train states served"))
        ifaces = parse_interface_list(f.get("logical interfaces"))
        if not ifaces:                       # fall back to labelled arrows in the flowchart
            for block in _mermaid_blocks(chunk):
                for m in ARROW_LABEL_RE.finditer(block):
                    i = parse_interface(m.group(1) or m.group(2) or "")
                    if i:
                        ifaces.append(i)
        art.interfaces = _dedupe(ifaces)

    elif t == "SD":
        art.refs["models_uc"] = related_uc
        art.states = split_list(f.get("train states modelled"))
        art.sw_elements = _sd_elements(chunk)

    elif t == "CC":
        art.refs["implements"] = related_uc + _first_ref(f.get("related state diagram"), "SD")

    return art


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for i in items:
        k = i["name"].lower()
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


def _sd_elements(chunk: str) -> list[dict]:
    """States and events from a Mermaid stateDiagram-v2 block."""
    out: list[dict] = []
    for block in _mermaid_blocks(chunk):
        if "stateDiagram" not in block:
            continue
        aliases = {a: n for n, a in SD_STATE_ALIAS_RE.findall(block)}
        for a, b, ev in SD_TRANSITION_RE.findall(block):
            for s in (a, b):
                if s != "[*]":
                    out.append({"name": aliases.get(s, s.strip('"').replace("_", " ")), "kind": "state"})
            if ev:
                out.append({"name": ev.strip(), "kind": "event"})
    seen, uniq = set(), []
    for e in out:
        k = (e["kind"], e["name"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq

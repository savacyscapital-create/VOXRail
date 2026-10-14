# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
# =============================================================================
"""
voxrail_cm.api — FastAPI routes mounted at /cm by app.py.

The project is taken from ?project_code=… or, if absent, from the caller's
VoxRail session cookie (resolved by the function app.py passes in).
"""

import json
from typing import Callable

from fastapi import APIRouter, Cookie, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from . import graph as g
from . import change as ch
from . import matrix as mx
from .store import ENTITY_KINDS, ekey, get_store


class CatalogueIn(BaseModel):
    names: list[str]


class MergeIn(BaseModel):
    into_id: int


class LinkIn(BaseModel):
    src: str
    dst: str
    kind: str


class TargetIn(BaseModel):
    target: str                      # "UC-MP-001", "state:Ready to Drive", "interface:Door Closed Loop"
    change_type: str = "modify"


class CRIn(BaseModel):
    title: str
    description: str = ""
    targets: list[TargetIn] = []


class StatusIn(BaseModel):
    status: str
    note: str = ""


def make_router(resolve_project_code: Callable[[str | None], str | None]) -> APIRouter:
    r = APIRouter(prefix="/cm", tags=["configuration-management"])

    def code_for(project_code: str | None, cookie: str | None) -> str:
        code = project_code or resolve_project_code(cookie)
        if not code:
            raise HTTPException(404, "No project: pass ?project_code= or start a VoxRail session first")
        return code

    def store_for(project_code: str | None, cookie: str | None):
        return get_store(code_for(project_code, cookie))

    def entity_key(store, kind: str, name_or_id: str) -> str:
        if name_or_id.isdigit():
            e = store.entity(int(name_or_id))
        else:
            e = store.find_entity(kind, name_or_id)
        if not e or e["kind"] != kind:
            raise HTTPException(404, f"{kind} '{name_or_id}' not found")
        return ekey(kind, e["id"])

    @r.get("/summary")
    def summary(project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        return g.summary(store_for(project_code, voxrail_session))

    @r.get("/graph")
    def graph(layer: str = Query("both", pattern="^(1|2|both)$"), project_code: str | None = None,
              voxrail_session: str | None = Cookie(default=None)):
        return g.graph(store_for(project_code, voxrail_session), layer)

    @r.get("/graph.mmd", response_class=PlainTextResponse)
    def graph_mermaid(layer: str = Query("both", pattern="^(1|2|both)$"), project_code: str | None = None,
                      voxrail_session: str | None = Cookie(default=None)):
        return g.mermaid(store_for(project_code, voxrail_session), layer)

    @r.get("/view", response_class=HTMLResponse)
    def view(layer: str = Query("both", pattern="^(1|2|both)$"), project_code: str | None = None,
             voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        src = json.dumps(g.mermaid(s, layer)).replace("</", "<\\/")
        return HTMLResponse(_VIEW_HTML.replace("{{MERMAID_JSON}}", src).replace("{{LAYER}}", layer))

    @r.get("/gaps")
    def gaps(project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        return g.gaps(store_for(project_code, voxrail_session))

    @r.get("/artefacts")
    def artefacts(type: str | None = None, project_code: str | None = None,
                  voxrail_session: str | None = Cookie(default=None)):
        return store_for(project_code, voxrail_session).artefacts(type)

    @r.get("/artefact/{ref}")
    def artefact(ref: str, text: bool = False, project_code: str | None = None,
                 voxrail_session: str | None = Cookie(default=None)):
        a = store_for(project_code, voxrail_session).artefact(ref, with_text=text)
        if not a:
            raise HTTPException(404, f"{ref} not found")
        return a

    @r.get("/uc/{ref}/coupled")
    def coupled(ref: str, project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        if not s.artefact(ref):
            raise HTTPException(404, f"{ref} not found")
        return g.coupled_use_cases(s, ref)

    # ── Matrix views ──────────────────────────────────────────────────────────
    def _filters(row, col, actor, state, gaps_only, hide_empty):
        return dict(row=row, col=col, actor=actor, state=state, gaps_only=gaps_only, hide_empty=hide_empty)

    @r.get("/matrix")
    def matrix_list():
        return [{"name": k, "title": v} for k, v in mx.MATRICES.items()]

    @r.get("/matrix.xlsx")
    def matrix_xlsx(row: str | None = None, col: str | None = None, actor: str | None = None,
                    state: str | None = None, gaps_only: bool = False, hide_empty: bool = False,
                    project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        f = _filters(row, col, actor, state, gaps_only, hide_empty)
        try:
            data = mx.to_xlsx([mx.build(s, n, **f) for n in mx.MATRICES])
        except ImportError:
            raise HTTPException(501, "Excel export needs openpyxl: pip install openpyxl (CSV export still works)")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="voxrail_cm_matrices.xlsx"'})

    @r.get("/matrix/{name}")
    def matrix(name: str, format: str = Query("json", pattern="^(json|csv)$"),
               row: str | None = None, col: str | None = None, actor: str | None = None,
               state: str | None = None, gaps_only: bool = False, hide_empty: bool = False,
               project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        try:
            m = mx.build(s, name, **_filters(row, col, actor, state, gaps_only, hide_empty))
        except KeyError:
            raise HTTPException(404, f"Unknown matrix '{name}'. Options: {', '.join(mx.MATRICES)}")
        if format == "csv":
            return Response(mx.to_csv(m), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        return m

    @r.get("/matrices", response_class=HTMLResponse)
    def matrices_page():
        return HTMLResponse(_MATRIX_HTML)

    # ── Change management (Phase 2) ───────────────────────────────────────────
    def cr_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except KeyError as e:
            raise HTTPException(404, f"Change request {e} not found")
        except ValueError as e:
            raise HTTPException(400, str(e))

    def export(m: dict, fmt: str, filename: str):
        if fmt == "csv":
            return Response(mx.to_csv(m), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})
        return m

    @r.get("/cr")
    def cr_list(project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        return ch.list_crs(store_for(project_code, voxrail_session))

    @r.post("/cr")
    def cr_create(body: CRIn, project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        code = code_for(project_code, voxrail_session)
        s = get_store(code)
        # validate targets before creating anything
        for t in body.targets:
            cr_call(ch.resolve_target, s, t.target)
        return cr_call(ch.create_cr, s, code, body.title, body.description,
                       [t.model_dump() for t in body.targets])

    @r.get("/cr/{cr_code}")
    def cr_get(cr_code: str, project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        return cr_call(ch.get_cr, store_for(project_code, voxrail_session), cr_code)

    @r.post("/cr/{cr_code}/targets")
    def cr_add_target(cr_code: str, body: TargetIn, project_code: str | None = None,
                      voxrail_session: str | None = Cookie(default=None)):
        return cr_call(ch.add_target, store_for(project_code, voxrail_session), cr_code, body.target, body.change_type)

    @r.delete("/cr/{cr_code}/targets")
    def cr_remove_target(cr_code: str, node_key: str, project_code: str | None = None,
                         voxrail_session: str | None = Cookie(default=None)):
        return cr_call(ch.remove_target, store_for(project_code, voxrail_session), cr_code, node_key)

    @r.post("/cr/{cr_code}/status")
    def cr_status(cr_code: str, body: StatusIn, project_code: str | None = None,
                  voxrail_session: str | None = Cookie(default=None)):
        return cr_call(ch.set_status, store_for(project_code, voxrail_session), cr_code, body.status, body.note)

    def _live(s, cr_code, include_actor_coupling):
        targets = cr_call(ch.cr_targets, s, cr_code)
        if not targets:
            raise HTTPException(400, "Add at least one target to the change request first")
        imp = ch.impact(s, targets, include_actor_coupling)
        return imp, ch.verification_scope(s, imp)

    @r.get("/cr/{cr_code}/impact")
    def cr_impact(cr_code: str, include_actor_coupling: bool = False,
                  format: str = Query("json", pattern="^(json|csv|matrix)$"),
                  project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        imp, _ = _live(store_for(project_code, voxrail_session), cr_code, include_actor_coupling)
        if format == "json":
            return imp
        return export(ch.impact_matrix(imp), format, f"{cr_code}_impact")

    @r.get("/cr/{cr_code}/scope")
    def cr_scope(cr_code: str, include_actor_coupling: bool = False,
                 format: str = Query("json", pattern="^(json|csv)$"),
                 project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        _, scope = _live(store_for(project_code, voxrail_session), cr_code, include_actor_coupling)
        return export(scope, format, f"{cr_code}_verification_scope")

    @r.post("/cr/{cr_code}/assess")
    def cr_assess(cr_code: str, include_actor_coupling: bool = False, project_code: str | None = None,
                  voxrail_session: str | None = Cookie(default=None)):
        return cr_call(ch.assess, store_for(project_code, voxrail_session), cr_code, include_actor_coupling)

    @r.get("/cr/{cr_code}/assessment")
    def cr_assessment(cr_code: str, project_code: str | None = None,
                      voxrail_session: str | None = Cookie(default=None)):
        a = cr_call(ch.last_assessment, store_for(project_code, voxrail_session), cr_code)
        if not a:
            raise HTTPException(404, f"{cr_code} has not been assessed yet")
        return a

    @r.get("/cr/{cr_code}/report.xlsx")
    def cr_report(cr_code: str, frozen: bool = False, include_actor_coupling: bool = False,
                  project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        if frozen:
            a = cr_call(ch.last_assessment, s, cr_code)
            if not a:
                raise HTTPException(404, f"{cr_code} has not been assessed yet")
            imp, scope = a["impact"], a["scope"]
        else:
            imp, scope = _live(s, cr_code, include_actor_coupling)
        try:
            data = mx.to_xlsx([ch.impact_matrix(imp), scope, ch.out_of_scope_matrix(scope)])
        except ImportError:
            raise HTTPException(501, "Excel export needs openpyxl: pip install openpyxl (CSV export still works)")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{cr_code}_report.xlsx"'})

    @r.get("/impact")
    def what_if(target: list[str] = Query(...), include_actor_coupling: bool = False,
                project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        """What-if impact without a change request: /cm/impact?target=state:Ready to Drive"""
        s = store_for(project_code, voxrail_session)
        keys = [cr_call(ch.resolve_target, s, t) for t in target]
        imp = ch.impact(s, keys, include_actor_coupling)
        return {"impact": imp, "scope": ch.verification_scope(s, imp)}

    @r.get("/changes", response_class=HTMLResponse)
    def changes_page():
        return HTMLResponse(_CHANGES_HTML)

    # ── Train state catalogue ─────────────────────────────────────────────────
    @r.get("/states")
    def states(project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        return g.states_overview(store_for(project_code, voxrail_session))

    @r.post("/states")
    def add_states(body: CatalogueIn, project_code: str | None = None,
                   voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        s.add_catalogue("state", body.names)
        return g.states_overview(s)

    @r.get("/states/{name_or_id}")
    def state(name_or_id: str, project_code: str | None = None,
              voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        return g.state_view(s, entity_key(s, "state", name_or_id))

    # ── Entities, aliases and suggestions ─────────────────────────────────────
    @r.get("/entities")
    def entities(kind: str | None = None, project_code: str | None = None,
                 voxrail_session: str | None = Cookie(default=None)):
        if kind and kind not in ENTITY_KINDS:
            raise HTTPException(400, f"kind must be one of {ENTITY_KINDS}")
        return store_for(project_code, voxrail_session).entities(kind)

    @r.post("/entities/{eid}/confirm")
    def confirm(eid: int, project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        if not store_for(project_code, voxrail_session).confirm_entity(eid):
            raise HTTPException(404, "entity not found")
        return {"status": "confirmed", "id": eid}

    @r.post("/entities/{eid}/merge")
    def merge(eid: int, body: MergeIn, project_code: str | None = None,
              voxrail_session: str | None = Cookie(default=None)):
        try:
            store_for(project_code, voxrail_session).merge_entity(eid, body.into_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"status": "merged", "id": eid, "into_id": body.into_id}

    @r.get("/suggestions")
    def suggestions(project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        return store_for(project_code, voxrail_session).suggestions()

    @r.post("/suggestions/{sid}/{decision}")
    def decide(sid: int, decision: str, project_code: str | None = None,
               voxrail_session: str | None = Cookie(default=None)):
        if decision not in ("accept", "reject"):
            raise HTTPException(400, "decision must be accept or reject")
        try:
            return store_for(project_code, voxrail_session).resolve_suggestion(sid, decision == "accept")
        except ValueError as e:
            raise HTTPException(404, str(e))

    # ── Manual links ──────────────────────────────────────────────────────────
    @r.post("/links")
    def add_link(body: LinkIn, project_code: str | None = None, voxrail_session: str | None = Cookie(default=None)):
        s = store_for(project_code, voxrail_session)
        s.add_link(body.src, body.dst, body.kind, origin="manual")
        s.commit()
        return {"status": "added", **body.model_dump()}

    @r.delete("/links")
    def delete_link(body: LinkIn, project_code: str | None = None,
                    voxrail_session: str | None = Cookie(default=None)):
        if not store_for(project_code, voxrail_session).remove_link(body.src, body.dst, body.kind):
            raise HTTPException(404, "link not found")
        return {"status": "removed", **body.model_dump()}

    return r


_VIEW_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>VoxRail Configuration Graph</title>
<style>
 body{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
 nav a{margin-right:12px} .mermaid{background:#fff;padding:12px;border:1px solid #ddd;overflow:auto}
 pre{white-space:pre-wrap}
</style></head>
<body>
<h2>VoxRail configuration graph</h2>
<nav>Layer: <a href="?layer=1">1 · Use case level</a><a href="?layer=2">2 · Logical level</a>
<a href="?layer=both">Both</a> (showing: {{LAYER}}) · <a href="matrices">Matrices</a> · <a href="gaps">Gaps (JSON)</a> · <a href="states">States (JSON)</a></nav>
<div class="mermaid" id="graph">Rendering…</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>
 const SRC = {{MERMAID_JSON}};
 mermaid.initialize({startOnLoad:false, securityLevel:'strict'});
 mermaid.render('cmgraph', SRC).then(({svg}) => { document.getElementById('graph').innerHTML = svg; })
   .catch(e => { const el = document.getElementById('graph'); el.textContent = SRC; });
</script>
</body></html>"""


_MATRIX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>VoxRail Configuration Matrices</title>
<style>
 :root{--bg:#f7f8fa;--fg:#1d2330;--muted:#5b6475;--line:#d9dde5;--head:#1f3864;--hit:#e2efda;--gap:#fce4d6;--card:#fff}
 body{font-family:system-ui,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
 header{padding:14px 16px;background:var(--card);border-bottom:1px solid var(--line)}
 h1{font-size:18px;margin:0 0 10px}
 .tabs button{border:1px solid var(--line);background:var(--card);padding:6px 10px;margin:0 4px 4px 0;border-radius:6px;cursor:pointer}
 .tabs button.on{background:var(--head);color:#fff;border-color:var(--head)}
 form{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px;font-size:13px}
 input[type=text]{padding:5px 7px;border:1px solid var(--line);border-radius:5px;width:130px}
 .bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:10px 16px;font-size:13px;color:var(--muted)}
 .bar a{color:var(--head)}
 .wrap{overflow:auto;max-height:calc(100vh - 190px);margin:0 16px 16px;border:1px solid var(--line);background:var(--card)}
 table{border-collapse:separate;border-spacing:0;font-size:12px}
 th,td{border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:4px 6px;vertical-align:top;white-space:nowrap}
 thead th{position:sticky;top:0;background:var(--head);color:#fff;z-index:2;font-weight:600}
 thead th.v{writing-mode:vertical-rl;transform:rotate(180deg);max-height:180px;text-align:left}
 th.grp{font-size:10px;font-weight:400;opacity:.85}
 tbody th{position:sticky;left:0;background:var(--card);z-index:1;text-align:left}
 td.hit{background:var(--hit);text-align:center;font-weight:600}
 td.wide{white-space:normal;min-width:120px;text-align:left;font-weight:400}
 td.gapc{background:var(--gap)}
 tr.gap th,tr.gap td.note{background:var(--gap)}
 td.f{color:var(--muted);max-width:260px;overflow:hidden;text-overflow:ellipsis}
 .empty{padding:24px;color:var(--muted)}
</style></head>
<body>
<header>
 <h1>VoxRail configuration matrices</h1>
 <div class="tabs" id="tabs"></div>
 <form id="f">
  <label>Rows <input type="text" name="row" placeholder="e.g. UC-MP-00 or door"></label>
  <label>Columns <input type="text" name="col" placeholder="e.g. MVB"></label>
  <label>Actor <input type="text" name="actor"></label>
  <label>Train state <input type="text" name="state"></label>
  <label><input type="checkbox" name="gaps_only"> Gaps only</label>
  <label><input type="checkbox" name="hide_empty"> Hide empty</label>
  <button type="submit">Apply</button> <button type="reset">Clear</button>
 </form>
</header>
<div class="bar"><span id="legend"></span><span id="totals"></span>
 <a id="csv" href="#">Export CSV</a><a id="xlsx" href="#">Export Excel (all matrices)</a>
 <a href="view">Graph view</a> <a href="changes">Change requests</a></div>
<div class="wrap" id="out"><div class="empty">Loading…</div></div>
<script>
const MATS=[["uc_state","Use case × State"],["state_logic","State × Logic"],["uc_trace","Traceability"],["uc_uc","Use case × Use case"]];
const page=new URLSearchParams(location.search);
let cur=page.get("m")||"uc_state";
const f=document.getElementById("f");
for(const [k,v] of page) if(f.elements[k]) { if(f.elements[k].type==="checkbox") f.elements[k].checked=v==="true"; else f.elements[k].value=v; }
function qs(){const q=new URLSearchParams();
 if(page.get("project_code")) q.set("project_code",page.get("project_code"));
 for(const el of f.elements){ if(!el.name) continue;
  if(el.type==="checkbox"){ if(el.checked) q.set(el.name,"true"); } else if(el.value.trim()) q.set(el.name,el.value.trim()); }
 return q;}
function el(t,cls,txt){const e=document.createElement(t); if(cls) e.className=cls; if(txt!==undefined) e.textContent=txt; return e;}
function tabs(){const t=document.getElementById("tabs"); t.textContent="";
 for(const [k,v] of MATS){const b=el("button",k===cur?"on":"",v); b.type="button"; b.onclick=()=>{cur=k;load();}; t.appendChild(b);}}
async function load(){tabs(); const q=qs();
 const url=new URL(location.href); url.search=new URLSearchParams([...q,["m",cur]]).toString(); history.replaceState(null,"",url);
 document.getElementById("csv").href="matrix/"+cur+"?"+new URLSearchParams([...q,["format","csv"]]);
 document.getElementById("xlsx").href="matrix.xlsx?"+q;
 const out=document.getElementById("out");
 const r=await fetch("matrix/"+cur+"?"+q); if(!r.ok){out.textContent="Error: "+(await r.text()); return;}
 const m=await r.json();
 document.getElementById("legend").textContent=m.legend;
 document.getElementById("totals").textContent=m.totals.rows+" rows · "+m.totals.columns+" columns · "+m.totals.gaps+" gaps";
 out.textContent="";
 if(!m.rows.length){out.appendChild(el("div","empty","Nothing matches these filters yet.")); return;}
 const vertical=m.name!=="uc_trace", tbl=el("table"), th=el("thead"), h1=el("tr"), h2=el("tr");
 h1.appendChild(el("th","",m.name==="state_logic"?"Train state":"Use case")); h2.appendChild(el("th","grp",""));
 for(const fld of m.row_fields){h1.appendChild(el("th","",fld)); h2.appendChild(el("th","grp",""));}
 for(const c of m.columns){h1.appendChild(el("th",vertical?"v":"",c.label)); h2.appendChild(el("th","grp",c.detail?c.group+" · "+c.detail:c.group));}
 h1.appendChild(el("th","","Gap")); h2.appendChild(el("th","grp",""));
 th.appendChild(h1); th.appendChild(h2); tbl.appendChild(th);
 const tb=el("tbody");
 for(const row of m.rows){const tr=el("tr",row.gap?"gap":""); tr.appendChild(el("th","",row.label));
  for(const fld of m.row_fields){const v=row.fields[fld]; const td=el("td","f",v===undefined?"":String(v)); td.title=td.textContent; tr.appendChild(td);}
  for(const c of m.columns){const v=row.cells[c.key]||""; let cls=v?"hit":"";
   if(v && (m.name==="uc_trace"||m.name==="uc_uc")) cls+=" wide";
   if(!v && (row.gap_cols||[]).includes(c.key)) cls="gapc";
   tr.appendChild(el("td",cls,v));}
  tr.appendChild(el("td","note",row.gap_note||"")); tb.appendChild(tr);}
 tbl.appendChild(tb); out.appendChild(tbl);}
f.onsubmit=e=>{e.preventDefault(); load();};
f.onreset=()=>setTimeout(load,0);
load();
</script>
</body></html>"""


_CHANGES_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>VoxRail Change Management</title>
<style>
 :root{--bg:#f7f8fa;--fg:#1d2330;--muted:#5b6475;--line:#d9dde5;--head:#1f3864;--hit:#e2efda;--gap:#fce4d6;--card:#fff;--warn:#c00000}
 *{box-sizing:border-box}
 body{font-family:system-ui,sans-serif;margin:0;background:var(--bg);color:var(--fg);font-size:14px}
 header{padding:12px 16px;background:var(--card);border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
 h1{font-size:18px;margin:0} h2{font-size:15px;margin:0 0 8px} a{color:var(--head)}
 main{display:grid;grid-template-columns:300px 1fr;gap:16px;padding:16px}
 @media(max-width:800px){main{grid-template-columns:1fr}}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}
 .cr{padding:8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;cursor:pointer}
 .cr.on{border-color:var(--head);box-shadow:0 0 0 1px var(--head)}
 .cr small{color:var(--muted);display:block}
 .pill{display:inline-block;font-size:11px;padding:1px 6px;border-radius:9px;background:#e8ecf4;margin-left:4px}
 input,textarea,select{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:5px;width:100%}
 textarea{min-height:60px} label{display:block;margin:6px 0 2px;color:var(--muted);font-size:12px}
 button{font:inherit;padding:5px 10px;border-radius:5px;border:1px solid var(--head);background:var(--head);color:#fff;cursor:pointer}
 button.sec{background:var(--card);color:var(--head)}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
 .row>*{width:auto}
 .chips span{display:inline-block;margin:0 6px 6px 0;padding:4px 8px;border-radius:6px;background:#eef1f6;font-size:12px}
 .chips span.warn{background:var(--gap);color:var(--warn)}
 .tabs button{margin-right:4px}
 .wrap{overflow:auto;max-height:60vh;border:1px solid var(--line);margin-top:8px}
 table{border-collapse:separate;border-spacing:0;font-size:12px}
 th,td{border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:4px 6px;vertical-align:top}
 thead th{position:sticky;top:0;background:var(--head);color:#fff;z-index:2;white-space:nowrap}
 tbody th{position:sticky;left:0;background:var(--card);text-align:left;white-space:nowrap}
 td{max-width:320px;min-width:120px} td.hit{background:var(--hit)} td.gapc{background:var(--gap)}
 tr.gap th{background:var(--gap)} .muted{color:var(--muted)} .err{color:var(--warn)}
 ul.t{margin:4px 0;padding-left:18px}
</style></head>
<body>
<header><h1>VoxRail change management</h1>
 <a href="matrices">Matrices</a><a href="view">Graph view</a><span class="muted" id="proj"></span></header>
<main>
 <section>
  <div class="card"><h2>Change requests</h2><div id="list" class="muted">Loading…</div></div>
  <div class="card" style="margin-top:12px"><h2>New change request</h2>
   <form id="newcr">
    <label>Title</label><input name="title" required>
    <label>Description</label><textarea name="description"></textarea>
    <label>Changed items, one per line (UC-…, ED-…, state:Name, interface:Name, actor:Name)</label>
    <textarea name="targets" list="targets"></textarea>
    <div class="row"><button>Create</button><button type="button" class="sec" id="whatif">What-if only</button></div>
    <div id="newerr" class="err"></div>
   </form>
  </div>
 </section>
 <section class="card" id="detail"><p class="muted">Select or create a change request. "What-if only" shows the impact without saving a CR.</p></section>
</main>
<datalist id="tl"></datalist>
<script>
const page=new URLSearchParams(location.search), PC=page.get("project_code");
const q=(o={})=>{const p=new URLSearchParams(o); if(PC) p.set("project_code",PC); return p.toString();};
const el=(t,c,x)=>{const e=document.createElement(t); if(c) e.className=c; if(x!==undefined) e.textContent=x; return e;};
async function api(path,opt={},params={}){const r=await fetch(path+"?"+q(params),opt); const j=await r.json().catch(()=>({}));
 if(!r.ok) throw new Error(j.detail||r.statusText); return j;}
const post=(path,body,params)=>api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})},params);
let cur=page.get("cr"), tab="impact", actors=false;
if(PC) document.getElementById("proj").textContent="Project "+PC;

async function loadList(){const l=document.getElementById("list");
 try{const crs=await api("cr"); l.textContent=""; if(!crs.length) l.textContent="None yet.";
  for(const c of crs){const d=el("div","cr"+(c.code===cur?" on":"")); d.appendChild(el("b","",c.code));
   d.appendChild(el("span","pill",c.status)); if(c.origin==="variation_order") d.appendChild(el("span","pill","variation order"));
   d.appendChild(el("small","",c.title)); d.onclick=()=>{cur=c.code; show();}; l.appendChild(d);}
 }catch(e){l.textContent=e.message;}}

function table(m){const w=el("div","wrap"); if(!m.rows.length){w.appendChild(el("p","muted","Nothing here.")); return w;}
 const t=el("table"),h=el("thead"),tr=el("tr"); tr.appendChild(el("th","","Item"));
 for(const f of m.row_fields) tr.appendChild(el("th","",f)); for(const c of m.columns) tr.appendChild(el("th","",c.label));
 h.appendChild(tr); t.appendChild(h); const b=el("tbody");
 for(const r of m.rows){const x=el("tr",r.gap?"gap":""); x.appendChild(el("th","",r.label));
  for(const f of m.row_fields) x.appendChild(el("td","",r.fields[f]===undefined?"":String(r.fields[f])));
  for(const c of m.columns){const v=r.cells[c.key]||""; x.appendChild(el("td",(r.gap_cols||[]).includes(c.key)?"gapc":(v?"hit":""),v));}
  b.appendChild(x);} t.appendChild(b); w.appendChild(t); return w;}

function chips(scope,imp){const d=el("div","chips"), s=scope.summary;
 d.appendChild(el("span","",imp.items.length+" impacted items"));
 d.appendChild(el("span","",s.use_cases_in_scope+" use cases in scope, "+s.use_cases_out_of_scope+" out"));
 d.appendChild(el("span","","Re-run: "+(s.tests_to_rerun.join(", ")||"none")));
 if(s.new_tests_needed.length) d.appendChild(el("span","warn","New tests needed: "+s.new_tests_needed.join(", ")));
 if(s.safety_reviews.length) d.appendChild(el("span","warn","Safety re-review: "+s.safety_reviews.join(", ")));
 return d;}

function impactMatrix(imp){const cats=["Changed","Direct dependent","Coupled via state","Coupled via interface","Coupled via actor","Logical design","Indirect dependent"];
 return {row_fields:["Type","Title","Severity","Safety"],columns:cats.map(c=>({key:c,label:c})),
  rows:imp.items.map(i=>({label:i.label,fields:{Type:i.kind,Title:i.title,Severity:i.severity||"",Safety:i.safety?"Yes":""},cells:{[i.category]:i.reason},gap:i.missing}))};}
function oosMatrix(scope){return {row_fields:["Title","Justification"],columns:[],rows:scope.out_of_scope.map(o=>({label:o.label,fields:{Title:o.title,Justification:o.justification},cells:{}}))};}

function results(box,imp,scope,frozenNote){box.appendChild(chips(scope,imp));
 if(frozenNote) box.appendChild(el("p","muted",frozenNote));
 const tabs=el("div","tabs row"); for(const [k,v] of [["impact","Impact"],["scope","Verification scope"],["oos","Out of scope"]]){
  const b=el("button",k===tab?"":"sec",v); b.type="button"; b.onclick=()=>{tab=k; show();}; tabs.appendChild(b);} box.appendChild(tabs);
 box.appendChild(table(tab==="impact"?impactMatrix(imp):tab==="scope"?scope:oosMatrix(scope)));}

async function show(){loadList(); const d=document.getElementById("detail"); d.textContent="";
 const url=new URL(location.href); if(cur) url.searchParams.set("cr",cur); history.replaceState(null,"",url);
 if(!cur){d.appendChild(el("p","muted","Select or create a change request.")); return;}
 let cr; try{cr=await api("cr/"+cur);}catch(e){d.appendChild(el("p","err",e.message)); return;}
 d.appendChild(el("h2","",cr.code+" · "+cr.title)); if(cr.description) d.appendChild(el("p","muted",cr.description));
 const st=el("div","row"), sel=el("select"); for(const s of ["Draft","Open","Assessed","Approved","Implemented","Verified","Closed","Rejected"]){const o=el("option","",s); if(s===cr.status) o.selected=true; sel.appendChild(o);}
 sel.onchange=async()=>{await post("cr/"+cur+"/status",{status:sel.value}); show();};
 st.append(el("span","muted","Status"),sel); d.appendChild(st);
 d.appendChild(el("b","","Changed items")); const ul=el("ul","t");
 for(const t of cr.targets){const li=el("li","",t.label+" ("+t.kind+", "+t.change_type+") "); const x=el("a","","remove"); x.href="#";
  x.onclick=async e=>{e.preventDefault(); await api("cr/"+cur+"/targets",{method:"DELETE"},{node_key:t.key}); show();}; li.appendChild(x); ul.appendChild(li);}
 d.appendChild(ul);
 const add=el("div","row"), inp=el("input"); inp.placeholder="UC-…, state:Name, interface:Name"; inp.setAttribute("list","tl"); inp.style.width="260px";
 const ct=el("select"); for(const c of ["modify","add","remove"]) ct.appendChild(el("option","",c));
 const ab=el("button","sec","Add item"), er=el("span","err");
 ab.onclick=async()=>{try{await post("cr/"+cur+"/targets",{target:inp.value,change_type:ct.value}); show();}catch(e){er.textContent=e.message;}};
 add.append(inp,ct,ab,er); d.appendChild(add);
 const ctl=el("div","row"), cb=el("input"); cb.type="checkbox"; cb.checked=actors; cb.style.width="auto"; cb.onchange=()=>{actors=cb.checked; show();};
 const lab=el("label"); lab.style.display="inline"; lab.append(cb," Include coupling through shared actors");
 const as=el("button","","Assess and record"); as.onclick=async()=>{try{await post("cr/"+cur+"/assess",{}, {include_actor_coupling:actors}); show();}catch(e){alert(e.message);}};
 ctl.append(lab,as); d.appendChild(ctl);
 const ex=el("div","row"); const mk=(t,h)=>{const a=el("a","",t); a.href=h; return a;};
 ex.append(mk("Impact CSV","cr/"+cur+"/impact?"+q({format:"csv",include_actor_coupling:actors})),
  mk("Scope CSV","cr/"+cur+"/scope?"+q({format:"csv",include_actor_coupling:actors})),
  mk("Excel report (live)","cr/"+cur+"/report.xlsx?"+q({include_actor_coupling:actors})));
 if(cr.last_assessment) ex.append(mk("Excel report (recorded "+cr.last_assessment.created_at+")","cr/"+cur+"/report.xlsx?"+q({frozen:true})));
 d.appendChild(ex);
 if(!cr.targets.length){d.appendChild(el("p","muted","Add a changed item to see the impact.")); return;}
 try{const [imp,scope]=await Promise.all([api("cr/"+cur+"/impact",{}, {include_actor_coupling:actors}),api("cr/"+cur+"/scope",{}, {include_actor_coupling:actors})]);
  let note=""; if(cr.last_assessment){const a=await api("cr/"+cur+"/assessment"); note="Live view. Last recorded assessment: "+a.created_at+
   (a.artefacts_changed_since.length?" (changed since: "+a.artefacts_changed_since.join(", ")+")":" (nothing changed since)");}
  results(d,imp,scope,note);}catch(e){d.appendChild(el("p","err",e.message));}}

document.getElementById("newcr").onsubmit=async e=>{e.preventDefault(); const f=e.target, err=document.getElementById("newerr"); err.textContent="";
 const targets=f.elements.targets.value.split("\\n").map(s=>s.trim()).filter(Boolean).map(t=>({target:t}));
 try{const cr=await post("cr",{title:f.elements.title.value,description:f.elements.description.value,targets}); cur=cr.code; f.reset(); show();}catch(x){err.textContent=x.message;}};
document.getElementById("whatif").onclick=async()=>{const f=document.getElementById("newcr"), err=document.getElementById("newerr"); err.textContent="";
 const t=f.elements.targets.value.split("\\n").map(s=>s.trim()).filter(Boolean); if(!t.length){err.textContent="List at least one changed item."; return;}
 const p=new URLSearchParams(q({include_actor_coupling:actors})); for(const x of t) p.append("target",x);
 try{const r=await fetch("impact?"+p); const j=await r.json(); if(!r.ok) throw new Error(j.detail); cur=null;
  const d=document.getElementById("detail"); d.textContent=""; d.appendChild(el("h2","","What-if: "+t.join(", ")));
  results(d,j.impact,j.scope,"Not saved. Create the change request to record it.");}catch(x){err.textContent=x.message;}};

(async()=>{try{const [arts,ents]=await Promise.all([api("artefacts"),api("entities")]); const dl=document.getElementById("tl");
 for(const a of arts) dl.appendChild(Object.assign(el("option"),{value:a.ref}));
 for(const e of ents) if(e.kind!=="sw_element") dl.appendChild(Object.assign(el("option"),{value:e.kind+":"+e.name}));}catch(e){}})();
show();
</script>
</body></html>"""

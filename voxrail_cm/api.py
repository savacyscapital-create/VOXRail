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


def make_router(resolve_project_code: Callable[[str | None], str | None]) -> APIRouter:
    r = APIRouter(prefix="/cm", tags=["configuration-management"])

    def store_for(project_code: str | None, cookie: str | None):
        code = project_code or resolve_project_code(cookie)
        if not code:
            raise HTTPException(404, "No project: pass ?project_code= or start a VoxRail session first")
        return get_store(code)

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
 <a href="view">Graph view</a></div>
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

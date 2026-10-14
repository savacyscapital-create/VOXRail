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
VoxRail Web Server (app.py)  v3.0
===================================
FastAPI backend for the VoxRail Railway AI Assistant.

Features:
- Cookie-based session management (one session per browser tab)
- In-memory session store with TTL eviction
- Full artefact counter exposure: UC, TC, FMEA, HZ, ED, CC
- Error logging — failures written to JSON log too
- Startup LLM connectivity check
- /health endpoint
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Cookie
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn

from main import VoxRailSession, chat, log_interaction, PLUGINS

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("voxrail")

# ── Config ─────────────────────────────────────────────────────────────────────
SESSION_TTL_HOURS = int(os.getenv("VOXRAIL_SESSION_TTL", "24"))
COOKIE_MAX_AGE    = SESSION_TTL_HOURS * 3600


# ── Session store ──────────────────────────────────────────────────────────────
class SessionEntry:
    """Wraps VoxRailSession with a TTL timestamp."""
    def __init__(self, session: VoxRailSession):
        self.session     = session
        self.last_active = datetime.utcnow()

    def touch(self):
        self.last_active = datetime.utcnow()

    def is_expired(self) -> bool:
        return datetime.utcnow() - self.last_active > timedelta(hours=SESSION_TTL_HOURS)


SESSION_STORE: dict[str, SessionEntry] = {}


def evict_expired_sessions() -> int:
    expired = [sid for sid, e in SESSION_STORE.items() if e.is_expired()]
    for sid in expired:
        del SESSION_STORE[sid]
    if expired:
        logger.info("Evicted %d expired session(s)", len(expired))
    return len(expired)


def get_or_create_session(
    session_id:   str | None = None,
    project_name: str | None = None
) -> tuple[str, VoxRailSession]:
    evict_expired_sessions()

    if session_id and session_id in SESSION_STORE:
        entry   = SESSION_STORE[session_id]
        session = entry.session
        if project_name and project_name != session.project_name:
            logger.info("Project changed to '%s' — new session", project_name)
            session_id = str(uuid.uuid4())
            session    = VoxRailSession(project_name)
            SESSION_STORE[session_id] = SessionEntry(session)
        else:
            entry.touch()
        return session_id, session

    session_id = session_id or str(uuid.uuid4())
    name       = project_name or "Default Project"
    session    = VoxRailSession(name)
    SESSION_STORE[session_id] = SessionEntry(session)
    logger.info("New session %s | Project: '%s'", session_id[:8], name)
    return session_id, session


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("VoxRail v3.0 starting — checking LLM connectivity...")
    try:
        probe = VoxRailSession("probe")
        chat(probe, "Reply with exactly: OK")
        logger.info("✅ LLM connectivity confirmed")
    except Exception as e:
        logger.warning("⚠️  LLM probe failed: %s — server will start anyway", e)
    yield
    logger.info("VoxRail shutting down. Sessions in store: %d", len(SESSION_STORE))


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="VoxRail - Railway AI Assistant", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── Configuration management plugin routes (/cm) ──────────────────────────────
def project_code_for_cookie(session_id: str | None) -> str | None:
    entry = SESSION_STORE.get(session_id) if session_id else None
    return entry.session.project_code if entry else None


if any(getattr(p, "name", "") == "config_management" for p in PLUGINS):
    from voxrail_cm.api import make_router
    app.include_router(make_router(project_code_for_cookie))


# ── Models ─────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    content: str
    project: str | None = None


class ChatResponse(BaseModel):
    content:      str
    uc_counter:   int
    tc_counter:   int
    fmea_counter: int
    hz_counter:   int
    elec_counter: int
    code_counter: int
    sd_counter:   int
    train_type:   str | None
    project:      str


# ── Cookie helper ──────────────────────────────────────────────────────────────
def attach_session_cookie(response, session_id: str):
    response.set_cookie(
        key="voxrail_session", value=session_id,
        httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE
    )


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    voxrail_session: str | None = Cookie(default=None)
):
    session_id, session = get_or_create_session(voxrail_session)
    response = templates.TemplateResponse("index.html", {
        "request":      request,
        "project_name": session.project_name,
    })
    attach_session_cookie(response, session_id)
    return response


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    message: ChatRequest,
    voxrail_session: str | None = Cookie(default=None)
):
    session_id, session = get_or_create_session(voxrail_session, message.project)

    preview = message.content[:100] + ("…" if len(message.content) > 100 else "")
    logger.info("[%s] User: %s", session.project_code, preview)

    success = True
    try:
        response_text = chat(session, message.content)
    except Exception as e:
        logger.exception("LLM error in session %s", session_id[:8])
        response_text = f"⚠️ Sorry, an error occurred: {e}"
        success = False
    finally:
        log_interaction(session, message.content, response_text)

    if success:
        logger.info(
            "[%s] OK | UC=%d TC=%d FM=%d HZ=%d ED=%d CC=%d | history=%d",
            session.project_code,
            session.uc_counter, session.tc_counter,
            session.fmea_counter, session.hz_counter,
            session.elec_counter, session.code_counter,
            len(session.history)
        )

    payload = ChatResponse(
        content      = response_text,
        uc_counter   = session.uc_counter,
        tc_counter   = session.tc_counter,
        fmea_counter = session.fmea_counter,
        hz_counter   = session.hz_counter,
        elec_counter = session.elec_counter,
        code_counter = session.code_counter,
        sd_counter   = session.sd_counter,
        train_type   = session.train_type,
        project      = session.project_name,
    )
    json_response = JSONResponse(content=payload.model_dump())
    attach_session_cookie(json_response, session_id)
    return json_response


@app.get("/health")
async def health():
    return {
        "status":          "healthy",
        "sessions_active": len(SESSION_STORE),
        "session_ttl_hrs": SESSION_TTL_HOURS,
        "llm_model":       "llama-3.3-70b-versatile",
        "version":         "3.0",
        "artefacts":       ["UC", "TC", "SD", "FM", "HZ", "ED", "CC"],
        "plugins":         [getattr(p, "name", str(p)) for p in PLUGINS],
    }


@app.get("/session/info")
async def session_info(voxrail_session: str | None = Cookie(default=None)):
    if not voxrail_session or voxrail_session not in SESSION_STORE:
        return {"status": "no active session"}
    entry = SESSION_STORE[voxrail_session]
    info  = entry.session.summary()
    info["last_active"]        = entry.last_active.isoformat()
    info["session_store_size"] = len(SESSION_STORE)
    return info


@app.delete("/session/reset")
async def session_reset(voxrail_session: str | None = Cookie(default=None)):
    if voxrail_session and voxrail_session in SESSION_STORE:
        del SESSION_STORE[voxrail_session]
        logger.info("Session %s manually reset", voxrail_session[:8])
    response = JSONResponse({"status": "session cleared"})
    response.delete_cookie("voxrail_session")
    return response


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 VoxRail v3.0 Web Server")
    print("─" * 50)
    print("  Artefacts    → UC · TC · SD · FM · HZ · ED · CC")
    print("  UI           → http://127.0.0.1:8000")
    print("  Health       → http://127.0.0.1:8000/health")
    print("  Session info → http://127.0.0.1:8000/session/info")
    if PLUGINS:
        print("  Config graph → http://127.0.0.1:8000/cm/view")
    print(f"  Session TTL  → {SESSION_TTL_HOURS}h (VOXRAIL_SESSION_TTL to change)")
    print("─" * 50)

    dev_mode = os.getenv("VOXRAIL_DEV", "false").lower() == "true"
    if dev_mode:
        print("  ⚠️  Dev mode: reload=True — sessions reset on file change")
    print()

    uvicorn.run(
        "app:app",
        host="0.0.0.0", port=8000,
        reload=dev_mode, log_level="info"
    )

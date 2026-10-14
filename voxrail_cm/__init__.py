# =============================================================================
# VoxRail — Railway Systems Engineering AI
# Copyright © 2026 Savacys Capital. All rights reserved.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# for non-commercial use. See LICENSE for full terms.
# =============================================================================
"""
voxrail_cm — Configuration Management plugin for VoxRail
=========================================================
Links artefacts as users generate them, on two layers with the train state
as the hub (see store.py). Phase 1: linking, catalogue, graph views.

Hooks called by main.py:
  on_response(session, text)  -> link every artefact in an LLM response
  prompt_context(session)     -> catalogue text injected into the system prompt

Phase 2 (change.py): change requests, impact assessment across both layers,
verification scope. Variation orders flagged by VoxRail open a Draft CR.
"""

import logging

from . import change as _change
from . import graph as _graph
from .linker import ingest_response
from .parser import detect_types
from .store import get_store, store_exists

logger = logging.getLogger("voxrail.cm")


class ConfigManagementPlugin:
    name = "config_management"

    def on_response(self, session, text: str) -> None:
        if not detect_types(text) and "VARIATION ORDER" not in text:
            return
        store = get_store(session.project_code)
        results = ingest_response(store, text)
        for r in results:
            logger.info("[CM] %s linked (v%d)", r["ref"], r["version"])
        cr = _change.capture_variation_order(store, session.project_code, text, [r["ref"] for r in results])
        if cr:
            logger.info("[CM] Variation order captured as %s (Draft)", cr["code"])

    def prompt_context(self, session) -> str:
        if not store_exists(session.project_code):
            return ""
        return _graph.prompt_context(get_store(session.project_code))


plugin = ConfigManagementPlugin()

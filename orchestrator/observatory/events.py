"""SSE streams: the live log tail and debounced run-change events (plan U4).

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["events"])

"""Per-group reports and verdicts as the run wrote them (plan U8).

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["artifacts"])

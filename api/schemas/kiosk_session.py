"""Response model for minting a scoped kiosk (child capture/results) token.

See ``services/kiosk_session.py`` for the token itself and
``routers/kiosk_session.py`` for the mint endpoint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ChildSessionResponse(BaseModel):
    """The signed kiosk token plus the metadata the caller needs to use it.

    ``token`` is presented on subsequent kiosk requests via the
    ``X-Child-Session`` header (never ``Authorization: Bearer`` — the two
    credential planes are kept physically separate).
    """

    token: str
    scope: Literal["capture", "results"]
    expires_at: datetime

"""Route guard: refuse (in CONSENT_MODE=enforce) to process data the farmer has not agreed to.

    @router.post("/plots", dependencies=[Depends(require_consent("service"))])
"""

from fastapi import Depends, HTTPException

from app.core import metrics
from app.core.auth import current_user
from app.core.config import get_settings
from app.models import User
from app.services import consent


def require_consent(purpose: str):
    def dependency(user: User = Depends(current_user)) -> None:
        mode = get_settings().consent_mode
        if mode == "off":
            return
        given = consent.has(user, purpose)
        metrics.CONSENT.labels(purpose, "given" if given else "missing").inc()
        if not given and mode == "enforce":
            # The app recognises this message and shows the privacy notice.
            raise HTTPException(403, f"Consent required: {purpose}")

    return dependency

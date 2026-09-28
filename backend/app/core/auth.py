"""Authentication: Firebase ID tokens in production, an X-Dev-User header for local development."""

import hmac

from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.db import get_db
from app.models import User

_firebase_ready = False


def ensure_firebase() -> None:
    """Initialise the Firebase Admin app once (raises if credentials are missing/invalid)."""
    global _firebase_ready
    if _firebase_ready:
        return
    import firebase_admin
    from firebase_admin import credentials

    path = get_settings().firebase_credentials_path
    firebase_admin.initialize_app(credentials.Certificate(path) if path else None)
    _firebase_ready = True


def _verify_firebase(token: str) -> str:
    from firebase_admin import auth

    ensure_firebase()
    return auth.verify_id_token(token)["uid"]


def _verify_app_check(token: str) -> None:
    from firebase_admin import app_check

    ensure_firebase()
    app_check.verify_token(token)


def check_app_check(token: str | None) -> None:
    """Firebase App Check (see APP_CHECK_MODE): is this request from the genuine, unmodified app?"""
    mode = get_settings().app_check_mode
    if mode == "off":
        return
    result = "missing"
    if token:
        try:
            _verify_app_check(token)
            result = "valid"
        except Exception:  # noqa: BLE001 - expired, forged, wrong project, or Firebase unreachable
            result = "invalid"
    metrics.APP_CHECK.labels(result).inc()
    if result != "valid" and mode == "enforce":
        raise HTTPException(403, "This app could not be verified. Update Agro Nova from the official store.")


def current_user(
    authorization: str | None = Header(None),
    x_dev_user: str | None = Header(None),
    x_firebase_appcheck: str | None = Header(None),
    db: Session = Depends(get_db),
) -> User:
    settings = get_settings()
    if settings.auth_mode == "firebase":
        check_app_check(x_firebase_appcheck)
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        try:
            uid = _verify_firebase(authorization.split(" ", 1)[1])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(401, "Invalid token") from exc
    else:
        if not x_dev_user:
            raise HTTPException(401, "Send X-Dev-User header in AUTH_MODE=dev")
        uid = x_dev_user

    user = db.get(User, uid)
    if user is None:
        user = User(uid=uid, country=settings.default_country)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # The app fires several requests at start-up; a parallel one created this user first.
            db.rollback()
            user = db.get(User, uid)
            if user is None:
                raise
    return user


def _is_admin_key(candidate: str | None) -> bool:
    return candidate is not None and hmac.compare_digest(candidate.encode(), get_settings().admin_api_key.encode())


def require_admin(x_api_key: str | None = Header(None)) -> None:
    if not _is_admin_key(x_api_key):
        raise HTTPException(403, "Admin key required")


def _verify_task_oidc(token: str) -> dict:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    s = get_settings()
    return id_token.verify_oauth2_token(token, google_requests.Request(), audience=s.task_audience or s.task_target_url)


def require_task_caller(x_api_key: str | None = Header(None), authorization: str | None = Header(None)) -> None:
    """Who may run a queued task: an operator with the admin key, or Cloud Tasks itself.

    Cloud Tasks signs each call with an OIDC token for TASK_SERVICE_ACCOUNT. The token is checked against Google's
    public keys, must be for this service (audience), and must be a verified token of exactly that account.
    """
    bearer = authorization.split(" ", 1)[1] if authorization and authorization.lower().startswith("bearer ") else None
    if _is_admin_key(x_api_key) or _is_admin_key(bearer):
        return
    s = get_settings()
    if bearer and s.task_service_account:
        try:
            claims = _verify_task_oidc(bearer)
        except Exception:  # noqa: BLE001 - expired, wrong audience, forged...
            claims = None
        if claims and claims.get("email_verified") and hmac.compare_digest(
                str(claims.get("email", "")).encode(), s.task_service_account.encode()):
            return
    raise HTTPException(403, "Admin key or a Cloud Tasks token required")


def require_admin_or_bearer(x_api_key: str | None = Header(None), authorization: str | None = Header(None)) -> None:
    """The admin key as `X-API-Key`, or as `Authorization: Bearer <key>` (what Prometheus' `authorization` sends)."""
    bearer = authorization.split(" ", 1)[1] if authorization and authorization.lower().startswith("bearer ") else None
    if not (_is_admin_key(x_api_key) or _is_admin_key(bearer)):
        raise HTTPException(403, "Admin key required")

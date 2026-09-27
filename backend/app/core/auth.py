"""Authentication: Firebase ID tokens in production, an X-Dev-User header for local development."""

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

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


def current_user(
    authorization: str | None = Header(None),
    x_dev_user: str | None = Header(None),
    db: Session = Depends(get_db),
) -> User:
    settings = get_settings()
    if settings.auth_mode == "firebase":
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
        db.commit()
    return user


def require_admin(x_api_key: str | None = Header(None)) -> None:
    if x_api_key != get_settings().admin_api_key:
        raise HTTPException(403, "Admin key required")

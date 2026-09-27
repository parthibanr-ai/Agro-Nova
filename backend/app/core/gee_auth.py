"""Earth Engine initialization.

Failures are logged and exposed via /health instead of crashing startup, so the API stays usable
(with Open-Meteo fallback) before GEE credentials are configured.
"""

import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_initialized = False
_init_error: str | None = None


def init_earth_engine() -> None:
    global _initialized, _init_error
    if _initialized:
        return
    settings = get_settings()
    if not settings.gee_cloud_project:
        _init_error = "GEE_CLOUD_PROJECT not set; using fallback climate provider."
        return
    try:
        import ee

        if settings.ee_auth_mode == "service_account":
            if not settings.gee_service_account_email or not settings.gee_service_account_key_path:
                raise RuntimeError("service_account mode needs GEE_SERVICE_ACCOUNT_EMAIL and _KEY_PATH")
            creds = ee.ServiceAccountCredentials(
                settings.gee_service_account_email, settings.gee_service_account_key_path
            )
            ee.Initialize(creds, project=settings.gee_cloud_project)
        else:
            ee.Initialize(project=settings.gee_cloud_project)
        _initialized = True
        _init_error = None
        logger.info("Earth Engine initialized (mode=%s)", settings.ee_auth_mode)
    except Exception as exc:  # noqa: BLE001 - intentionally broad, surfaced via /health
        _init_error = str(exc)
        logger.warning("Earth Engine initialization failed: %s", exc)


def is_earth_engine_ready() -> bool:
    return _initialized


def earth_engine_init_error() -> str | None:
    return _init_error

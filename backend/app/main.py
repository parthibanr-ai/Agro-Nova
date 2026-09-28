import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import get_settings
from app.core.gee_auth import init_earth_engine
from app.db import init_db
from app.services.translation import localize_payload

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_earth_engine()
    yield


app = FastAPI(title="AgriN API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _localize(body: bytes, lang: str):
    return localize_payload(json.loads(body), lang)


@app.middleware("http")
async def localize_json(request: Request, call_next):
    """Translate human-readable JSON strings to the caller's language (?lang= or Accept-Language)."""
    response = await call_next(request)
    lang = request.query_params.get("lang") or request.headers.get("accept-language", "").split(",")[0].split(";")[0]
    if (
        not lang
        or lang.lower().startswith("en")
        or not response.headers.get("content-type", "").startswith("application/json")
        or request.url.path.startswith(("/docs", "/openapi"))
    ):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    # Translation makes blocking HTTP calls; run it in a worker thread so the event loop keeps serving others.
    translated = await run_in_threadpool(_localize, body, lang)
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")}
    headers["Content-Language"] = lang
    return Response(json.dumps(translated, ensure_ascii=False), status_code=response.status_code,
                    headers=headers, media_type="application/json")


app.include_router(router)

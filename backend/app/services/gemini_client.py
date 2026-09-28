"""Shared Gemini call used by diagnosis, crop recommendation and personalised advice.

Gemini intermittently answers 503 "high demand" (or 429) for a given model. Retry briefly, then fall back to
GEMINI_FALLBACK_MODEL, so a short spike does not fail the farmer's request. Other errors (bad key, bad request)
are raised immediately.
"""

import json
import logging
import time

from app.core.config import get_settings

logger = logging.getLogger(__name__)

ATTEMPTS_PER_MODEL = 2
RETRY_DELAY_S = 1.5


def generate_json(contents: list, temperature: float) -> dict:
    from google import genai
    from google.genai import errors, types

    s = get_settings()
    if s.gemini_use_vertex:
        client = genai.Client(vertexai=True, project=s.gee_cloud_project, location=s.gcp_location)
    else:
        client = genai.Client(api_key=s.gemini_api_key)
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=temperature)

    models = [s.gemini_model]
    if s.gemini_fallback_model and s.gemini_fallback_model not in models:
        models.append(s.gemini_fallback_model)

    last: Exception | None = None
    for model in models:
        for attempt in range(ATTEMPTS_PER_MODEL):
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                return json.loads(resp.text)
            except errors.APIError as e:
                if e.code not in (429, 500, 503, 504):
                    raise
                last = e
                logger.warning("Gemini %s returned %s (attempt %d)", model, e.code, attempt + 1)
                time.sleep(RETRY_DELAY_S)
    assert last is not None
    raise last

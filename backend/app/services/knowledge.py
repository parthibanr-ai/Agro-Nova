"""Loads the curated JSON knowledge base (countries, crops, remedies, schemes, ...)."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent.parent / "data"


@lru_cache
def load(name: str) -> Any:
    return json.loads((_DATA / f"{name}.json").read_text(encoding="utf-8"))


def countries() -> dict[str, dict]:
    return load("countries")


def country(code: str) -> dict:
    c = countries().get(code.upper())
    if c is None:
        raise KeyError(f"Unsupported country '{code}'. Supported: {sorted(countries())}")
    return c


def crops() -> dict[str, dict]:
    return load("crops")


def crop(crop_id: str) -> dict:
    c = crops().get(crop_id)
    if c is None:
        raise KeyError(f"Unknown crop '{crop_id}'. Supported: {sorted(crops())}")
    return c

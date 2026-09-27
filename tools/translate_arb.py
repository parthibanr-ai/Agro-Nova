"""Generate ARB files for the remaining languages with Google Cloud Translation.

Hand-written: en, hi, ta, pt, ru, zh. Everything else (the other Indian scheduled languages) is machine-
translated from app_en.arb and MUST be reviewed by a native speaker / agronomy translator before release
-- farm advice needs precise wording.

    set TRANSLATE_API_KEY=...        (PowerShell: $env:TRANSLATE_API_KEY="...")
    python tools/translate_arb.py            # writes app/lib/l10n/app_<code>.arb for missing languages
    python tools/translate_arb.py --force    # regenerate machine-translated files

Languages Cloud Translation does not cover (see backend/app/core/languages.py) are skipped; the app falls
back to Hindi/Urdu/English for those (see app/lib/core/locales.dart).
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.core.languages import LANGUAGES  # noqa: E402

L10N = ROOT / "app" / "lib" / "l10n"
HAND_WRITTEN = {"en", "hi", "ta", "pt-BR", "ru", "zh-CN"}
API = "https://translation.googleapis.com/language/translate/v2"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def translate(texts: list[str], target: str, key: str) -> list[str]:
    # Protect ICU placeholders like {count} from being altered.
    protected = [PLACEHOLDER.sub(lambda m: f'<x id="{m.group(1)}"/>', t) for t in texts]
    r = httpx.post(API, params={"key": key}, timeout=60,
                   json={"q": protected, "source": "en", "target": target, "format": "html"})
    r.raise_for_status()
    out = [t["translatedText"] for t in r.json()["data"]["translations"]]
    return [re.sub(r'<x id="(\w+)"\s*/?>(</x>)?', r"{\1}", t) for t in out]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    key = os.environ.get("TRANSLATE_API_KEY")
    if not key:
        sys.exit("Set TRANSLATE_API_KEY first.")

    source = json.loads((L10N / "app_en.arb").read_text(encoding="utf-8"))
    keys = [k for k in source if not k.startswith("@")]
    texts = [source[k] for k in keys]

    for lang in LANGUAGES:
        if lang.code in HAND_WRITTEN or not lang.machine_translation:
            continue
        path = L10N / f"app_{lang.code.replace('-', '_')}.arb"
        if path.exists() and not args.force:
            continue
        translated = translate(texts, lang.google_code, key)
        arb = {"@@locale": lang.code.replace("-", "_")} | dict(zip(keys, translated, strict=True))
        path.write_text(json.dumps(arb, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("wrote", path.name)


if __name__ == "__main__":
    main()

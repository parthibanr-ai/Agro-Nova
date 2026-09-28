"""Language registry: the 22 Indian scheduled languages + BRIC national languages.

`google_code` is the Cloud Translation code. `machine_translation=False` marks languages where
Cloud Translation coverage is uncertain -- content falls back to `fallback` until verified against
the current supported-language list (https://cloud.google.com/translate/docs/languages).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    native_name: str
    group: str  # "IN" | "BR" | "RU" | "CN" | "GLOBAL"
    google_code: str
    machine_translation: bool = True
    fallback: str = "en"


LANGUAGES: list[Language] = [
    Language("en", "English", "English", "GLOBAL", "en"),
    # --- India: 22 scheduled languages (Eighth Schedule) ---
    Language("as", "Assamese", "অসমীয়া", "IN", "as"),
    Language("bn", "Bengali", "বাংলা", "IN", "bn"),
    Language("brx", "Bodo", "बर'", "IN", "brx", False, "hi"),
    Language("doi", "Dogri", "डोगरी", "IN", "doi"),
    Language("gu", "Gujarati", "ગુજરાતી", "IN", "gu"),
    Language("hi", "Hindi", "हिन्दी", "IN", "hi"),
    Language("kn", "Kannada", "ಕನ್ನಡ", "IN", "kn"),
    Language("ks", "Kashmiri", "کٲشُر", "IN", "ks", False, "ur"),
    Language("kok", "Konkani", "कोंकणी", "IN", "gom"),
    Language("mai", "Maithili", "मैथिली", "IN", "mai"),
    Language("ml", "Malayalam", "മലയാളം", "IN", "ml"),
    Language("mni", "Manipuri (Meitei)", "মৈতৈলোন্", "IN", "mni-Mtei"),
    Language("mr", "Marathi", "मराठी", "IN", "mr"),
    Language("ne", "Nepali", "नेपाली", "IN", "ne"),
    Language("or", "Odia", "ଓଡ଼ିଆ", "IN", "or"),
    Language("pa", "Punjabi", "ਪੰਜਾਬੀ", "IN", "pa"),
    Language("sa", "Sanskrit", "संस्कृतम्", "IN", "sa", False, "hi"),
    Language("sat", "Santali", "ᱥᱟᱱᱛᱟᱲᱤ", "IN", "sat", False, "hi"),
    Language("sd", "Sindhi", "سنڌي", "IN", "sd"),
    Language("ta", "Tamil", "தமிழ்", "IN", "ta"),
    Language("te", "Telugu", "తెలుగు", "IN", "te"),
    Language("ur", "Urdu", "اردو", "IN", "ur"),
    # --- Brazil / Russia / China (India's Hindi/English already above) ---
    Language("pt-BR", "Portuguese (Brazil)", "Português (Brasil)", "BR", "pt"),
    Language("ru", "Russian", "Русский", "RU", "ru"),
    Language("zh-CN", "Chinese (Simplified)", "简体中文", "CN", "zh-CN"),
]

_BY_CODE = {lang.code.lower(): lang for lang in LANGUAGES}

# Languages Gemini is asked to write farm advice in directly, which is cheaper than English text plus a paid
# translation and reads more naturally. Deliberately a short list of widely-used languages Gemini handles well; the
# rest (Dogri, Konkani, Maithili, Manipuri, Sindhi, Bodo, Kashmiri, Sanskrit, Santali) keep the English-then-translate
# path until a native speaker has checked Gemini's output in that language. Extend after review.
GEMINI_AUTHORED = {"hi", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa", "ur", "or", "as", "ne", "pt-br", "ru", "zh-cn"}


def authoring_language(code: str | None) -> "Language | None":
    """The language Gemini should write advice in for this request, or None to write English (and translate)."""
    lang = get_language(code)
    return lang if lang.code.lower() in GEMINI_AUTHORED else None


def get_language(code: str | None) -> Language:
    """Resolve a (possibly region-tagged) code; unknown codes resolve to English."""
    if not code:
        return _BY_CODE["en"]
    c = code.strip().lower()
    if c in _BY_CODE:
        return _BY_CODE[c]
    base = c.split("-")[0]
    if base in _BY_CODE:
        return _BY_CODE[base]
    # bare 'pt' / 'zh' -> the regional variant we ship ('pt-BR' / 'zh-CN')
    return next((lang for code, lang in _BY_CODE.items() if code.split("-")[0] == base), _BY_CODE["en"])

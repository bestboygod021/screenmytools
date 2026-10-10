"""Minimal in-app internationalisation (Qt-free).

A tiny key -> string catalogue plus a current-language switch. ``tr(key)``
returns the active language's text, falling back to English and finally to the
key itself, so a missing translation never crashes the UI.
"""

from __future__ import annotations

SUPPORTED = ("en", "fa")

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "app_subtitle": "Automated full-page website screenshots, saved the way you want them.",
        "install_browser": "Install browser",
        "theme_light": "Light mode",
        "theme_dark": "Dark mode",
        "check_updates": "Check updates",
        "history": "History",
        "walk": "Screens",
        "language": "Language",
        "start": "Start Capture",
        "stop": "Stop",
        "open_folder": "Open folder",
        "pdf_report": "PDF report",
        "ready_hint": "Paste your URLs, pick a folder, then press Start Capture.",
        "urls_title": "URLs",
        "clicks_title": "Clicks & screens",
        "profiles_title": "Profiles",
        "alerts_title": "Change alerts",
        "network_title": "Network & session",
        "destination_title": "Destination",
        "options_title": "Capture options",
        "credentials_title": "Credentials",
        "advanced_title": "Advanced",
    },
    "fa": {
        "app_subtitle": "اسکرین‌شات تمام‌صفحه‌ی خودکار از وب‌سایت‌ها، دقیقاً همان‌طور که می‌خواهید ذخیره می‌شود.",
        "install_browser": "نصب مرورگر",
        "theme_light": "حالت روشن",
        "theme_dark": "حالت تیره",
        "check_updates": "بررسی به‌روزرسانی",
        "history": "تاریخچه",
        "walk": "صفحه‌ها",
        "language": "زبان",
        "start": "شروع ثبت",
        "stop": "توقف",
        "open_folder": "بازکردن پوشه",
        "pdf_report": "گزارش PDF",
        "ready_hint": "نشانی‌ها را بچسبانید، یک پوشه انتخاب کنید، سپس «شروع ثبت» را بزنید.",
        "urls_title": "نشانی‌ها",
        "clicks_title": "کلیک‌ها و صفحه‌ها",
        "profiles_title": "پروفایل‌ها",
        "alerts_title": "هشدار تغییرات",
        "network_title": "شبکه و نشست",
        "destination_title": "مقصد",
        "options_title": "گزینه‌های ثبت",
        "credentials_title": "اعتبارنامه‌ها",
        "advanced_title": "پیشرفته",
    },
}

_state = {"lang": "en"}


def set_language(lang: str) -> None:
    _state["lang"] = lang if lang in SUPPORTED else "en"


def current_language() -> str:
    return _state["lang"]


def is_rtl() -> bool:
    return _state["lang"] == "fa"


def tr(key: str) -> str:
    """Translate ``key`` for the active language (English, then key fallback)."""
    lang = _state["lang"]
    return _STRINGS.get(lang, {}).get(key) or _STRINGS["en"].get(key, key)

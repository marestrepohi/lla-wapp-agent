"""Validation helpers for the minimal context attached to WhatsApp templates."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Literal

MAX_CALLBACK_DATA_BYTES = 512
_ALLOWED_CALLBACK_FIELDS = {"v", "flow", "contact_name", "opening_message"}


def _short_text(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_length:
        return None
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        return None
    return normalized


def parse_opening_callback_data(value: str) -> dict[str, str] | None:
    """Parse only the allowlisted contact-confirmation context from Meta status events."""
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_CALLBACK_DATA_BYTES:
        return None
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _ALLOWED_CALLBACK_FIELDS:
        return None
    if str(payload.get("v")) != "1" or payload.get("flow") != "contact_confirmation":
        return None
    contact_name = _short_text(payload.get("contact_name"), max_length=60)
    opening_message = _short_text(payload.get("opening_message"), max_length=180)
    if not contact_name or not opening_message:
        return None
    return {
        "v": "1",
        "flow": "contact_confirmation",
        "contact_name": contact_name,
        "opening_message": opening_message,
    }


def classify_contact_confirmation(
    text: str, contact_name: str | None = None
) -> Literal["yes", "no", "unclear"]:
    """Classify concise Spanish contact replies; negative answers take precedence."""
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    if not normalized:
        return "unclear"

    negative_patterns = (
        r"\bno soy\b",
        r"\bno es\b",
        r"\bnumero equivocado\b",
        r"\bpersona equivocada\b",
        r"\bse equivoco(?:n)?\b",
        r"\bya no vive\b",
        r"\bno lo conozco\b",
        r"\bno la conozco\b",
        r"^(?:no|nop|nope|negativo)$",
    )
    if any(re.search(pattern, normalized) for pattern in negative_patterns):
        return "no"

    normalized_name = ""
    if contact_name:
        normalized_name = unicodedata.normalize("NFKD", contact_name.casefold())
        normalized_name = "".join(
            char for char in normalized_name if not unicodedata.combining(char)
        )
        normalized_name = re.sub(r"[^a-z0-9]+", " ", normalized_name).strip()
    if normalized_name and normalized in {
        normalized_name,
        f"soy {normalized_name}",
        f"yo soy {normalized_name}",
        f"si soy {normalized_name}",
        f"si {normalized_name}",
    }:
        return "yes"

    affirmative_patterns = (
        r"^(?:si|claro|correcto|asi es|afirmativo|soy yo|yo soy|habla|con el|con ella)$",
        r"\bsoy yo\b",
        r"\bsi soy\b",
        r"\bsi habla\b",
        r"\bcon el habla\b",
        r"\bcon ella habla\b",
        r"\bsi correcto\b",
        r"\basi es\b",
    )
    if any(re.search(pattern, normalized) for pattern in affirmative_patterns):
        return "yes"
    return "unclear"

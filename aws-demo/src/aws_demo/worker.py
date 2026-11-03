"""SQS Lambda consumer used to show the asynchronous architecture boundary."""

from __future__ import annotations

import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _valid_envelope(body: str) -> bool:
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("message_id"), str)
        and isinstance(payload.get("sender_id"), str)
        and isinstance(payload.get("message"), dict)
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    """Validate queue envelopes without logging their content or pretending to run the agent."""
    del context
    failures: list[dict[str, str]] = []
    records = event.get("Records", [])
    if not isinstance(records, list):
        return {"batchItemFailures": []}

    processed = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        item_identifier = record.get("messageId")
        body = record.get("body")
        if not isinstance(item_identifier, str) or not isinstance(body, str):
            continue
        if not _valid_envelope(body):
            failures.append({"itemIdentifier": item_identifier})
            continue
        processed += 1

    if processed:
        _LOGGER.info(
            "Architecture demo consumed %d event(s); business-agent processing is not attached",
            processed,
        )
    return {"batchItemFailures": failures}

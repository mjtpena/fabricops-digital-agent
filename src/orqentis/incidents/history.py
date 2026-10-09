"""Search for related historical incidents and detect recurring patterns (deterministic)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from orqentis.domain.incidents import Incident, RelatedIncidentMatch
from orqentis.incidents.classification import ErrorClassifier
from orqentis.storage.incidents import IncidentQuery, IncidentRepository

_NOISE = re.compile(r"(\"[^\"]*\"|'[^']*'|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b0x[0-9a-f]+\b|\d+)", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z][a-z_]{2,}")


def error_tokens(text: str) -> set[str]:
    """Normalised error 'shape': quoted values, ids and numbers removed."""
    return set(_TOKEN.findall(_NOISE.sub(" ", text.lower())))


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class RecurringPattern(BaseModel):
    item_id: str
    category: str
    occurrences: list[str] = Field(description="Incident ids, oldest first")
    weekday: str
    local_window: str
    timezone: str
    statement: str


class IncidentHistory:
    def __init__(self, repository: IncidentRepository, classifier: ErrorClassifier, *, timezone: str) -> None:
        self._repo = repository
        self._classifier = classifier
        self._tz = ZoneInfo(timezone)

    async def search_related(
        self,
        *,
        primary_item_id: str,
        before: datetime,
        exclude_incident_id: str | None = None,
        category_hint: str | None = None,
        error_text: str | None = None,
        limit: int = 5,
        min_similarity: float = 0.35,
    ) -> list[RelatedIncidentMatch]:
        candidates = [
            i
            for i in await self._repo.list(IncidentQuery(limit=1000))
            if i.incident_id != exclude_incident_id and i.detected_at < before
        ]
        current_tokens = error_tokens(error_text or "")
        current_sig = self._classifier.classify(error_text or "")
        matches: list[RelatedIncidentMatch] = []
        for other in candidates:
            score = 0.0
            reasons: list[str] = []
            if other.primary_item_id == primary_item_id:
                score += 0.4
                reasons.append("same primary item")
            if category_hint and other.category == category_hint:
                score += 0.3
                reasons.append(f"same category {other.category}")
            other_text = " ".join(s.description for s in other.symptoms)
            other_sig = self._classifier.classify(other_text) if other_text else None
            if current_sig and other_sig and other_sig.signature_id == current_sig.signature_id:
                score += 0.2
                reasons.append(f"same error signature {current_sig.signature_id}")
            similarity = jaccard(current_tokens, error_tokens(other_text))
            if similarity > 0:
                score += 0.1 * similarity
                reasons.append(f"error text similarity {similarity:.2f}")
            score = min(score, 1.0)
            if score >= min_similarity:
                matches.append(
                    RelatedIncidentMatch(
                        incident_id=other.incident_id,
                        title=other.title,
                        category=other.category,
                        detected_at=other.detected_at,
                        status=other.status,
                        similarity=round(score, 3),
                        reasons=reasons,
                        resolved_with_runbook_id=other.resolved_with_runbook_id,
                        resolution_summary=other.resolution_summary,
                    )
                )
        matches.sort(key=lambda m: (-m.similarity, -m.detected_at.timestamp()))
        return matches[:limit]

    async def recurring_pattern(
        self,
        incident: Incident,
        *,
        category: str,
        lookback: timedelta = timedelta(days=35),
        window_minutes: int = 45,
        min_occurrences: int = 3,
    ) -> RecurringPattern | None:
        """Same item + category on the same weekday at a similar local time, repeatedly.

        ``incident`` is the current occurrence (its own category may not be final yet).
        """
        local_now = incident.detected_at.astimezone(self._tz)
        history = [
            i
            for i in await self._repo.list(IncidentQuery(primary_item_id=incident.primary_item_id, limit=200))
            if i.incident_id != incident.incident_id
            and i.category == category
            and incident.detected_at - lookback <= i.detected_at < incident.detected_at
        ]
        same_slot: list[Incident] = []
        minute_of_day = local_now.hour * 60 + local_now.minute
        for other in history:
            local = other.detected_at.astimezone(self._tz)
            if (
                local.isoweekday() == local_now.isoweekday()
                and abs(local.hour * 60 + local.minute - minute_of_day) <= window_minutes
            ):
                same_slot.append(other)
        occurrences = sorted([*same_slot, incident], key=lambda i: i.detected_at)
        if len(occurrences) < min_occurrences:
            return None
        times = sorted(i.detected_at.astimezone(self._tz).strftime("%H:%M") for i in occurrences)
        weekday = local_now.strftime("%A")
        window = f"{times[0]} and {times[-1]}"
        gaps = [(b.detected_at - a.detected_at).days for a, b in pairwise(occurrences)]
        cadence = "consecutive " if all(6 <= g <= 8 for g in gaps) else ""
        return RecurringPattern(
            item_id=incident.primary_item_id,
            category=category,
            occurrences=[i.incident_id for i in occurrences],
            weekday=weekday,
            local_window=window,
            timezone=str(self._tz),
            statement=(
                f"{incident.primary_item_name or incident.primary_item_id} has failed with {category} on "
                f"{len(occurrences)} {cadence}{weekday}s between {window} ({self._tz})"
            ),
        )

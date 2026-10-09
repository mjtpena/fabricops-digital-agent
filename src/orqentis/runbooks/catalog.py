"""Runbook retrieval. The agent retrieves procedures from here; it never invents them."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from orqentis.domain.common import ItemType
from orqentis.domain.runbooks import Runbook, RunbookMatch
from orqentis.runbooks.loader import load_runbooks

CATEGORY_WEIGHT = 0.5
SIGNAL_WEIGHT = 0.35
APPLIES_TO_WEIGHT = 0.15


class RunbookQuery(BaseModel):
    category: str | None = None
    text: str | None = None
    item_types: list[ItemType] = Field(default_factory=list)
    limit: int = Field(default=3, ge=1, le=20)
    min_score: float = Field(default=0.4, ge=0, le=1)


class RunbookCatalog:
    def __init__(self, runbooks: list[Runbook]) -> None:
        self._by_id = {r.id: r for r in runbooks}

    @classmethod
    def load(cls, root: Path) -> RunbookCatalog:
        return cls(load_runbooks(root))

    def get(self, runbook_id: str) -> Runbook | None:
        return self._by_id.get(runbook_id)

    def all(self) -> list[Runbook]:
        return sorted(self._by_id.values(), key=lambda r: r.id)

    def search(self, query: RunbookQuery) -> list[RunbookMatch]:
        text = query.text or ""
        matches: list[RunbookMatch] = []
        for runbook in self._by_id.values():
            reasons: list[str] = []
            score = 0.0
            category_match = bool(query.category and runbook.category == query.category)
            if category_match:
                score += CATEGORY_WEIGHT
                reasons.append(f"category {runbook.category}")
            matched = [s for s in runbook.signals if text and re.search(s, text, re.IGNORECASE)]
            if matched:
                score += SIGNAL_WEIGHT * len(matched) / len(runbook.signals)
                reasons.append(f"{len(matched)}/{len(runbook.signals)} signals matched")
            if not category_match and not matched:
                continue
            if any(t in runbook.applies_to for t in query.item_types):
                score += APPLIES_TO_WEIGHT
                reasons.append("applies to affected item types")
            score = round(min(score, 1.0), 3)
            if score >= query.min_score:
                matches.append(
                    RunbookMatch(
                        runbook_id=runbook.id,
                        version=runbook.version,
                        title=runbook.title,
                        category=runbook.category,
                        score=score,
                        matched_signals=matched,
                        reasons=reasons,
                        path=runbook.path,
                        content_hash=runbook.content_hash,
                        risk=runbook.risk,
                        approval_required=runbook.approval_required,
                    )
                )
        matches.sort(key=lambda m: (-m.score, m.runbook_id))
        return matches[: query.limit]

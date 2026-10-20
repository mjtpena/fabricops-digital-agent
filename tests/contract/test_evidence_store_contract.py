"""EvidenceStore contract: the filesystem and Blob adapters behave the same (write-once, redaction, safe names)."""

from __future__ import annotations

import pytest

from orqentis.storage.evidence_store import EvidenceExistsError, EvidenceStore


async def test_json_is_redacted_stored_and_immutable_by_default(evidence_store: EvidenceStore) -> None:
    uri = await evidence_store.put_json("INC-1", "EV001.json", {"token": "abc", "v": 1, "text": "é"})
    assert uri == "evidence://INC-1/EV001.json"
    assert await evidence_store.get_json(uri) == {"token": "[REDACTED]", "v": 1, "text": "é"}
    with pytest.raises(EvidenceExistsError):
        await evidence_store.put_json("INC-1", "EV001.json", {"v": 2})
    assert await evidence_store.get_json(uri) == {"token": "[REDACTED]", "v": 1, "text": "é"}  # untouched


async def test_text_can_be_overwritten_unless_it_is_marked_immutable(evidence_store: EvidenceStore) -> None:
    uri = await evidence_store.put_text("INC-1", "report.md", "# one")
    assert await evidence_store.put_text("INC-1", "report.md", "# two") == uri
    assert await evidence_store.get_text(uri) == "# two"
    await evidence_store.put_text("INC-1", "frozen.md", "a", immutable=True)
    with pytest.raises(EvidenceExistsError):
        await evidence_store.put_text("INC-1", "frozen.md", "b", immutable=True)
    assert await evidence_store.get_text("evidence://INC-1/frozen.md") == "a"


async def test_mutable_json_can_be_rewritten(evidence_store: EvidenceStore) -> None:
    uri = await evidence_store.put_json("INC-1", "report.json", {"n": 1}, immutable=False)
    await evidence_store.put_json("INC-1", "report.json", {"n": 2}, immutable=False)
    assert await evidence_store.get_json(uri) == {"n": 2}


async def test_list_returns_the_uris_of_one_incident_sorted(evidence_store: EvidenceStore) -> None:
    assert await evidence_store.list("INC-1") == []
    await evidence_store.put_json("INC-1", "EV002.json", {})
    await evidence_store.put_json("INC-1", "EV001.json", {})
    await evidence_store.put_text("INC-10", "report.md", "x")  # a different incident that shares a prefix
    assert await evidence_store.list("INC-1") == ["evidence://INC-1/EV001.json", "evidence://INC-1/EV002.json"]


async def test_unsafe_names_are_rejected(evidence_store: EvidenceStore) -> None:
    for bad in ("../x", "a/b", ".hidden", "", "x" * 200):
        with pytest.raises(ValueError, match="Unsafe evidence path segment"):
            await evidence_store.put_text("INC-1", bad, "x")
    with pytest.raises(ValueError, match="Unsafe evidence path segment"):
        await evidence_store.put_text("../INC", "a.md", "x")
    with pytest.raises(ValueError, match="Not an evidence URI"):
        await evidence_store.get_text("https://example.com/x")
    with pytest.raises(ValueError, match="Unsafe evidence path segment"):
        await evidence_store.get_text("evidence://INC-1/../../etc/passwd")


async def test_a_missing_artefact_is_a_file_not_found_error(evidence_store: EvidenceStore) -> None:
    with pytest.raises(FileNotFoundError):
        await evidence_store.get_text("evidence://INC-1/nope.json")

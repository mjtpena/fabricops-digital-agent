"""ContractsSource contract: the file and the Blob source read, hash and refuse the contracts file the same way."""

from __future__ import annotations

import hashlib
import json

import pytest
from tests.contract.conftest import ContractsPublisher

from orqentis.contracts.diagnostics import MAX_FILE_BYTES
from orqentis.contracts.errors import ContractsFileNotFoundError, ContractsInvalidError
from orqentis.contracts.source import ContractsHolder, load_contracts_document, load_contracts_from

DOCUMENT = json.dumps({"dependencies": []}).encode()


async def test_a_document_carries_its_text_its_sha256_and_where_it_came_from(
    contracts_publisher: ContractsPublisher, contracts_backend: str
) -> None:
    await contracts_publisher.put(DOCUMENT)
    document = await contracts_publisher.source.read()
    assert document.text == DOCUMENT.decode()
    assert document.sha256 == hashlib.sha256(DOCUMENT).hexdigest()
    assert document.location == contracts_publisher.source.location
    if contracts_backend == "blob":
        assert document.location.endswith("/estate.json") and document.location.startswith("blob:")
        assert document.version  # the version id (or the ETag): a later reload can tell which upload it read
    contracts = load_contracts_document(document)
    assert contracts.source_sha256 == document.sha256


async def test_a_byte_order_mark_is_tolerated_and_does_not_change_the_digest_of_the_bytes(
    contracts_publisher: ContractsPublisher,
) -> None:
    data = b"\xef\xbb\xbf" + DOCUMENT
    await contracts_publisher.put(data)
    document = await contracts_publisher.source.read()
    assert document.text == DOCUMENT.decode()
    assert document.sha256 == hashlib.sha256(data).hexdigest()  # the digest is of what was uploaded


async def test_a_missing_file_is_the_typed_not_found_error_naming_where_it_looked(
    contracts_publisher: ContractsPublisher,
) -> None:
    with pytest.raises(ContractsFileNotFoundError) as excinfo:
        await contracts_publisher.source.read()
    assert contracts_publisher.source.location in str(excinfo.value)


async def test_a_removed_file_is_not_found_again(contracts_publisher: ContractsPublisher) -> None:
    await contracts_publisher.put(DOCUMENT)
    await contracts_publisher.source.read()
    await contracts_publisher.remove()
    with pytest.raises(ContractsFileNotFoundError):
        await contracts_publisher.source.read()


async def test_something_that_is_not_text_or_is_too_large_is_refused_without_echoing_it(
    contracts_publisher: ContractsPublisher,
) -> None:
    await contracts_publisher.put(b"\xff\xfe\x00 client_secret=hunter2")
    with pytest.raises(ContractsInvalidError, match="UTF-8") as not_text:
        await contracts_publisher.source.read()
    assert "hunter2" not in str(not_text.value)

    await contracts_publisher.put(b" " * (MAX_FILE_BYTES + 1))
    with pytest.raises(ContractsInvalidError, match="larger than"):
        await contracts_publisher.source.read()


async def test_invalid_json_and_unknown_fields_name_the_location_and_the_problem_but_not_the_values(
    contracts_publisher: ContractsPublisher,
) -> None:
    await contracts_publisher.put(b'{"dependencies": [], "client_secret": "hunter2-SECRET"}')
    with pytest.raises(ContractsInvalidError) as excinfo:
        await load_contracts_from(contracts_publisher.source)
    message = str(excinfo.value)
    assert contracts_publisher.source.location in message and "unknown field" in message
    assert "hunter2" not in message

    await contracts_publisher.put(b"not json at all")
    with pytest.raises(ContractsInvalidError, match="invalid JSON"):
        await load_contracts_from(contracts_publisher.source)


async def test_a_holder_reloads_on_change_ignores_the_same_bytes_and_keeps_the_last_good_contracts(
    contracts_publisher: ContractsPublisher,
) -> None:
    applied: list[str | None] = []
    await contracts_publisher.put(DOCUMENT)
    contracts, document = await load_contracts_from(contracts_publisher.source)
    holder = ContractsHolder(contracts_publisher.source, contracts, document, lambda c: applied.append(c.source_sha256))

    assert not await holder.reload()  # nothing changed: nothing applied, nothing audited
    await contracts_publisher.put(DOCUMENT)  # the same bytes uploaded again are not a change either
    assert not await holder.reload() and applied == []

    changed = json.dumps({"dependencies": [], "environments": []}).encode()
    await contracts_publisher.put(changed)
    assert await holder.reload()
    assert holder.document.sha256 == hashlib.sha256(changed).hexdigest() and applied == [holder.document.sha256]

    good = holder.document.sha256
    await contracts_publisher.put(b"{ broken")
    with pytest.raises(ContractsInvalidError):
        await holder.reload()
    await contracts_publisher.remove()
    with pytest.raises(ContractsFileNotFoundError):
        await holder.reload()
    # Neither failure replaced the contracts in force, and a fixed upload is picked up on the next reload.
    assert holder.document.sha256 == good and applied == [good]
    await contracts_publisher.put(DOCUMENT)
    assert await holder.reload() and holder.document.sha256 == hashlib.sha256(DOCUMENT).hexdigest()

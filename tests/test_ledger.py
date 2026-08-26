from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from frontier_harness.blobs import BlobStore
from frontier_harness.errors import LedgerIntegrityError
from frontier_harness.ledger import EventLedger
from frontier_harness.models import BlobRef


def test_ledger_is_append_only_and_hash_chained(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = EventLedger(path, "run_test")
    try:
        first = ledger.append("run.created", {"value": 1})
        second = ledger.append("action.completed", {"value": 2})
        count, last_hash = ledger.verify()
        assert count == 2
        assert second.previous_hash == first.event_hash
        assert last_hash == second.event_hash
        assert first.event_schema_version == 2

        connection = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                connection.execute("UPDATE events SET actor='tampered' WHERE seq=1")
        finally:
            connection.close()
    finally:
        ledger.close()


def test_append_validation_failure_rolls_back_event(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = EventLedger(path, "run_test")
    try:
        first = ledger.append("run.created", {"value": 1})

        def reject(_: object) -> None:
            raise ValueError("invalid transition")

        with pytest.raises(ValueError, match="invalid transition"):
            ledger.append("action.started", {"value": 2}, validate=reject)

        assert ledger.count() == 1
        assert ledger.last_event() == first
        assert ledger.verify() == (1, first.event_hash)
    finally:
        ledger.close()


def test_existing_v1_ledger_migrates_without_changing_hashes(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = EventLedger(path, "run_test")
    try:
        first = ledger.append("run.created", {"value": 1})
    finally:
        ledger.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute("DROP TRIGGER events_no_delete")
        connection.execute("UPDATE events SET event_schema_version = 1")
        legacy_material = json.dumps(
            {
                "action_id": first.action_id,
                "actor": first.actor,
                "event_id": first.event_id,
                "event_type": first.event_type,
                "payload_hash": first.payload_hash,
                "previous_hash": first.previous_hash,
                "run_id": first.run_id,
                "timestamp": first.timestamp,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        import hashlib

        legacy_hash = hashlib.sha256(legacy_material.encode("utf-8")).hexdigest()
        connection.execute("UPDATE events SET event_hash = ?", (legacy_hash,))
        connection.commit()
    finally:
        connection.close()

    reopened = EventLedger(path, "run_test")
    try:
        assert reopened.verify() == (1, legacy_hash)
        event = reopened.last_event()
        assert event is not None
        assert event.event_schema_version == 1
    finally:
        reopened.close()


def test_hash_verification_detects_payload_tampering(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = EventLedger(path, "run_test")
    ledger.append("run.created", {"value": 1})
    ledger.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute(
            "UPDATE events SET payload_json=? WHERE seq=1",
            (json.dumps({"value": 99}),),
        )
        connection.commit()
    finally:
        connection.close()

    reopened = EventLedger(path, "run_test")
    try:
        with pytest.raises(LedgerIntegrityError, match="Payload hash mismatch"):
            reopened.verify()
    finally:
        reopened.close()


def test_blob_store_detects_corruption(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    ref = store.put_text("important evidence")
    store.path(ref).write_text("corrupt", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="Corrupted blob"):
        store.verify(ref)


def test_blob_store_rejects_same_size_corruption_on_deduplicated_put(
    tmp_path: Path,
) -> None:
    store = BlobStore(tmp_path / "blobs")
    ref = store.put_text("abcd")
    store.path(ref).write_text("wxyz", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="collision or corruption"):
        store.put_text("abcd")


def test_blob_reference_cannot_redirect_outside_content_addressed_path(
    tmp_path: Path,
) -> None:
    store = BlobStore(tmp_path / "blobs")
    ref = store.put_text("safe")
    redirected = ref.model_copy(update={"relative_path": "../../outside"})
    with pytest.raises(LedgerIntegrityError, match="does not match its digest"):
        store.read_bytes(redirected)


def test_blob_reference_rejects_non_sha256_digest() -> None:
    with pytest.raises(ValueError, match="string_pattern_mismatch"):
        BlobRef(
            digest="../not-a-digest",
            size=0,
            relative_path="sha256/../not-a-digest",
        )


def test_blob_store_rejects_digest_path_traversal_even_from_unvalidated_copy(
    tmp_path: Path,
) -> None:
    store = BlobStore(tmp_path / "blobs")
    ref = store.put_text("safe")
    malicious_digest = "../" + "a" * 61
    forged = ref.model_copy(
        update={
            "digest": malicious_digest,
            "relative_path": f"sha256/{malicious_digest[:2]}/{malicious_digest[2:]}",
        }
    )
    with pytest.raises(LedgerIntegrityError, match="Invalid SHA-256 blob digest"):
        store.read_bytes(forged)

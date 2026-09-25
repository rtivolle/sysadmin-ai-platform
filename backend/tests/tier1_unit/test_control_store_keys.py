"""Durable API-key lifecycle: hashing, revocation, rotation, bootstrap import."""
import hashlib

import pytest

from control_store_fakes import KeyTableExecutor
from services.control_store.errors import ControlStoreIntegrityError, ControlStoreUnavailable
from services.control_store.key_store import KeyStore, generate_token, hash_token


class RecordingAudit:
    def __init__(self):
        self.events = []

    def __call__(self, **kwargs):
        self.events.append(kwargs)
        return {"logged": True}


@pytest.fixture
def audit():
    return RecordingAudit()


@pytest.fixture
def store(audit):
    return KeyStore(KeyTableExecutor(), audit=audit)


def test_generated_tokens_use_the_provisioned_shape():
    token = generate_token()
    assert token.startswith("sk-")
    assert len(token) > 40
    assert generate_token() != token


def test_issue_persists_only_the_token_hash(store, audit):
    plaintext = store.issue("sysadmin-01", label="laptop", created_by="sysadmin-admin")
    executor = store._executor
    assert plaintext not in executor.rows
    assert hash_token(plaintext) in executor.rows
    assert all(plaintext not in str(row) for row in executor.rows.values())
    assert executor.rows[hash_token(plaintext)]["user_id"] == "sysadmin-01"
    assert audit.events[0]["action"] == "api_key_issue"
    assert audit.events[0]["parameters"]["key_fingerprint"] == hash_token(plaintext)[:12]
    assert plaintext not in str(audit.events)


def test_resolve_returns_the_user_and_none_after_revocation(store):
    plaintext = store.issue("sysadmin-02", created_by="sysadmin-admin")
    assert store.resolve(plaintext) == "sysadmin-02"
    assert store.revoke("sysadmin-02", token=plaintext, actor="sysadmin-admin") == 1
    assert store.resolve(plaintext) is None


def test_resolve_ignores_blank_tokens_without_querying(store):
    assert store.resolve("") is None
    assert store.resolve("   ") is None
    assert store._executor.statements == []


def test_rotate_atomically_replaces_the_live_key(store, audit):
    old = store.issue("sysadmin-03", created_by="sysadmin-admin")
    new = store.rotate("sysadmin-03", label="rotated", created_by="sysadmin-admin")
    assert new != old
    assert store.resolve(old) is None
    assert store.resolve(new) == "sysadmin-03"
    assert len(store._executor.live_tokens_for("sysadmin-03")) == 1
    rotate_events = [event for event in audit.events if event["action"] == "api_key_rotate"]
    assert rotate_events[0]["parameters"]["revoked"] == 1
    assert new not in str(audit.events)


def test_revoke_without_a_token_removes_every_live_key(store):
    store.issue("sysadmin-04", created_by="sysadmin-admin")
    store.issue("sysadmin-04", label="second", created_by="sysadmin-admin")
    assert store.revoke("sysadmin-04", actor="sysadmin-admin") == 2
    assert store.active_keys("sysadmin-04") == []


def test_duplicate_token_is_refused_rather_than_reissued(store):
    store.issue("sysadmin-05", created_by="sysadmin-admin", token="sk-fixed-token")
    with pytest.raises(ControlStoreIntegrityError):
        store.issue("sysadmin-05", created_by="sysadmin-admin", token="sk-fixed-token")


def test_import_file_keys_is_idempotent_and_skips_non_identity_files(tmp_path, store):
    for name, value in {
        "sysadmin-01.key": "sk-one",
        "emergency-p1.key": "sk-emergency",
        "master.key": "sk-master",
        "valkey-password.key": "valkey-secret",
        "postgres-password.key": "pg-secret",
    }.items():
        (tmp_path / name).write_text(value + "\n")
    first = store.import_file_keys(tmp_path, actor="provision")
    assert first == {"imported": 2, "existing": 0, "skipped": 3}
    second = store.import_file_keys(tmp_path, actor="provision")
    assert second == {"imported": 0, "existing": 2, "skipped": 3}
    assert store.resolve("sk-one") == "sysadmin-01"
    assert store.resolve("sk-emergency") == "emergency-p1-oncall"
    assert store.resolve("sk-master") is None


def test_store_outage_is_a_connection_error(store):
    store._executor.fail_with = ControlStoreUnavailable("PostgreSQL is unreachable")
    with pytest.raises(ConnectionError):
        store.resolve("sk-anything")


def test_active_keys_never_expose_token_material(store):
    plaintext = store.issue("sysadmin-06", label="console", created_by="sysadmin-admin")
    listed = store.active_keys()
    assert listed == [{
        "user_id": "sysadmin-06",
        "label": "console",
        "created_at": "2026-09-24T00:00:00Z",
        "created_by": "sysadmin-admin",
    }]
    assert plaintext not in str(listed)
    assert hashlib.sha256(plaintext.encode()).hexdigest() not in str(listed)


def test_key_history_includes_revoked_keys(store):
    store.issue("sysadmin-07", created_by="sysadmin-admin")
    store.revoke("sysadmin-07", actor="sysadmin-admin")
    history = store.key_history("sysadmin-07")
    assert len(history) == 1
    assert history[0]["revoked_at"] is not None

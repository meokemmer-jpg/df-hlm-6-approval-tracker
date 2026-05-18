
# K12+K13+K16 Trinity-CONTRARIAN 2026-05-17 (Cross-LLM-validated)
def k12_provenance(payload: bytes, key: bytes = b"df-trinity-contrarian-v1") -> dict:
    import hashlib, hmac
    return {
        "payload_hash": hashlib.sha256(payload).hexdigest(),
        "hmac_sha256": hmac.new(key, payload, hashlib.sha256).hexdigest(),
    }

def k13_anchor(payload_hash: str) -> dict:
    from datetime import datetime, timezone
    return {
        "anchor_type": "rfc3161-mock",
        "iso_ts": datetime.now(timezone.utc).isoformat(),
        "payload_hash": payload_hash,
    }

def k16_lock_or_exit(df_name: str):
    import fcntl, os, sys
    lock_path = f"/tmp/df-trinity-{df_name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        sys.exit(3)

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
COMMON_ROOT = PROJECT_ROOT.parent / "_df_common"
for candidate in (str(SRC_ROOT), str(COMMON_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from approval_tracker import (  # noqa: E402
    ApprovalItem,
    ApprovalTracker,
    ConfigurationError,
    ConcurrentRunError,
    SalesforceUnavailable,
    TrackerConfig,
)


UTC = timezone.utc


class FakeSalesforceClient:
    def __init__(self, responses: list[object] | None = None):
        self.responses = list(responses or [])
        self.calls = 0

    def sync_approval_item(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"mode": "full", "anchor_id": payload["idempotency_key"], "custom_object": "Marketing-Approval-Item"}


def build_config(tmp_path: Path, env: dict[str, str] | None = None) -> TrackerConfig:
    return TrackerConfig.from_env(tmp_path / "runtime", env=env)


def build_tracker(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    client: FakeSalesforceClient | None = None,
) -> ApprovalTracker:
    config = build_config(tmp_path, env=env)
    return ApprovalTracker(config, salesforce_client=client)


@pytest.fixture
def engine(tmp_path: Path) -> ApprovalTracker:
    return build_tracker(tmp_path)


def make_item(**overrides: object) -> ApprovalItem:
    base = {
        "asset_id": "asset-001",
        "version": "v3",
        "approval_round": 2,
        "workflow_status": "pending",
        "created_at": datetime.now(UTC) - timedelta(hours=1),
        "updated_at": datetime.now(UTC),
        "approver": "Imke",
        "decision": None,
        "decision_reason": None,
    }
    base.update(overrides)
    return ApprovalItem(**base)


def test_default_mock_mode_no_sf_call(tmp_path: Path) -> None:
    client = FakeSalesforceClient()
    tracker = build_tracker(tmp_path, client=client)
    result = tracker.run([make_item()])
    assert result["results"][0]["salesforce"]["mode"] == "mock"
    assert client.calls == 0


def test_env_var_true_real_mode_sandbox(tmp_path: Path) -> None:
    config = build_config(
        tmp_path,
        env={
            "DF_HLM_6_REAL_SALESFORCE_ENABLED": "true",
            "SF_ENV": "sandbox",
            "PHRONESIS_TICKET": "PT-2026-05-001",
        },
    )
    assert config.real_salesforce_enabled is True
    assert config.sf_env == "sandbox"


def test_sf_env_production_requires_explicit_set(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        build_config(
            tmp_path,
            env={
                "DF_HLM_6_REAL_SALESFORCE_ENABLED": "true",
                "PHRONESIS_TICKET": "PT-2026-05-001",
            },
        )


def test_concurrent_spawn_protection(tmp_path: Path) -> None:
    tracker_a = build_tracker(tmp_path)
    tracker_b = build_tracker(tmp_path)
    assert tracker_a.acquire_run_lock() is True
    with pytest.raises(ConcurrentRunError):
        tracker_b.run([make_item(asset_id="asset-002")])
    tracker_a.lock.release()


def test_cascade_containment(tmp_path: Path) -> None:
    client = FakeSalesforceClient(
        responses=[
            SalesforceUnavailable("down", unreachable_for_s=5),
            {"mode": "full", "anchor_id": "ok-2", "custom_object": "Marketing-Approval-Item"},
        ]
    )
    tracker = build_tracker(
        tmp_path,
        env={
            "DF_HLM_6_REAL_SALESFORCE_ENABLED": "true",
            "SF_ENV": "sandbox",
            "PHRONESIS_TICKET": "PT-2026-05-001",
        },
        client=client,
    )
    result = tracker.run([make_item(asset_id="asset-a"), make_item(asset_id="asset-b")])
    assert len(result["results"]) == 1
    dlq_entries = list((tracker.config.dlq_dir).glob("*.json"))
    assert len(dlq_entries) == 1


def test_external_anchor_salesforce(tmp_path: Path) -> None:
    client = FakeSalesforceClient(
        responses=[{"mode": "full", "anchor_id": "sf-123", "custom_object": "Marketing-Approval-Item"}]
    )
    tracker = build_tracker(
        tmp_path,
        env={
            "DF_HLM_6_REAL_SALESFORCE_ENABLED": "true",
            "SF_ENV": "sandbox",
            "PHRONESIS_TICKET": "PT-2026-05-001",
        },
        client=client,
    )
    result = tracker.run([make_item(decision="approved")])
    entry = result["results"][0]
    assert entry["external_anchor_type"] == "salesforce_api"
    assert entry["salesforce"]["anchor_id"] == "sf-123"


def test_circuit_breaker_open(tmp_path: Path) -> None:
    client = FakeSalesforceClient(
        responses=[
            SalesforceUnavailable("down-1", unreachable_for_s=31),
            SalesforceUnavailable("down-2", unreachable_for_s=31),
            SalesforceUnavailable("down-3", unreachable_for_s=31),
        ]
    )
    tracker = build_tracker(
        tmp_path,
        env={
            "DF_HLM_6_REAL_SALESFORCE_ENABLED": "true",
            "SF_ENV": "sandbox",
            "PHRONESIS_TICKET": "PT-2026-05-001",
        },
        client=client,
    )
    tracker.process_item(make_item(asset_id="asset-1"), now=datetime.now(UTC))
    tracker.process_item(make_item(asset_id="asset-2"), now=datetime.now(UTC))
    third = tracker.process_item(make_item(asset_id="asset-3"), now=datetime.now(UTC))
    fourth = tracker.process_item(make_item(asset_id="asset-4"), now=datetime.now(UTC))
    assert third["salesforce"]["mode"] == "standalone_local_queue"
    assert fourth["salesforce"]["reason"] == "circuit_open"


def test_direct_mode_local_queue(tmp_path: Path) -> None:
    client = FakeSalesforceClient(responses=[SalesforceUnavailable("down", unreachable_for_s=31)])
    tracker = build_tracker(
        tmp_path,
        env={
            "DF_HLM_6_REAL_SALESFORCE_ENABLED": "true",
            "SF_ENV": "sandbox",
            "PHRONESIS_TICKET": "PT-2026-05-001",
        },
        client=client,
    )
    item = make_item(asset_id="asset-direct")
    result = tracker.process_item(item, now=datetime.now(UTC))
    assert result["salesforce"]["mode"] == "standalone_local_queue"
    assert tracker._queue_file(item).exists()


def test_idempotent_approval_hash(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    key_a = tracker.make_idempotency_key("asset-1", "v7", 2)
    key_b = tracker.make_idempotency_key("asset-1", "v7", 2)
    assert key_a == key_b


def test_health_check_no_deps(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    assert tracker.health_check() == {"status": "ok", "dependencies": []}


def test_workflow_pending_to_approved(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    result = tracker.process_item(make_item(decision="approved"), now=datetime.now(UTC))
    assert result["workflow_status"] == "approved"


def test_workflow_pending_to_rejected_with_reason(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    result = tracker.process_item(
        make_item(decision="rejected", decision_reason="Brand fit insufficient"),
        now=datetime.now(UTC),
    )
    assert result["workflow_status"] == "rejected"
    assert result["decision_reason"] == "Brand fit insufficient"


def test_auto_escalation_pending_72h(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    item = make_item(created_at=datetime.now(UTC) - timedelta(hours=73), approver=None)
    result = tracker.process_item(item, now=datetime.now(UTC))
    assert result["workflow_status"] == "escalated-to-martin"


def test_auto_ping_imke_in_review_48h(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    item = make_item(workflow_status="in-review", created_at=datetime.now(UTC) - timedelta(hours=49))
    result = tracker.process_item(item, now=datetime.now(UTC))
    assert result["ping_imke"] is True


def test_rejected_triggers_asset_modification(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    result = tracker.process_item(
        make_item(decision="rejected", decision_reason="CTA mismatch"),
        now=datetime.now(UTC),
    )
    assert result["asset_modification_required"] is True


def test_provenance_in_output(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    result = tracker.process_item(make_item(decision="approved"), now=datetime.now(UTC))
    provenance = result["provenance"]
    assert provenance["asset_id"] == "asset-001"
    assert provenance["approver"] == "Imke"
    assert provenance["decision"] == "approved"


def test_pre_action_domain_check_env_tag(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        build_config(
            tmp_path,
            env={
                "DF_HLM_6_REAL_SALESFORCE_ENABLED": "true",
                "SF_ENV": "staging",
                "PHRONESIS_TICKET": "PT-2026-05-001",
            },
        )


def test_audit_log_appended_per_run(tmp_path: Path) -> None:
    tracker = build_tracker(tmp_path)
    tracker.run([make_item(asset_id="asset-r1")])
    tracker.run([make_item(asset_id="asset-r2")])
    lines = tracker.config.audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 4
    assert all(json.loads(line)["event"] for line in lines)


def test_pii_scrubbed_in_output_with_kemmer_name(engine: ApprovalTracker) -> None:
    """Output enthaelt keinen Kemmer-Familien-Namen."""
    engine.run([make_item(asset_id="asset-martin", approver="Martin", decision="approved")])

    output_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            engine.config.report_dir / "daily-status-dashboard.html",
            engine.config.report_dir / "weekly-report.md",
            engine.config.report_dir / "slack-updates.json",
            *engine.config.queue_dir.glob("*.json"),
            engine.config.audit_log_path,
        )
    )
    assert "Martin" not in output_text
    assert "Imke" not in output_text


def test_k13_pre_action_verification_env_tag_block(
    engine: ApprovalTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-Mode mit falschem env_tag wird geblockt."""
    monkeypatch.setenv("DF_ENV_TAG", "prod")
    monkeypatch.setenv("DF_HLM_6_REAL_SALESFORCE_ENABLED", "true")
    engine.config.real_salesforce_enabled = True
    engine.config.sf_env = "sandbox"
    engine.config.phronesis_ticket = "PT-2026-05-001"

    with pytest.raises(RuntimeError) as exc_info:
        engine.run([make_item(asset_id="asset-k13")])

    assert "K13" in str(exc_info.value)


def test_mock_provenance_explicit_in_output(engine: ApprovalTracker) -> None:
    """Mock-Outputs haben 'mode': 'mock' in Provenance."""
    result = engine.run([make_item(asset_id="asset-mock-provenance")])

    weekly_report = Path(result["weekly_report"]).read_text(encoding="utf-8")
    slack_updates = json.loads((engine.config.report_dir / "slack-updates.json").read_text(encoding="utf-8"))
    assert '"mode": "mock"' in weekly_report
    assert slack_updates["provenance"]["mode"] == "mock"
    assert result["results"][0]["salesforce"]["anchor_id"].startswith("MOCK-")


def test_k16_mutex_blocks_concurrent_spawn(tmp_path: Path, engine: ApprovalTracker) -> None:
    """Concurrent Engine-Spawn wird geblockt."""
    tracker_b = build_tracker(tmp_path)
    assert engine.acquire_run_lock() is True

    try:
        with pytest.raises(ConcurrentRunError) as exc_info:
            tracker_b.run([make_item(asset_id="asset-k16")])
    finally:
        engine.lock.release()

    assert "K16" in str(exc_info.value)

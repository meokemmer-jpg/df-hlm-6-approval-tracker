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

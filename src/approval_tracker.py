from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import requests

try:
    import structlog
except ImportError:  # pragma: no cover - tiny compatibility shim
    class _JsonRenderer:
        def __init__(self, *args: Any, **kwargs: Any):
            pass

        def __call__(self, _: Any, __: str, event_dict: dict[str, Any]) -> str:
            return json.dumps(event_dict, sort_keys=True)

    class _Processors:
        JSONRenderer = _JsonRenderer

    class _StructlogShim:
        processors = _Processors()

    structlog = _StructlogShim()  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DARK_FACTORIES_ROOT = PROJECT_ROOT.parent
COMMON_ROOT = DARK_FACTORIES_ROOT / "_df_common"
if str(COMMON_ROOT) not in sys.path and COMMON_ROOT.exists():
    sys.path.insert(0, str(COMMON_ROOT))

from atomic_lock import AtomicLock  # type: ignore[import-not-found]
from secret_vault import SecretVault, VaultError  # type: ignore[import-not-found]


UTC = timezone.utc


class ConfigurationError(RuntimeError):
    """Raised when env gating or runtime configuration is invalid."""


class ConcurrentRunError(RuntimeError):
    """Raised when K16 detects a concurrently running DF-HLM-6 instance."""


class SalesforceUnavailable(RuntimeError):
    """Raised when Salesforce is unavailable long enough to trigger LC2."""

    def __init__(self, message: str, *, unreachable_for_s: float = 0.0):
        super().__init__(message)
        self.unreachable_for_s = unreachable_for_s


class SalesforceClient(Protocol):
    def sync_approval_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class ApprovalItem:
    asset_id: str
    version: str
    approval_round: int
    workflow_status: str = "pending"
    source: str = "df-hlm-1"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    approver: str | None = None
    decision: str | None = None
    decision_reason: str | None = None
    manual_ticket_id: str | None = None


@dataclass
class TrackerConfig:
    runtime_dir: Path
    lock_dir: Path
    audit_log_path: Path
    queue_dir: Path
    dlq_dir: Path
    report_dir: Path
    real_salesforce_enabled: bool = False
    sf_env: str | None = None
    phronesis_ticket: str | None = None
    custom_object_name: str = "Marketing-Approval-Item"
    failure_blast_radius: int = 1
    dependency_dlq_separate: bool = True
    provenance_required_in_output: bool = True
    non_llm_validation_layer: bool = True
    external_anchor_type: str = "salesforce_api"
    pre_action_domain_check: bool = True
    override_complexity: str = "single_command"
    martin_review_cadence: str = "weekly"
    entropy_added_loc_estimate: int = 350
    entropy_justified_by_rho: str = "EUR 45-60k/Jahr Approval-Velocity"
    degradation_modes: tuple[str, ...] = (
        "full",
        "degraded_salesforce_api",
        "degraded_slack_api",
        "standalone_local_queue",
    )
    direct_mode_capability: float = 0.50
    salesforce_timeout_s: int = 30
    circuit_breaker_open_threshold: int = 3
    circuit_breaker_backoff_s: int = 60
    health_check_dependencies: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, runtime_dir: Path, env: dict[str, str] | None = None) -> "TrackerConfig":
        env_map = dict(os.environ)
        if env:
            env_map.update(env)
        runtime_dir = Path(runtime_dir)
        lock_dir = Path(env_map.get("DF_HLM_6_LOCK_DIR", "/tmp/df-hlm-6.lock"))
        config = cls(
            runtime_dir=runtime_dir,
            lock_dir=lock_dir,
            audit_log_path=runtime_dir / "audit.jsonl",
            queue_dir=runtime_dir / "local_queue",
            dlq_dir=runtime_dir / "local_queue" / "dlq",
            report_dir=runtime_dir / "reports",
            real_salesforce_enabled=env_map.get("DF_HLM_6_REAL_SALESFORCE_ENABLED", "").lower() == "true",
            sf_env=env_map.get("SF_ENV"),
            phronesis_ticket=env_map.get("PHRONESIS_TICKET"),
        )
        config.ensure_runtime_dirs()
        config.validate_pre_action()
        return config

    def ensure_runtime_dirs(self) -> None:
        for path in (self.runtime_dir, self.queue_dir, self.dlq_dir, self.report_dir):
            path.mkdir(parents=True, exist_ok=True)

    def validate_pre_action(self) -> None:
        if not self.pre_action_domain_check or not self.real_salesforce_enabled:
            return
        if self.sf_env not in {"sandbox", "production"}:
            raise ConfigurationError("SF_ENV must be explicitly set to sandbox or production.")
        if not self.phronesis_ticket:
            raise ConfigurationError("PHRONESIS_TICKET is required for real Salesforce mode.")


class JsonAuditLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.renderer = structlog.processors.JSONRenderer(sort_keys=True)

    def log(self, event: str, **fields: Any) -> None:
        entry = {"event": event, "ts": _utcnow().isoformat()}
        entry.update(fields)
        rendered = self.renderer(None, "info", entry)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")


class RealSalesforceClient:
    def __init__(
        self,
        config: TrackerConfig,
        *,
        session: requests.Session | None = None,
        base_url: str | None = None,
    ):
        self.config = config
        self.session = session or requests.Session()
        self.base_url = base_url or os.environ.get("SF_API_BASE_URL", "https://example.invalid")
        self.token = self._load_oauth_token()

    def _load_oauth_token(self) -> str:
        if os.environ.get("DF_HLM_6_SF_OAUTH_TOKEN"):
            return os.environ["DF_HLM_6_SF_OAUTH_TOKEN"]
        vault_path = Path(os.environ.get("DF_HLM_6_SECRET_VAULT_PATH", self.config.runtime_dir / "vault.bin"))
        master_key_path = Path(
            os.environ.get("DF_HLM_6_SECRET_VAULT_MASTER_KEY_PATH", self.config.runtime_dir / "vault.key")
        )
        try:
            vault = SecretVault(vault_path=vault_path, master_key_path=master_key_path)
            return str(vault.get_secret("salesforce_oauth_access_token"))
        except (VaultError, FileNotFoundError) as exc:
            raise ConfigurationError(f"Salesforce OAuth token unavailable via SecretVault: {exc}") from exc

    def sync_approval_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/services/data/v1/custom-objects/{self.config.custom_object_name}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(self.config.circuit_breaker_open_threshold):
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.config.salesforce_timeout_s,
                )
                response.raise_for_status()
                body = response.json() if response.content else {}
                return {
                    "mode": "full",
                    "anchor_id": body.get("id", payload["idempotency_key"]),
                    "custom_object": self.config.custom_object_name,
                    "sf_env": self.config.sf_env,
                }
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.circuit_breaker_open_threshold - 1:
                    break
        raise SalesforceUnavailable(str(last_error or "unknown-salesforce-error"), unreachable_for_s=31.0)


class MockSalesforceClient:
    def __init__(self) -> None:
        self.calls = 0

    def sync_approval_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {
            "mode": "mock",
            "anchor_id": payload["idempotency_key"],
            "custom_object": "Marketing-Approval-Item",
            "sf_env": "mock",
        }


class ApprovalTracker:
    def __init__(self, config: TrackerConfig, *, salesforce_client: SalesforceClient | None = None):
        self.config = config
        self.audit = JsonAuditLogger(config.audit_log_path)
        self.salesforce_client = salesforce_client or (
            RealSalesforceClient(config) if config.real_salesforce_enabled else MockSalesforceClient()
        )
        self.lock = AtomicLock(config.lock_dir / "approval-tracker.lock", ttl_s=600.0)
        self._consecutive_failures = 0
        self._circuit_open_until: datetime | None = None

    @staticmethod
    def make_idempotency_key(asset_id: str, version: str, approval_round: int) -> str:
        raw = f"{asset_id}:{version}:{approval_round}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "dependencies": list(self.config.health_check_dependencies)}

    def _validate_item(self, item: ApprovalItem) -> None:
        if self.config.non_llm_validation_layer and item.decision == "rejected" and not item.decision_reason:
            raise ValueError("Rejected approvals require a decision_reason.")
        if not item.asset_id or not item.version:
            raise ValueError("asset_id and version are required.")

    def _is_circuit_open(self, now: datetime) -> bool:
        return self._circuit_open_until is not None and now < self._circuit_open_until

    def _queue_file(self, item: ApprovalItem) -> Path:
        return self.config.queue_dir / f"{self.make_idempotency_key(item.asset_id, item.version, item.approval_round)}.json"

    def _dlq_file(self, item: ApprovalItem) -> Path:
        return self.config.dlq_dir / f"{self.make_idempotency_key(item.asset_id, item.version, item.approval_round)}.json"

    def _build_provenance(self, item: ApprovalItem, decision: str, now: datetime) -> dict[str, Any]:
        return {
            "asset_id": item.asset_id,
            "approver": item.approver or "system",
            "timestamp": now.isoformat(),
            "decision": decision,
        }

    def _resolve_status(self, item: ApprovalItem, now: datetime) -> tuple[str, dict[str, Any]]:
        meta: dict[str, Any] = {"ping_imke": False, "asset_modification_required": False}
        age = now - item.created_at
        if item.decision == "approved":
            return "approved", meta
        if item.decision == "rejected":
            meta["asset_modification_required"] = True
            return "rejected", meta
        if item.decision == "escalated-to-martin":
            return "escalated-to-martin", meta
        if item.workflow_status == "pending" and age > timedelta(hours=72):
            return "escalated-to-martin", meta
        if item.workflow_status == "in-review" and age > timedelta(hours=48):
            meta["ping_imke"] = True
            return "in-review", meta
        return item.workflow_status, meta

    def _build_payload(self, item: ApprovalItem, now: datetime, status: str) -> dict[str, Any]:
        decision = item.decision or status
        return {
            "asset_id": item.asset_id,
            "version": item.version,
            "approval_round": item.approval_round,
            "workflow_status": status,
            "decision": decision,
            "decision_reason": item.decision_reason,
            "approver": item.approver,
            "manual_ticket_id": item.manual_ticket_id,
            "custom_object": self.config.custom_object_name,
            "idempotency_key": self.make_idempotency_key(item.asset_id, item.version, item.approval_round),
            "phronesis_ticket": self.config.phronesis_ticket,
            "sf_env": self.config.sf_env if self.config.real_salesforce_enabled else "mock",
            "provenance": self._build_provenance(item, decision, now),
        }

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=_json_default)
            handle.flush()
            tmp_name = handle.name
        os.replace(tmp_name, path)

    def _persist_local_state(self, item: ApprovalItem, result: dict[str, Any]) -> None:
        self._write_json_atomic(self._queue_file(item), result)

    def _write_dlq(self, item: ApprovalItem, exc: Exception) -> None:
        payload = {
            "asset_id": item.asset_id,
            "version": item.version,
            "approval_round": item.approval_round,
            "error": str(exc),
        }
        self._write_json_atomic(self._dlq_file(item), payload)

    def _sync_payload(self, payload: dict[str, Any], now: datetime) -> dict[str, Any]:
        if self._is_circuit_open(now):
            return {
                "mode": "standalone_local_queue",
                "anchor_id": payload["idempotency_key"],
                "reason": "circuit_open",
            }
        try:
            result = self.salesforce_client.sync_approval_item(payload)
            self._consecutive_failures = 0
            self._circuit_open_until = None
            return result
        except SalesforceUnavailable as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.circuit_breaker_open_threshold:
                self._circuit_open_until = now + timedelta(seconds=self.config.circuit_breaker_backoff_s)
            if exc.unreachable_for_s > self.config.salesforce_timeout_s:
                return {
                    "mode": "standalone_local_queue",
                    "anchor_id": payload["idempotency_key"],
                    "reason": "salesforce_unreachable",
                }
            raise

    def process_item(self, item: ApprovalItem, *, now: datetime | None = None) -> dict[str, Any]:
        current_time = now or _utcnow()
        self._validate_item(item)
        status, meta = self._resolve_status(item, current_time)
        payload = self._build_payload(item, current_time, status)
        transport = self._sync_payload(payload, current_time) if self.config.real_salesforce_enabled else {
            "mode": "mock",
            "anchor_id": payload["idempotency_key"],
            "reason": "default_mock_mode",
        }
        result = {
            "asset_id": item.asset_id,
            "version": item.version,
            "approval_round": item.approval_round,
            "workflow_status": status,
            "decision": payload["decision"],
            "decision_reason": item.decision_reason,
            "idempotency_key": payload["idempotency_key"],
            "provenance": payload["provenance"] if self.config.provenance_required_in_output else {},
            "external_anchor_type": self.config.external_anchor_type,
            "salesforce": transport,
            "ping_imke": meta["ping_imke"],
            "asset_modification_required": meta["asset_modification_required"],
            "slack_update": _slack_update(item.asset_id, status, payload["decision"], meta["ping_imke"]),
        }
        self._persist_local_state(item, result)
        self.audit.log("item_processed", asset_id=item.asset_id, status=status, mode=transport["mode"])
        return result

    def acquire_run_lock(self) -> bool:
        return self.lock.acquire()

    def run(self, items: list[ApprovalItem], *, now: datetime | None = None) -> dict[str, Any]:
        current_time = now or _utcnow()
        if not self.acquire_run_lock():
            raise ConcurrentRunError(f"K16 mutex already held: {self.config.lock_dir}")
        self.audit.log("run_start", item_count=len(items), sf_env=self.config.sf_env or "mock")
        results: list[dict[str, Any]] = []
        try:
            for item in items:
                try:
                    results.append(self.process_item(item, now=current_time))
                except Exception as exc:
                    self._write_dlq(item, exc)
                    self.audit.log("item_failed", asset_id=item.asset_id, error=str(exc))
                    if self.config.failure_blast_radius != 1:
                        raise
            dashboard = self._generate_dashboard(results, current_time)
            weekly = self._generate_weekly_report(results, current_time)
            slack_updates = [entry["slack_update"] for entry in results if entry["slack_update"]]
            self._write_json_atomic(self.config.report_dir / "slack-updates.json", {"updates": slack_updates})
            self.audit.log("run_end", processed=len(results))
            return {
                "results": results,
                "daily_dashboard": str(dashboard),
                "weekly_report": str(weekly),
                "slack_updates": slack_updates,
            }
        finally:
            self.lock.release()

    def _generate_dashboard(self, results: list[dict[str, Any]], now: datetime) -> Path:
        rows = "".join(
            f"<tr><td>{entry['asset_id']}</td><td>{entry['workflow_status']}</td><td>{entry['decision']}</td></tr>"
            for entry in results
        )
        html = (
            "<html><body><h1>DF-HLM-6 Daily Status Dashboard</h1>"
            f"<p>Generated: {now.isoformat()}</p>"
            "<table><tr><th>Asset</th><th>Status</th><th>Decision</th></tr>"
            f"{rows}</table></body></html>"
        )
        path = self.config.report_dir / "daily-status-dashboard.html"
        path.write_text(html, encoding="utf-8")
        return path

    def _generate_weekly_report(self, results: list[dict[str, Any]], now: datetime) -> Path:
        counts: dict[str, int] = {}
        for entry in results:
            counts[entry["workflow_status"]] = counts.get(entry["workflow_status"], 0) + 1
        lines = ["# DF-HLM-6 Weekly Report", f"- Generated: {now.isoformat()}"]
        for key in sorted(counts):
            lines.append(f"- {key}: {counts[key]}")
        path = self.config.report_dir / "weekly-report.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")


def _slack_update(asset_id: str, status: str, decision: str, ping_imke: bool) -> str:
    suffix = " | ping Imke" if ping_imke else ""
    return f"[DF-HLM-6] {asset_id}: {status} ({decision}){suffix}"


def _load_items_from_path(path: Path) -> list[ApprovalItem]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items: list[ApprovalItem] = []
    for entry in raw:
        items.append(
            ApprovalItem(
                asset_id=entry["asset_id"],
                version=entry["version"],
                approval_round=int(entry["approval_round"]),
                workflow_status=entry.get("workflow_status", "pending"),
                source=entry.get("source", "df-hlm-1"),
                created_at=_parse_dt(entry.get("created_at")) or _utcnow(),
                updated_at=_parse_dt(entry.get("updated_at")) or _utcnow(),
                approver=entry.get("approver"),
                decision=entry.get("decision"),
                decision_reason=entry.get("decision_reason"),
                manual_ticket_id=entry.get("manual_ticket_id"),
            )
        )
    return items


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DF-HLM-6 approval tracker")
    parser.add_argument("--runtime-dir", default=str(PROJECT_ROOT / "runtime"))
    parser.add_argument("--input", default=None, help="Optional JSON list of approval items")
    args = parser.parse_args(argv)

    config = TrackerConfig.from_env(Path(args.runtime_dir))
    tracker = ApprovalTracker(config)
    items = _load_items_from_path(Path(args.input)) if args.input else []
    result = tracker.run(items)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

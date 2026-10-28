"""Runtime settings, read from AIOPS_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import MODES

_PKG = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    mode: str = "diagnose"                 # safe default: read-only diagnosis
    dry_run: bool = True                   # safe default: server-side dry run only
    policy_file: str = str(_PKG / "policies" / "policy.yaml")
    runbook_dir: str = str(_PKG / "runbooks")
    audit_dir: str = str(_PKG / "audit")
    prometheus_url: str = "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090"
    loki_url: str = "http://loki.monitoring.svc:3100"
    lookback_minutes: int = 15
    webhook_token: str = field(default="", repr=False)
    approval_token: str = field(default="", repr=False)
    llm_provider: str = "none"             # none|anthropic|openai  (none = deterministic reasoner)
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_base_url: str = ""
    verify_timeout_s: int = 180
    verify_interval_s: int = 5
    dedupe_window_s: int = 600
    github_enabled: bool = False
    github_token: str = field(default="", repr=False)
    github_repo: str = ""                  # owner/name
    github_base_branch: str = "main"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        mode = e("AIOPS_MODE", "diagnose")
        if mode not in MODES:
            raise ValueError(f"AIOPS_MODE must be one of {MODES}, got {mode!r}")
        d = cls()
        return cls(
            mode=mode,
            dry_run=_bool("AIOPS_DRY_RUN", True),
            policy_file=e("AIOPS_POLICY_FILE", d.policy_file),
            runbook_dir=e("AIOPS_RUNBOOK_DIR", d.runbook_dir),
            audit_dir=e("AIOPS_AUDIT_DIR", d.audit_dir),
            prometheus_url=e("AIOPS_PROMETHEUS_URL", d.prometheus_url),
            loki_url=e("AIOPS_LOKI_URL", d.loki_url),
            lookback_minutes=int(e("AIOPS_LOOKBACK_MINUTES", d.lookback_minutes)),
            webhook_token=e("AIOPS_WEBHOOK_TOKEN", "").strip(),   # a trailing newline in a mounted Secret must not break auth
            approval_token=e("AIOPS_APPROVAL_TOKEN", "").strip(),
            llm_provider=e("AIOPS_LLM_PROVIDER", "none"),
            llm_model=e("AIOPS_LLM_MODEL", ""),
            llm_api_key=e("AIOPS_LLM_API_KEY", ""),
            llm_base_url=e("AIOPS_LLM_BASE_URL", ""),
            verify_timeout_s=int(e("AIOPS_VERIFY_TIMEOUT_S", d.verify_timeout_s)),
            verify_interval_s=int(e("AIOPS_VERIFY_INTERVAL_S", d.verify_interval_s)),
            dedupe_window_s=int(e("AIOPS_DEDUPE_WINDOW_S", d.dedupe_window_s)),
            github_enabled=_bool("AIOPS_GITHUB_ENABLED", False),
            github_token=e("AIOPS_GITHUB_TOKEN", ""),
            github_repo=e("AIOPS_GITHUB_REPO", ""),
            github_base_branch=e("AIOPS_GITHUB_BASE_BRANCH", "main"),
        )

import json
from datetime import timedelta

from aiops.agent.audit import AuditLog
from aiops.agent.redact import REDACTED, redact, redact_obj


def test_records_are_chained_and_verify(tmp_path):
    log = AuditLog(tmp_path)
    a = log.record("inc-1", "alert_received", "agent", {"x": 1})
    b = log.record("inc-1", "diagnosis", "agent", {"y": 2})
    assert b["prev_hash"] == a["hash"]
    assert log.verify_chain() == (True, "ok")


def test_tampering_is_detected(tmp_path):
    log = AuditLog(tmp_path)
    for i in range(3):
        log.record("inc-1", f"e{i}", "agent", {"i": i})
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["data"]["i"] = 999
    lines[1] = json.dumps(rec)
    log.path.write_text("\n".join(lines) + "\n")
    ok, msg = log.verify_chain()
    assert not ok and "record 2" in msg


def test_deleted_record_is_detected(tmp_path):
    log = AuditLog(tmp_path)
    for i in range(3):
        log.record("inc-1", f"e{i}", "agent")
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not log.verify_chain()[0]


def test_secrets_never_reach_the_log(tmp_path):
    log = AuditLog(tmp_path)
    log.record("inc-1", "x", "agent", {"db_password": "hunter2", "line": "DB_PASSWORD=hunter2 token=abcd1234efgh",
                                      "dsn": "app:s3cr3t@tcp(mysql:3306)/db"})
    raw = log.path.read_text()
    assert "hunter2" not in raw and "s3cr3t" not in raw and "abcd1234efgh" not in raw
    assert REDACTED in raw


def test_filter_by_incident(tmp_path):
    log = AuditLog(tmp_path)
    log.record("a", "e", "x")
    log.record("b", "e", "x")
    assert len(log.events("a")) == 1 and len(log.events()) == 2


def test_chain_survives_new_instance(tmp_path):
    AuditLog(tmp_path).record("a", "e", "x")
    log2 = AuditLog(tmp_path)
    log2.record("a", "f", "x")
    assert log2.verify_chain()[0]


def test_execution_counting_ignores_dry_runs(tmp_path):
    log = AuditLog(tmp_path)
    log.record("a", "action_executed", "x", {"namespace": "n", "workload": "w", "dry_run": True})
    log.record("a", "action_executed", "x", {"namespace": "n", "workload": "w", "dry_run": False})
    log.record("a", "action_executed", "x", {"namespace": "n", "workload": "other", "dry_run": False})
    assert log.count_executions("n", "w", timedelta(hours=1)) == 1


def test_redaction_patterns():
    for secret in ["Bearer abcdefghijklmnopqrstuv", "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 30, "sk-" + "b" * 24,
                   "mysql://root:topsecret@db:3306/x", "password: 'pa55w0rd!'"]:
        assert REDACTED in redact(secret), secret
    assert redact_obj({"a": {"api_key": "zzz", "ok": "fine"}}) == {"a": {"api_key": REDACTED, "ok": "fine"}}
    assert redact("nothing secret here") == "nothing secret here"

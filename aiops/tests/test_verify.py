from aiops.agent.verify import evaluate_recovery, wait_for_recovery
from aiops.tests import fakes


def snap(scenario, fixed=False):
    kube, apps = fakes.make_kube(scenario)
    apps.fixed = fixed
    return kube.collect(fakes.NS, fakes.DEP)


def test_broken_workloads_are_not_recovered():
    for sc in ("crash", "oom", "readiness"):
        r = evaluate_recovery(snap(sc))
        assert not r["recovered"]
        assert any(not c["ok"] for c in r["checks"])


def test_healthy_workload_is_recovered():
    r = evaluate_recovery(snap("crash", fixed=True))
    assert r["recovered"], r["checks"]


def test_new_restarts_fail_recovery():
    s = snap("crash", fixed=True)
    s["pods"][0]["containers"][0]["restart_count"] = 3
    r = evaluate_recovery(s, baseline_restarts={f"{fakes.DEP}-rev2-abcde/backend": 1})
    assert not r["recovered"] and any(c["check"] == "no_new_restarts" and not c["ok"] for c in r["checks"])


def test_wait_requires_two_consecutive_healthy_polls():
    seq = [snap("crash"), snap("crash", True), snap("crash"), snap("crash", True), snap("crash", True)]
    calls, t = iter(seq), [0.0]
    r = wait_for_recovery(lambda: next(calls), 60, 5, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert r["status"] == "recovered" and r["waited_s"] == 20.0     # flapping poll 2 did not count


def test_wait_times_out_without_claiming_recovery():
    t = [0.0]
    r = wait_for_recovery(lambda: snap("crash"), 20, 5, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert r["status"] == "not_recovered" and r["recovered"] is False

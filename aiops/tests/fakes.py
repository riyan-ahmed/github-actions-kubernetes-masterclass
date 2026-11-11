"""Fake Kubernetes / Prometheus / Loki APIs built from real client model objects."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
from kubernetes import client as k

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
NS, DEP = "skillpulse-stg", "backend-sim"
REV = "deployment.kubernetes.io/revision"


def _template(port="8080", mem="128Mi", probe="/health"):
    return k.V1PodTemplateSpec(
        metadata=k.V1ObjectMeta(labels={"app": DEP}),
        spec=k.V1PodSpec(containers=[k.V1Container(
            name="backend", image="skillpulse-backend:latest",
            env=[k.V1EnvVar(name="PORT", value=port),
                 k.V1EnvVar(name="DB_PASSWORD", value_from=k.V1EnvVarSource(
                     secret_key_ref=k.V1SecretKeySelector(name="skillpulse-db", key="MYSQL_PASSWORD")))],
            resources=k.V1ResourceRequirements(limits={"memory": mem}, requests={"memory": "64Mi"}),
            readiness_probe=k.V1Probe(http_get=k.V1HTTPGetAction(path=probe, port=8080)))]))


def _rs(rev, template, replicas, ready, name=None):
    return k.V1ReplicaSet(
        metadata=k.V1ObjectMeta(name=name or f"{DEP}-rev{rev}", annotations={REV: str(rev)},
                                creation_timestamp=NOW - timedelta(minutes=30 - rev * 10),
                                owner_references=[k.V1OwnerReference(api_version="apps/v1", kind="Deployment",
                                                                     name=DEP, uid="dep-uid")]),
        spec=k.V1ReplicaSetSpec(selector=k.V1LabelSelector(match_labels={"app": DEP}), template=template),
        status=k.V1ReplicaSetStatus(replicas=replicas, ready_replicas=ready))


def _cs(state, last=None, restarts=0, ready=False):
    return k.V1ContainerStatus(name="backend", image="skillpulse-backend:latest", image_id="", ready=ready,
                               restart_count=restarts, state=state, last_state=last or k.V1ContainerState())


def _term(reason, code, msg=None):
    return k.V1ContainerState(terminated=k.V1ContainerStateTerminated(
        reason=reason, exit_code=code, message=msg, finished_at=NOW))


def _event(reason, message, etype="Warning", kind="Pod", name=f"{DEP}-rev2-abcde", minutes=1):
    return k.CoreV1Event(metadata=k.V1ObjectMeta(name=f"e-{reason}-{minutes}", namespace=NS),
                         involved_object=k.V1ObjectReference(kind=kind, name=name),
                         reason=reason, message=message, type=etype, count=3,
                         last_timestamp=NOW - timedelta(minutes=minutes))


SCENARIOS = {
    "crash": dict(
        template=_template(port="notaport"),
        container=_cs(k.V1ContainerState(waiting=k.V1ContainerStateWaiting(reason="CrashLoopBackOff")),
                      _term("Error", 1), restarts=5),
        ready=False,
        events=[_event("BackOff", "Back-off restarting failed container backend")],
        logs="2026/10/27 SkillPulse\nlisten tcp: address notaport: unknown port\n"),
    "oom": dict(
        template=_template(mem="10Mi"),
        container=_cs(k.V1ContainerState(waiting=k.V1ContainerStateWaiting(reason="CrashLoopBackOff")),
                      _term("OOMKilled", 137), restarts=4),
        ready=False,
        events=[_event("BackOff", "Back-off restarting failed container backend")],
        logs=""),
    "readiness": dict(
        template=_template(probe="/healthz"),
        container=_cs(k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=NOW)), None, 0),
        ready=False,
        events=[_event("Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 404")],
        logs="[GIN] 404 | GET /healthz\n"),
    "healthy": dict(
        template=_template(),
        container=_cs(k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=NOW)), None, 0, ready=True),
        ready=True, events=[], logs=""),
}


class FakeApps:
    def __init__(self, scenario: str, history=True, deployment_labels=None):
        sc = SCENARIOS[scenario]
        self.scenario, self.patches = scenario, []
        self.rs = [_rs(2, sc["template"], 1, 0)]
        if history:
            self.rs.insert(0, _rs(1, _template(), 0, 0))
        self.labels = deployment_labels or {}
        self.fixed = False

    def _dep(self):
        ok = self.fixed
        return k.V1Deployment(
            metadata=k.V1ObjectMeta(name=DEP, namespace=NS, uid="dep-uid", generation=2, labels=self.labels,
                                    annotations={REV: "3" if ok else "2"}),
            spec=k.V1DeploymentSpec(replicas=1, selector=k.V1LabelSelector(match_labels={"app": DEP}),
                                    template=SCENARIOS[self.scenario]["template"]),
            status=k.V1DeploymentStatus(observed_generation=2, ready_replicas=1 if ok else 0,
                                        available_replicas=1 if ok else 0, updated_replicas=1,
                                        conditions=[k.V1DeploymentCondition(type="Available", status="False",
                                                                            reason="MinimumReplicasUnavailable", message="x")]))

    def read_namespaced_deployment(self, name, ns):
        if name != DEP:
            raise k.ApiException(status=404)
        return self._dep()

    def read_namespaced_stateful_set(self, name, ns):
        raise k.ApiException(status=404)

    def list_namespaced_replica_set(self, ns):
        return k.V1ReplicaSetList(items=self.rs)

    def read_namespaced_replica_set(self, name, ns):
        return next(r for r in self.rs if r.metadata.name == name)

    def patch_namespaced_deployment(self, name, ns, body, dry_run=None):
        self.patches.append({"name": name, "ns": ns, "body": body, "dry_run": dry_run})
        if dry_run is None:
            self.fixed = True   # a real patch "heals" the fake cluster


class FakeCore:
    def __init__(self, apps: FakeApps):
        self.apps = apps

    def _pod(self):
        sc = SCENARIOS[self.apps.scenario]
        ready = self.apps.fixed or sc["ready"]
        cs = _cs(k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=NOW)), None, 0, True) \
            if self.apps.fixed else sc["container"]
        return k.V1Pod(
            metadata=k.V1ObjectMeta(name=f"{DEP}-rev2-abcde", namespace=NS, creation_timestamp=NOW,
                                    owner_references=[k.V1OwnerReference(api_version="apps/v1", kind="ReplicaSet",
                                                                         name=f"{DEP}-rev2", uid="rs-uid")]),
            spec=k.V1PodSpec(containers=[], node_name="skillpulse-worker"),
            status=k.V1PodStatus(phase="Running", container_statuses=[cs],
                                 conditions=[k.V1PodCondition(type="Ready", status="True" if ready else "False")]))

    def read_namespaced_pod(self, name, ns):
        return self._pod()

    def list_namespaced_pod(self, ns, label_selector=None):
        return k.V1PodList(items=[self._pod()])

    def list_namespaced_event(self, ns):
        return k.CoreV1EventList(items=SCENARIOS[self.apps.scenario]["events"])

    def read_namespaced_pod_log(self, name, ns, container=None, previous=False, tail_lines=None):
        return SCENARIOS[self.apps.scenario]["logs"]


class FakeRS2(FakeApps):
    """read_namespaced_replica_set must expose the Deployment owner for pod->deployment resolution."""
    def read_namespaced_replica_set(self, name, ns):
        return next((r for r in self.rs if r.metadata.name == name), self.rs[-1])


def make_kube(scenario, history=True, labels=None):
    from aiops.agent.collectors.k8s import KubeClient
    apps = FakeRS2(scenario, history, labels)
    return KubeClient(core=FakeCore(apps), apps=apps), apps


def prom_transport(prev_ready=True, fail=None, oom=False, crash=False):
    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params["query"]
        if fail and fail in q:
            return httpx.Response(500, text="boom")
        res = []
        if "last_terminated_reason" in q and oom:
            res = [{"metric": {"pod": f"{DEP}-rev2-abcde", "reason": "OOMKilled"}, "value": [1.0, "1"]}]
        elif "waiting_reason" in q and crash:
            res = [{"metric": {"pod": f"{DEP}-rev2-abcde", "reason": "CrashLoopBackOff"}, "value": [1.0, "1"]}]
        elif "max_over_time" in q:
            res = [{"metric": {}, "value": [1.0, "1" if prev_ready else "0"]}] if prev_ready else []
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": res}})
    return httpx.MockTransport(handler)


def loki_transport(lines=(), status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="down")
        values = [[str(1_700_000_000_000_000_000 + i), l] for i, l in enumerate(lines)]
        result = [{"stream": {"pod": f"{DEP}-rev2-abcde"}, "values": values}] if values else []
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": result}})
    return httpx.MockTransport(handler)

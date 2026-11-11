#!/usr/bin/env bash
# Rotate the Grafana admin password without ever printing it.
#   scripts/grafana-rotate-admin-password.sh
# The new password is generated in memory, set through the Grafana API (using the current password from the Secret), then
# stored in Secret monitoring-grafana/admin-password so the backup script and operators keep working. If any step fails the
# previous state is restored. Read the new value yourself with:
#   kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d
# Note: the Secret is Helm-managed; a later `helm upgrade` reuses the live value (the chart looks it up), so there is no drift.
set -euo pipefail
CTX="${GRAFANA_CONTEXT:-kind-skillpulse}"
kubectl --context "$CTX" -n monitoring port-forward svc/monitoring-grafana 13004:80 >/dev/null 2>&1 &
PF=$!; trap 'kill $PF 2>/dev/null || true' EXIT
sleep 4
CTX="$CTX" exec python3 - <<'PY'
import base64, json, os, secrets, subprocess, urllib.error, urllib.request

CTX, URL = os.environ["CTX"], "http://127.0.0.1:13004"


def kubectl(*args, stdin=None):
    return subprocess.run(["kubectl", "--context", CTX, "-n", "monitoring", *args], input=stdin, capture_output=True, text=True, check=True).stdout


def secret(key):
    return base64.b64decode(json.loads(kubectl("get", "secret", "monitoring-grafana", "-o", "json"))["data"][key]).decode()


def call(method, path, user, pw, body=None):
    req = urllib.request.Request(URL + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def store(pw):      # patch via stdin so the value never appears in a process argument list
    patch = json.dumps({"data": {"admin-password": base64.b64encode(pw.encode()).decode()}})
    kubectl("patch", "secret", "monitoring-grafana", "--type=merge", "--patch-file=/dev/stdin", stdin=patch)


user, old = secret("admin-user"), secret("admin-password")
assert call("GET", "/api/org", user, old) == 200, "current credentials do not work; refusing to continue"
new = secrets.token_urlsafe(24)
assert call("PUT", "/api/admin/users/1/password", user, old, {"password": new}) == 200, "Grafana refused the change"
try:
    store(new)
except Exception:
    call("PUT", "/api/admin/users/1/password", user, new, {"password": old})      # roll back
    raise SystemExit("could not store the new password; Grafana restored to the previous one")
ok_new, ok_old = call("GET", "/api/org", user, new), call("GET", "/api/org", user, old)
assert secret("admin-password") == new
print(f"new credentials accepted: {ok_new == 200}; old credentials rejected: {ok_old == 401}; Secret updated: True")
if ok_new != 200 or ok_old != 401:
    raise SystemExit("verification failed")
PY

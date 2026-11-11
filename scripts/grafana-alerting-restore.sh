#!/usr/bin/env bash
# Restore alert rules / contact points / policy / mute timings / templates / dashboards that
# scripts/grafana-alerting-backup.sh saved. Run it AFTER the Grafana pod has been recreated.
#
#   scripts/grafana-alerting-restore.sh BACKUP_DIR
#
# Safe by design: objects whose uid/name already exist (for example the new file-provisioned
# aiops-* rules and the aiops-agent contact point) are skipped, never overwritten. The root
# notification policy is only restored if it is still the Grafana default.
# Environment: GRAFANA_URL, GRAFANA_USER, GRAFANA_PASSWORD as in the backup script.
set -euo pipefail
umask 077     # backup files are owner-only

SRC="${1:?usage: $0 BACKUP_DIR}"
PF_PID=""
cleanup() { [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true; }
trap cleanup EXIT
if [ -z "${GRAFANA_URL:-}" ]; then
  kubectl -n monitoring port-forward svc/monitoring-grafana 13001:80 >/dev/null 2>&1 &
  PF_PID=$!
  GRAFANA_URL="http://127.0.0.1:13001"
fi
if [ -z "${GRAFANA_PASSWORD:-}" ]; then
  GRAFANA_USER="$(kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-user}' | base64 -d)"
  GRAFANA_PASSWORD="$(kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d)"
fi
for _ in $(seq 1 60); do curl -fsS -K <(printf 'user = "%s:%s"\n' "${GRAFANA_USER:-admin}" "$GRAFANA_PASSWORD") "$GRAFANA_URL/api/health" >/dev/null 2>&1 && break; sleep 1; done

GF_USER="${GRAFANA_USER:-admin}" GF_PW="$GRAFANA_PASSWORD" python3 - "$SRC" "$GRAFANA_URL" <<'PY'
import base64, json, pathlib, sys, urllib.error, urllib.request

import os
src, url = sys.argv[1:3]
user, pw = os.environ["GF_USER"], os.environ["GF_PW"]
auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def call(method, path, body=None, headers=None):
    h = {"Authorization": auth, "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url + path, method=method, headers=h,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def load(name):
    p = pathlib.Path(src) / name
    return json.loads(p.read_text()) if p.exists() else []


NOPROV = {"X-Disable-Provenance": "true"}      # keep restored objects editable in the UI
report = []

# folders first (rules reference them by uid)
have = {f["uid"] for f in call("GET", "/api/folders")[1]}
for f in load("folders.json"):
    if f["uid"] not in have:
        s, _ = call("POST", "/api/folders", {"uid": f["uid"], "title": f["title"]})
        report.append(("folder", f["title"], s))

have = {c["name"] for c in call("GET", "/api/v1/provisioning/contact-points")[1]}
for c in load("contact-points.json"):
    if c["name"] in have:
        report.append(("contact-point", c["name"], "skipped (exists)"))
        continue
    c.pop("provenance", None)
    s, r = call("POST", "/api/v1/provisioning/contact-points", c, NOPROV)
    report.append(("contact-point", c["name"], s if s < 300 else f"{s} {r}"))

have = {t["name"] for t in call("GET", "/api/v1/provisioning/mute-timings")[1] or []}
for m in load("mute-timings.json"):
    if m["name"] not in have:
        m.pop("provenance", None)
        s, r = call("POST", "/api/v1/provisioning/mute-timings", m, NOPROV)
        report.append(("mute-timing", m["name"], s if s < 300 else f"{s} {r}"))

have = {t["name"] for t in call("GET", "/api/v1/provisioning/templates")[1] or []}
for t in load("templates.json"):
    if t["name"] not in have:
        s, r = call("PUT", f"/api/v1/provisioning/templates/{t['name']}", {"template": t["template"]}, NOPROV)
        report.append(("template", t["name"], s if s < 300 else f"{s} {r}"))

have = {r["uid"] for r in call("GET", "/api/v1/provisioning/alert-rules")[1]}
for r in load("alert-rules.json"):
    if r["uid"] in have:
        report.append(("alert-rule", r["title"], "skipped (exists)"))
        continue
    for internal in ("id", "provenance", "updated"):    # internal ids collide with newly provisioned rules
        r.pop(internal, None)
    s, resp = call("POST", "/api/v1/provisioning/alert-rules", r, NOPROV)
    report.append(("alert-rule", r["title"], s if s < 300 else f"{s} {resp}"))

# root policy: restore only when the current one is still the pristine default
cur = call("GET", "/api/v1/provisioning/policies")[1]
old = load("policies.json")
if old and not cur.get("routes") and old.get("routes"):
    old.pop("provenance", None)
    s, r = call("PUT", "/api/v1/provisioning/policies", old, NOPROV)
    report.append(("notification-policy", "root", s if s < 300 else f"{s} {r}"))
else:
    report.append(("notification-policy", "root", "left unchanged"))

have = {d["uid"] for d in (call("GET", "/api/search?type=dash-db&limit=5000")[1] or [])}
for p in sorted((pathlib.Path(src) / "dashboards").glob("*.json")):
    d = json.loads(p.read_text())
    uid = d["dashboard"]["uid"]
    if uid in have or d["meta"].get("provisioned"):    # sidecar/file-provisioned dashboards come back by themselves
        continue
    d["dashboard"].pop("id", None)
    s, r = call("POST", "/api/dashboards/db", {"dashboard": d["dashboard"], "overwrite": False,
                                              "folderUid": d["meta"].get("folderUid", "")})
    report.append(("dashboard", d["dashboard"]["title"], s if s < 300 else f"{s} {r}"))

# datasources created in the UI (credentials cannot be exported: re-enter them in Grafana)
have = {d["uid"] for d in call("GET", "/api/datasources")[1]}
for d in load("datasources.json"):
    if d["uid"] in have or d.get("readOnly"):
        continue
    body = {k: d.get(k) for k in ("uid", "name", "type", "access", "url", "user", "database", "basicAuth",
                                  "isDefault", "jsonData", "orgId") if d.get(k) is not None}
    s, r = call("POST", "/api/datasources", body)
    report.append(("datasource", d["name"] + " (credentials NOT restored)", s if s < 300 else f"{s} {r}"))

# rule-group evaluation intervals (they are group properties, not rule properties)
for g in load("rule-groups.json"):
    s, cur = call("GET", f"/api/v1/provisioning/folder/{g['folderUid']}/rule-groups/{g['title']}")
    if s == 200 and cur.get("interval") != g["interval"]:
        cur["interval"] = g["interval"]
        s, r = call("PUT", f"/api/v1/provisioning/folder/{g['folderUid']}/rule-groups/{g['title']}", cur, NOPROV)
        report.append(("rule-group interval", g["title"], s if s < 300 else f"{s} {r}"))

# library panels
have = {e["uid"] for e in (call("GET", "/api/library-elements?perPage=500")[1] or {}).get("result", {}).get("elements", [])}
for e in (load("library-elements.json") or {}).get("result", {}).get("elements", []) if isinstance(load("library-elements.json"), dict) else []:
    if e["uid"] in have:
        continue
    s, r = call("POST", "/api/library-elements", {"uid": e["uid"], "folderUid": e.get("folderUid", ""), "name": e["name"],
                                                  "model": e["model"], "kind": e["kind"]})
    report.append(("library-panel", e["name"], s if s < 300 else f"{s} {r}"))

# still-active silences only
import datetime
now = datetime.datetime.now(datetime.timezone.utc)
for sl in load("silences.json"):
    if sl.get("status", {}).get("state") == "expired":
        continue
    if datetime.datetime.fromisoformat(sl["endsAt"].replace("Z", "+00:00")) <= now:
        continue
    s, r = call("POST", "/api/alertmanager/grafana/api/v2/silences",
                {"matchers": sl["matchers"], "startsAt": max(sl["startsAt"], now.isoformat()), "endsAt": sl["endsAt"],
                 "createdBy": sl.get("createdBy", "restore"), "comment": sl.get("comment", "") + " (restored)"})
    report.append(("silence", sl.get("comment", sl["id"])[:40], s if s < 300 else f"{s} {r}"))

prefs = load("preferences.json")
if prefs:
    s, r = call("PUT", "/api/org/preferences", {k: v for k, v in prefs.items() if v not in (None, "")})
    report.append(("org-preferences", "org", s if s < 300 else f"{s} {r}"))

bad = 0
for kind, name, status in report:
    ok = status in (200, 201, 202) or str(status).startswith(("skipped", "left"))
    bad += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {kind:<20} {name}: {status}")
sys.exit(1 if bad else 0)
PY

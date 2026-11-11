#!/usr/bin/env bash
# Back up Grafana alerting + dashboards to a local directory BEFORE any Grafana upgrade.
# Read-only against Grafana. Grafana's storage is an emptyDir, so a pod restart deletes
# everything that was created in the UI; this backup is the only copy.
#
#   scripts/grafana-alerting-backup.sh [OUT_DIR]
#
# Environment:
#   GRAFANA_URL   if set, talk to this URL directly (used for local tests); otherwise the
#                 script port-forwards svc/monitoring-grafana in namespace monitoring.
#   GRAFANA_USER / GRAFANA_PASSWORD   if unset, read from Secret monitoring-grafana (kubectl).
set -euo pipefail
umask 077     # backup files are owner-only

OUT="${1:-aiops/audit/grafana-backup-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"; chmod 700 "$OUT"

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
curlcfg() { printf 'user = "%s:%s"\n' "${GRAFANA_USER:-admin}" "$GRAFANA_PASSWORD"; }   # fed via -K <(...): not visible in ps

for _ in $(seq 1 30); do curl -fsS -K <(curlcfg) "$GRAFANA_URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
get() { curl -fsS -K <(curlcfg) "$GRAFANA_URL$1"; }

get /api/v1/provisioning/alert-rules          > "$OUT/alert-rules.json"
get /api/v1/provisioning/contact-points       > "$OUT/contact-points.json"
get /api/v1/provisioning/policies             > "$OUT/policies.json"
get /api/alertmanager/grafana/config/api/v1/alerts > "$OUT/alertmanager-config.json"   # full record incl. receivers with no integrations (e.g. built-in "empty")
get /api/v1/provisioning/mute-timings         > "$OUT/mute-timings.json"
get /api/v1/provisioning/templates            > "$OUT/templates.json"
get /api/folders                              > "$OUT/folders.json"
get /api/datasources                          > "$OUT/datasources.json"
# human-readable, re-provisionable exports
get "/api/v1/provisioning/alert-rules/export?format=yaml"    > "$OUT/alert-rules.export.yaml"
get "/api/v1/provisioning/contact-points/export?format=yaml" > "$OUT/contact-points.export.yaml"
get "/api/v1/provisioning/policies/export?format=yaml"       > "$OUT/policies.export.yaml"

mkdir -p "$OUT/dashboards"
get "/api/search?type=dash-db&limit=5000" > "$OUT/dashboards-index.json"
GF_USER="${GRAFANA_USER:-admin}" GF_PW="$GRAFANA_PASSWORD" python3 - "$OUT" "$GRAFANA_URL" <<'PY'
import base64, json, pathlib, sys, urllib.request
import os
out, url = sys.argv[1:3]
user, pw = os.environ["GF_USER"], os.environ["GF_PW"]
hdr = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
for d in json.load(open(f"{out}/dashboards-index.json")):
    req = urllib.request.Request(f"{url}/api/dashboards/uid/{d['uid']}", headers=hdr)
    pathlib.Path(f"{out}/dashboards/{d['uid']}.json").write_text(urllib.request.urlopen(req).read().decode())
PY

# --- everything else that can live only in Grafana's emptyDir ---------------------------------
get "/api/library-elements?perPage=500"                  > "$OUT/library-elements.json"
get /api/alertmanager/grafana/api/v2/silences            > "$OUT/silences.json"
get /api/org/preferences                                 > "$OUT/preferences.json"
get "/api/teams/search?perpage=1000"                     > "$OUT/inventory-teams.json"        # export only
get /api/org/users                                       > "$OUT/inventory-users.json"        # export only
get "/api/serviceaccounts/search?perpage=1000"           > "$OUT/inventory-service-accounts.json"  # export only
GF_USER="${GRAFANA_USER:-admin}" GF_PW="$GRAFANA_PASSWORD" python3 - "$OUT" "$GRAFANA_URL" <<'PY'
# rule-group evaluation intervals are a property of the group, not of the individual rule
import base64, json, sys, urllib.parse, urllib.request
import os
out, url = sys.argv[1:3]
user, pw = os.environ["GF_USER"], os.environ["GF_PW"]
hdr = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
groups, seen = [], set()
for r in json.load(open(f"{out}/alert-rules.json")):
    key = (r["folderUID"], r["ruleGroup"])
    if key in seen:
        continue
    seen.add(key)
    u = f"{url}/api/v1/provisioning/folder/{urllib.parse.quote(key[0])}/rule-groups/{urllib.parse.quote(key[1])}"
    g = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=hdr)))
    groups.append({"folderUid": key[0], "title": key[1], "interval": g["interval"]})
json.dump(groups, open(f"{out}/rule-groups.json", "w"), indent=1)
PY

python3 - "$OUT" <<'PY'
import json, sys
o = sys.argv[1]
rules = json.load(open(f"{o}/alert-rules.json")); cps = json.load(open(f"{o}/contact-points.json"))
print(f"backup written to {o}: {len(rules)} alert rules, {len(cps)} contact points, "
      f"{len(json.load(open(f'{o}/dashboards-index.json')))} dashboards, "
      f"{len(json.load(open(f'{o}/datasources.json')))} datasources, "
      f"{len(json.load(open(f'{o}/library-elements.json'))['result']['elements'])} library panels, "
      f"{len(json.load(open(f'{o}/silences.json')))} silences")
PY
echo "NOTE: Grafana redacts secret fields (contact-point and datasource credentials). Re-enter them after a restore."
echo "NOTE: users/teams/service accounts are exported to inventory-*.json for reference only; they are NOT restored"
echo "      (passwords/tokens cannot be exported). Recreate them manually if you created any in the UI."

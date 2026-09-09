#!/usr/bin/env bash
# add-participant.sh — provision a Water Plant lab participant
#
# Creates a per-participant Keycloak realm and registers that realm's issuer
# with the MCP Gateway authentication and authorization policies.
#
# Usage:
#   ./deploy/scripts/add-participant.sh <participant-name>
#
# Example:
#   ./deploy/scripts/add-participant.sh alice
#
# What it does:
#   1. Renders the realm template and applies it to the cluster
#   2. Waits for the realm import to complete
#   3. Adds the realm's issuer URL to mcp-auth-policy  (public listener JWT validation)
#   4. Adds the realm's issuer URL to mcp-authz-policy (internal listener JWT validation)
#   5. Appends the issuer URL to the MCPGatewayExtension authorizationServers list
#
# Prerequisites:
#   - oc logged in with cluster-admin or sufficient rights to patch in mcp-gateway
#   - Keycloak running in wp-dev (deployed via deploy/keycloak/)
#   - MCP Gateway and Kuadrant auth/authz policies already installed

set -euo pipefail

NAME="${1:?Usage: $(basename "$0") <participant-name>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

KEYCLOAK_HOST="keycloak-wp-dev.apps.caiprod.rhoai.rh-aiservices-bu.com"
KEYCLOAK_URL="https://${KEYCLOAK_HOST}"
REALM="waterplant-${NAME}"
ISSUER_URL="${KEYCLOAK_URL}/realms/${REALM}"

echo "==> Provisioning participant: ${NAME}"
echo "    Realm:  ${REALM}"
echo "    Issuer: ${ISSUER_URL}"
echo ""

# ── 1. Create the Keycloak realm ─────────────────────────────────────────────
echo "[1/5] Applying Keycloak realm import..."
sed "s/\${PARTICIPANT_NAME}/${NAME}/g" \
  "${REPO_ROOT}/deploy/keycloak/realms/waterplant-realm-template.yaml" \
  | oc apply -f -

# ── 2. Wait for realm import to complete ─────────────────────────────────────
# Poll rather than using 'oc wait --for=condition=Done' to avoid a race where
# the condition is already True before the watch is established.
echo "[2/5] Waiting for realm import to complete (up to 90s)..."
DEADLINE=$(( $(date +%s) + 90 ))
while true; do
  DONE=$(oc get keycloakrealmimport "${REALM}" -n wp-dev \
    -o jsonpath='{.status.conditions[?(@.type=="Done")].status}' 2>/dev/null || true)
  HAS_ERRORS=$(oc get keycloakrealmimport "${REALM}" -n wp-dev \
    -o jsonpath='{.status.conditions[?(@.type=="HasErrors")].status}' 2>/dev/null || true)
  if [ "${HAS_ERRORS}" = "True" ]; then
    echo "ERROR: Realm import failed. Check: oc describe keycloakrealmimport ${REALM} -n wp-dev"
    exit 1
  fi
  if [ "${DONE}" = "True" ]; then
    echo "    Realm import complete."
    break
  fi
  if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
    echo "ERROR: Timed out waiting for realm import. Check: oc describe keycloakrealmimport ${REALM} -n wp-dev"
    exit 1
  fi
  sleep 3
done

# ── 3. Patch mcp-auth-policy (public listener) ───────────────────────────────
echo "[3/5] Adding issuer to mcp-auth-policy..."
oc patch authpolicy mcp-auth-policy -n mcp-gateway \
  --type=merge \
  --patch "{\"spec\":{\"defaults\":{\"rules\":{\"authentication\":{\"${REALM}\":{\"jwt\":{\"issuerUrl\":\"${ISSUER_URL}\"}}}}}}}";

# ── 4. Patch mcp-authz-policy (internal mcps listener) ───────────────────────
echo "[4/5] Adding issuer to mcp-authz-policy..."
oc patch authpolicy mcp-authz-policy -n mcp-gateway \
  --type=merge \
  --patch "{\"spec\":{\"rules\":{\"authentication\":{\"${REALM}\":{\"jwt\":{\"issuerUrl\":\"${ISSUER_URL}\"},\"when\":[{\"predicate\":\"request.headers.exists(h, h == \\\"x-mcp-toolname\\\")\"}]}}}}}"

# ── 5. Append issuer to MCPGatewayExtension ───────────────────────────────────
echo "[5/5] Appending issuer to MCPGatewayExtension..."
CURRENT_SERVERS=$(oc get mcpgatewayextension mcp-extension -n mcp-gateway \
  -o jsonpath='{.spec.oauthProtectedResource.authorizationServers}')

if echo "${CURRENT_SERVERS}" | grep -qF "${ISSUER_URL}"; then
  echo "    Already present — skipping."
else
  oc patch mcpgatewayextension mcp-extension -n mcp-gateway \
    --type=json \
    --patch "[{\"op\":\"add\",\"path\":\"/spec/oauthProtectedResource/authorizationServers/-\",\"value\":\"${ISSUER_URL}\"}]"
fi

echo ""
echo "Done!  Participant '${NAME}' is ready."
echo ""
echo "  Keycloak admin console: ${KEYCLOAK_URL}/admin/${REALM}/console"
echo "  Operator login:         username=operator  password=operator"
echo ""
echo "  The operator persona has access to:"
echo "    Telemetry:   all 6 read-only tools"
echo "    Maintenance: all 5 work-order tools"
echo "    Control:     set_pump_speed, start/stop_pump, open/close_valve"
echo "    (NOT: dump_plant_configuration, emergency_shutdown — see Scenarios 3 & 4)"

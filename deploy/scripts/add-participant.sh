#!/usr/bin/env bash
# add-participant.sh — provision a Water Plant lab participant
#
# Creates a per-participant Keycloak realm and registers that realm's JWT issuer
# with the MCP Gateway auth policy and the per-server authz policies.
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
#   3. Adds the realm's issuer to mcp-auth-policy        (JWT validation, public listener)
#   4. Adds the realm's issuer to telemetry-mcp-authz    (tool-level authz, per-route)
#   5. Adds the realm's issuer to maintenance-mcp-authz  (tool-level authz, per-route)
#   6. Adds the realm's issuer to control-mcp-authz      (tool-level authz, per-route)
#   7. Appends the realm's issuer to MCPGatewayExtension (OAuth metadata)
#   8. Creates admin/redhat user in participant realm with realm-admin rights
#   9. If the hermes-openshell chart is installed: provisions this
#      participant's Hermes-OpenShell sandbox (see hermes/README.md)
#
# Prerequisites:
#   - oc logged in with rights to patch in both wp-dev and mcp-gateway namespaces
#   - Keycloak running in wp-dev (deploy/keycloak/)
#   - MCP Gateway running with mcp-auth-policy already applied
#   - Per-server authz policies applied (oc apply -k deploy/authz/)

set -euo pipefail

NAME="${1:?Usage: $(basename "$0") <participant-name>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

KEYCLOAK_HOST=$(oc get route keycloak -n wp-dev -o jsonpath='{.spec.host}')
KEYCLOAK_URL="https://${KEYCLOAK_HOST}"
REALM="waterplant-${NAME}"
ISSUER_URL="${KEYCLOAK_URL}/realms/${REALM}"

echo "==> Provisioning participant: ${NAME}"
echo "    Realm:  ${REALM}"
echo "    Issuer: ${ISSUER_URL}"
echo ""

# ── 1. Create the Keycloak realm ─────────────────────────────────────────────
# Also provisions wp-dev/hermes-agent's client secret (rotated each run).
HERMES_AGENT_CLIENT_SECRET=$(openssl rand -hex 24)

echo "[1/9] Applying Keycloak realm import..."
sed -e "s/\${PARTICIPANT_NAME}/${NAME}/g" -e "s/\${HERMES_AGENT_CLIENT_SECRET}/${HERMES_AGENT_CLIENT_SECRET}/g" "${REPO_ROOT}/deploy/keycloak/realms/waterplant-realm-template.yaml" | oc apply -f -

# ── 2. Wait for realm import ──────────────────────────────────────────────────
echo "[2/9] Waiting for realm import to complete (up to 90s)..."
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
    echo "ERROR: Timed out waiting for realm import."
    exit 1
  fi
  sleep 3
done

# ── Helper: patch a JWT issuer entry into an AuthPolicy authentication map ───
patch_authpolicy() {
  local name="$1" namespace="$2"
  echo "    Patching ${namespace}/${name}..."
  oc patch authpolicy "${name}" -n "${namespace}" \
    --type=merge \
    --patch "{\"spec\":{\"rules\":{\"authentication\":{\"${REALM}\":{\"jwt\":{\"issuerUrl\":\"${ISSUER_URL}\"},\"when\":[{\"predicate\":\"request.headers.exists(h, h == \\\"x-mcp-toolname\\\")\"}]}}}}}"
}

# ── Helper: same but for the gateway-level auth policy (different spec path) ─
patch_auth_policy() {
  local name="$1" namespace="$2"
  echo "    Patching ${namespace}/${name}..."
  oc patch authpolicy "${name}" -n "${namespace}" \
    --type=merge \
    --patch "{\"spec\":{\"defaults\":{\"rules\":{\"authentication\":{\"${REALM}\":{\"jwt\":{\"issuerUrl\":\"${ISSUER_URL}\"}}}}}}}"
}

# ── 3. mcp-auth-policy (public mcp listener, mcp-gateway namespace) ──────────
echo "[3/9] Patching mcp-auth-policy..."
patch_auth_policy mcp-auth-policy mcp-gateway

# ── 4–6. Per-route authz policies (wp-dev namespace) ─────────────────────────
echo "[4/9] Patching telemetry-mcp-authz..."
patch_authpolicy telemetry-mcp-authz wp-dev

echo "[5/9] Patching maintenance-mcp-authz..."
patch_authpolicy maintenance-mcp-authz wp-dev

echo "[6/9] Patching control-mcp-authz..."
patch_authpolicy control-mcp-authz wp-dev

# ── 7. MCPGatewayExtension authorizationServers ───────────────────────────────
echo "[7/9] Appending issuer to MCPGatewayExtension..."
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
echo "  Realm admin login:      username=${NAME}-admin  password=redhat"
echo "  Operator login:         username=operator       password=operator"
echo ""
echo "  The operator persona has access to:"
echo "    Telemetry:   all 6 read-only tools"
echo "    Maintenance: all 5 work-order tools"
echo "    Control:     set_pump_speed, start/stop_pump, open/close_valve"
echo "    (NOT: dump_plant_configuration, emergency_shutdown — see Scenarios 3 & 4)"

# ── 8. Create realm admin user in participant realm ───────────────────────────
# Username: <name>-admin  Password: redhat
# Grants full realm-admin rights; logs in at /admin/waterplant-<name>/console
ADMIN_USERNAME="${NAME}-admin"
echo "[8/9] Creating realm admin user (${ADMIN_USERNAME}/redhat) in ${REALM}..."

TEMP_USER=$(oc get secret keycloak-initial-admin -n wp-dev -o jsonpath='{.data.username}' | base64 -d)
TEMP_PASS=$(oc get secret keycloak-initial-admin -n wp-dev -o jsonpath='{.data.password}' | base64 -d)

# Get master-realm admin token (temp-admin can manage master realm)
KC_TOKEN=$(curl -sk "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=${TEMP_USER}&password=${TEMP_PASS}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Create the admin user with full profile so Keycloak doesn't prompt for details on first login
ADMIN_PAYLOAD=$(printf '{"username":"%s","enabled":true,"emailVerified":true,"firstName":"Realm","lastName":"Admin","email":"admin@waterplant.local","credentials":[{"type":"password","value":"redhat","temporary":false}]}' "${ADMIN_USERNAME}")
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" \
  -X POST "${KEYCLOAK_URL}/admin/realms/${REALM}/users" \
  -H "Authorization: Bearer ${KC_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${ADMIN_PAYLOAD}")

if [ "${HTTP}" = "201" ]; then
  echo "    User created."
elif [ "${HTTP}" = "409" ]; then
  echo "    User already exists — skipping creation."
else
  echo "    WARNING: unexpected HTTP ${HTTP} creating admin user"
fi

# Locate the user
ADMIN_ID=$(curl -sk "${KEYCLOAK_URL}/admin/realms/${REALM}/users?username=${ADMIN_USERNAME}" \
  -H "Authorization: Bearer ${KC_TOKEN}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if isinstance(d, list) and d else '')")

if [ -n "${ADMIN_ID}" ]; then
  # Get the realm-management client UUID (built-in, always present)
  RM_CLIENT_ID=$(curl -sk "${KEYCLOAK_URL}/admin/realms/${REALM}/clients?clientId=realm-management" \
    -H "Authorization: Bearer ${KC_TOKEN}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if isinstance(d, list) and d else '')")

  # Get the realm-admin composite role (grants full realm management)
  REALM_ADMIN_ROLE=$(curl -sk \
    "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${RM_CLIENT_ID}/roles/realm-admin" \
    -H "Authorization: Bearer ${KC_TOKEN}")

  # Assign realm-admin to the admin user
  curl -sk -X POST \
    "${KEYCLOAK_URL}/admin/realms/${REALM}/users/${ADMIN_ID}/role-mappings/clients/${RM_CLIENT_ID}" \
    -H "Authorization: Bearer ${KC_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "[${REALM_ADMIN_ROLE}]" > /dev/null

  echo "    realm-admin role assigned."
else
  echo "    WARNING: could not locate admin user — skipping role assignment"
fi

# ── 9. Provision this participant's Hermes-OpenShell sandbox (optional) ──────
# Skipped (not failed) if the hermes-openshell chart isn't installed.
echo ""
if oc get configmap hermes-openshell-scripts -n wp-dev &>/dev/null; then
  echo "[9/9] Provisioning Hermes-OpenShell sandbox for '${NAME}'..."
  HERMES_TOKEN_URL="${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token"
  # api_server's own bearer key — see gateway/platforms/api_server.py.
  API_SERVER_KEY=$(openssl rand -hex 24)
  # Hermes authenticates to MCP as the realm's "operator" user (password
  # grant via mcp-gateway), not wp-dev/hermes-agent's full-access service
  # account — caps Hermes at the operator's own tool scope. See hermes/README.md.
  oc create secret generic "hermes-mcp-auth-${NAME}" -n wp-dev \
    --from-literal=MCP_CLIENT_ID="mcp-gateway" \
    --from-literal=MCP_USERNAME="operator" \
    --from-literal=MCP_PASSWORD="operator" \
    --from-literal=MCP_TOKEN_URL="${HERMES_TOKEN_URL}" \
    --from-literal=API_SERVER_KEY="${API_SERVER_KEY}" \
    --dry-run=client -o yaml | oc apply -f -
  "${SCRIPT_DIR}/provision-hermes-sandbox.sh" "${NAME}"
else
  echo "[9/9] hermes-openshell chart not installed in wp-dev — skipping Hermes provisioning."
  echo "       (helm install hermes-openshell hermes/chart -n wp-dev, then re-run this script)"
fi

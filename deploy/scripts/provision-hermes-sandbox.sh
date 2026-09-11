#!/usr/bin/env bash
# provision-hermes-sandbox.sh — create/update one participant's Hermes-OpenShell sandbox.
#
# Called by add-participant.sh after it has applied that participant's Keycloak
# realm (including the wp-dev/hermes-agent client — see
# deploy/keycloak/realms/waterplant-realm-template.yaml) and created the
# hermes-mcp-auth-<name> Secret holding that client's credentials.
#
# Requires: hermes-openshell chart already installed (helm install hermes-openshell
# hermes/chart -n wp-dev) — see hermes/README.md and deploy/README.md.
#
# Usage:
#   ./deploy/scripts/provision-hermes-sandbox.sh <participant-name>

set -euo pipefail

NAME="${1:?Usage: $(basename "$0") <participant-name>}"
NAMESPACE="wp-dev"
SANDBOX_NAME="hermes-${NAME}"
JOB_NAME="hermes-setup-${NAME}"
REALM="waterplant-${NAME}"

echo "==> Provisioning Hermes sandbox for: ${NAME}"

# ── Read chart-provided, non-secret config from the ConfigMap ───────────────
PROVISION_ENV=$(oc get configmap hermes-openshell-scripts -n "${NAMESPACE}" \
  -o jsonpath='{.data.provision\.env}' 2>/dev/null || true)
if [ -z "${PROVISION_ENV}" ]; then
  echo "ERROR: ConfigMap hermes-openshell-scripts not found in ${NAMESPACE}." >&2
  echo "       Install the chart first: helm install hermes-openshell hermes/chart -n ${NAMESPACE}" >&2
  exit 1
fi
eval "${PROVISION_ENV}"   # sets IMAGE_REF, LLM_API_KEY_SECRET_NAME/KEY, LLM_MODEL,
                          # MCP_SERVER_NAME, ADAPTER_PORT, OPENSHELL_CLI_VERSION

if [ -z "${LLM_API_KEY_SECRET_NAME:-}" ]; then
  echo "ERROR: hermes-openshell chart was installed without llm.apiKeySecretRef.name set." >&2
  exit 1
fi
if ! oc get secret "${LLM_API_KEY_SECRET_NAME}" -n "${NAMESPACE}" &>/dev/null; then
  echo "ERROR: Secret ${LLM_API_KEY_SECRET_NAME} (llm.apiKeySecretRef.name) not found in ${NAMESPACE}." >&2
  exit 1
fi

if ! oc get secret "hermes-mcp-auth-${NAME}" -n "${NAMESPACE}" &>/dev/null; then
  echo "ERROR: Secret hermes-mcp-auth-${NAME} not found — run add-participant.sh first." >&2
  exit 1
fi

KEYCLOAK_HOST=$(oc get route keycloak -n "${NAMESPACE}" -o jsonpath='{.spec.host}')

# ── Render and run the setup Job ─────────────────────────────────────────────
echo "    Sandbox: ${SANDBOX_NAME}"
echo "    Image:   ${IMAGE_REF}"
echo "    Realm:   ${REALM}"

oc delete job "${JOB_NAME}" -n "${NAMESPACE}" --ignore-not-found

cat <<EOF | oc apply -n "${NAMESPACE}" -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  labels:
    app.kubernetes.io/name: hermes-openshell
    app.kubernetes.io/instance: ${NAME}
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 600
  template:
    spec:
      serviceAccountName: hermes-openshell-installer
      restartPolicy: Never
      initContainers:
        - name: get-openshell
          image: registry.access.redhat.com/ubi9/ubi
          command:
            - sh
            - -c
            - |
              curl -fsSL "https://github.com/NVIDIA/OpenShell/releases/download/${OPENSHELL_CLI_VERSION}/openshell-x86_64-unknown-linux-musl.tar.gz" \
                  -o /tmp/openshell.tar.gz
              tar -xzf /tmp/openshell.tar.gz -C /tmp
              mv /tmp/openshell /tools/openshell
              chmod +x /tools/openshell
          volumeMounts:
            - name: tools
              mountPath: /tools
      containers:
        - name: setup
          image: quay.io/openshift/origin-cli:latest
          command:
            - bash
            - -c
            - |
              set -euo pipefail
              export PATH="/tools:\$PATH"
              export HOME=/tmp
              export XDG_CONFIG_HOME=/tmp/.config

              step() { echo ""; echo "=== \$* ==="; }

              step "Install gateway mTLS client cert"
              # The gateway's TLS listener requires a client cert unconditionally
              # once TLS is on (confirmed live: a request with no cert fails at
              # the TLS handshake itself, not with a 401 — allowUnauthenticatedUsers
              # only affects application-level auth, not the transport). The CLI
              # looks for it at this exact path, keyed by the sanitized gateway
              # name passed to 'gateway add --name' below — see
              # crates/openshell-cli/src/tls.rs.
              mkdir -p "\${XDG_CONFIG_HOME}/openshell/gateways/openshift/mtls"
              cp /mtls/ca.crt /mtls/tls.crt /mtls/tls.key "\${XDG_CONFIG_HOME}/openshell/gateways/openshift/mtls/"

              step "Register gateway"
              openshell gateway add "https://openshell.${NAMESPACE}.svc.cluster.local:8080" --local --name openshift
              openshell gateway select openshift

              step "Register LLM provider"
              openshell provider delete hermes-llm 2>/dev/null || true
              openshell provider create \
                  --name hermes-llm \
                  --type openai \
                  --credential "OPENAI_API_KEY=\${OPENAI_API_KEY}" \
                  --config "base_url=\${OPENAI_BASE_URL}"

              step "Create sandbox: ${SANDBOX_NAME}"
              openshell sandbox delete "${SANDBOX_NAME}" 2>/dev/null || true
              sleep 3
              # The trailing verification command (-- echo ready) is known to
              # sometimes error immediately after creation even though the
              # sandbox itself was created successfully (confirmed against the
              # proven cai-crew deployment's own setup script, which has the
              # same || true here) — don't let set -e abort the whole job on it.
              openshell sandbox create --name "${SANDBOX_NAME}" --from "${IMAGE_REF}" -- echo ready 2>&1 || true

              step "Wait for sandbox Ready"
              for i in \$(seq 1 30); do
                  STATUS=\$(openshell sandbox list 2>/dev/null | grep "${SANDBOX_NAME}" \
                      | sed 's/\x1b\[[0-9;]*m//g' | awk '{print \$NF}')
                  [ "\$STATUS" = "Ready" ] && echo "Sandbox is Ready" && sleep 5 && break
                  [ "\$i" -eq 30 ] && echo "ERROR: sandbox not Ready after 150s" >&2 && exit 1
                  sleep 5
              done

              step "Render config.yaml"
              sed "s|\\\${OPENAI_API_KEY}|\${OPENAI_API_KEY}|g" \
                  /scripts/hermes-config.yaml.template > /tmp/config.yaml
              CONFIG_B64=\$(base64 -w0 /tmp/config.yaml)
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  "mkdir -p /sandbox/.hermes && printf '%s' '\${CONFIG_B64}' | base64 -d > /sandbox/.hermes/config.yaml && echo config-ok"

              step "Upload SOUL.md"
              SOUL_B64=\$(base64 -w0 /scripts/SOUL.md)
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  "printf '%s' '\${SOUL_B64}' | base64 -d > /sandbox/.hermes/SOUL.md && echo soul-ok"

              step "Upload mcp-token-refresh.py"
              # chat-adapter.py and the cached openshell CLI binary are not
              # uploaded in this build — hermes gateway run's own api_server
              # platform is the reachable process now (see the "Start hermes
              # gateway run" step below), not a per-turn oneshot wrapper that
              # needed to shell back out to openshell sandbox exec.
              for f in mcp-token-refresh.py; do
                  SRC="/scripts/\${f}"
                  F_B64=\$(base64 -w0 "\${SRC}")
                  # split -b keeps each sandbox-exec argument under the ~32KB limit
                  split -b 20000 <(printf '%s' "\${F_B64}") /tmp/chunk_
                  FIRST=1
                  for CHUNK_FILE in /tmp/chunk_*; do
                      CHUNK=\$(cat "\${CHUNK_FILE}")
                      if [ "\${FIRST}" = "1" ]; then
                          openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c "printf '%s' '\${CHUNK}' > /tmp/f.b64"
                          FIRST=0
                      else
                          openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c "printf '%s' '\${CHUNK}' >> /tmp/f.b64"
                      fi
                  done
                  rm -f /tmp/chunk_*
                  openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                      "base64 -d /tmp/f.b64 > /sandbox/\${f} && chmod +x /sandbox/\${f} && rm /tmp/f.b64 && echo \${f}-ok"
              done

              step "Write environment init script"
              cat > /tmp/sandbox-init.sh <<INITEOF
              #!/bin/sh
              export HERMES_HOME=/sandbox/.hermes
              export OPENAI_API_KEY='\${OPENAI_API_KEY}'
              export OPENAI_BASE_URL='\${OPENAI_BASE_URL}'
              export OPENAI_MODEL='\${OPENAI_MODEL}'
              export ADAPTER_PORT='${ADAPTER_PORT}'
              export MCP_CLIENT_ID='\${MCP_CLIENT_ID}'
              export MCP_CLIENT_SECRET='\${MCP_CLIENT_SECRET}'
              export MCP_TOKEN_URL='\${MCP_TOKEN_URL}'
              export MCP_SERVER_NAME='${MCP_SERVER_NAME}'
              export SANDBOX_NAME='${SANDBOX_NAME}'
              export API_SERVER_ENABLED=true
              # Loopback only: openshell service expose relays to 127.0.0.1
              # inside the sandbox's own network namespace, not 0.0.0.0 on
              # whichever namespace a plain 'oc exec' would attach to (those
              # are two different loopback interfaces — confirmed live, this
              # is why the previous 'oc exec'-started process was unreachable
              # via the relay even though curl-from-inside-the-pod could see
              # it fine).
              export API_SERVER_HOST=127.0.0.1
              export API_SERVER_PORT='${ADAPTER_PORT}'
              export API_SERVER_KEY='\${API_SERVER_KEY}'
              export SANDBOX_ENV_LOADED=1
              INITEOF
              INIT_B64=\$(base64 -w0 /tmp/sandbox-init.sh)
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  "printf '%s' '\${INIT_B64}' | base64 -d > /sandbox/.sandbox-init.sh && chmod +x /sandbox/.sandbox-init.sh && echo init-ok"

              step "Render and apply sandbox policy"
              sed "s/\\\${KEYCLOAK_HOST}/${KEYCLOAK_HOST}/g" \
                  /scripts/policy-standard.yaml.template > /tmp/policy.yaml
              openshell policy set --policy /tmp/policy.yaml --wait "${SANDBOX_NAME}"

              step "Start mcp-token-refresh.py (seed) + background loop"
              # Stays on 'openshell sandbox exec' — it's a background task with
              # no inbound-reachability need, so it should stay in the
              # policy-enforced namespace (only the Keycloak token endpoint is
              # allowlisted for it).
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  ". /sandbox/.sandbox-init.sh && python3 /sandbox/mcp-token-refresh.py"
              # Wrapped in its own subshell "( ... & )" — confirmed live that this,
              # not setsid/nohup/</dev/null alone, is what's needed: a container
              # runtime's exec implementation waits for ALL processes sharing its
              # exec session (cgroup-level, not just open file descriptors) to
              # exit before releasing the connection. A bare "cmd &" still leaves
              # the backgrounded process attributed to this exec session; forking
              # it inside its own subshell (which itself exits immediately once
              # the fork completes) reparents it to PID 1 right away, fully
              # detached — confirmed via ps and by the exec call returning
              # promptly instead of hanging indefinitely.
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  ". /sandbox/.sandbox-init.sh && (setsid sh -c 'python3 /sandbox/mcp-token-refresh.py --loop 1200 < /dev/null > /sandbox/mcp-token-refresh.log 2>&1' < /dev/null > /dev/null 2>&1 &) ; echo refresher-started"

              step "Start hermes gateway run (openshell sandbox exec — policy-enforced)"
              # Routed through openshell sandbox exec, not plain oc exec: this
              # puts the process in the sandbox's own Landlock-policy-enforced
              # network namespace, which is what openshell service expose
              # relays into (127.0.0.1 there, not on the pod's default netns).
              # Same subshell+setsid daemonization pattern as the token
              # refresher above — required for the same reason (see its
              # comment): otherwise the exec call hangs waiting for the
              # backgrounded process to exit.
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  ". /sandbox/.sandbox-init.sh && (setsid sh -c 'hermes gateway run < /dev/null > /sandbox/hermes-gateway.log 2>&1' < /dev/null > /dev/null 2>&1 &) ; sleep 1 && echo gateway-started"

              step "Verify hermes gateway run is up (from inside its own netns)"
              sleep 5
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  "ps aux | grep -v grep | grep 'hermes gateway' && echo gateway-running || echo 'WARNING: hermes gateway run not running'"
              openshell sandbox exec --name "${SANDBOX_NAME}" -- /bin/sh -c \
                  "curl -sf --max-time 5 http://127.0.0.1:${ADAPTER_PORT}/health && echo '' && echo health-ok || echo 'WARNING: api_server /health not responding yet'"

              step "Expose it through the gateway's service relay"
              openshell service expose "${SANDBOX_NAME}" "${ADAPTER_PORT}" openai
              RELAY_URL="https://default--${SANDBOX_NAME}--openai.openshell.localhost:8080/"
              echo "Relay URL: \${RELAY_URL}"

              step "Verify the relay end-to-end (mTLS, from this Job's own pod — a non-loopback caller relative to the gateway, same position waterplant-ui will be in)"
              GATEWAY_IP=\$(getent hosts "openshell.${NAMESPACE}.svc.cluster.local" | awk '{print \$1}')
              curl -sk --max-time 10 -o /dev/null -w '%{http_code}\n' \
                  --resolve "default--${SANDBOX_NAME}--openai.openshell.localhost:8080:\${GATEWAY_IP}" \
                  --cacert "\${XDG_CONFIG_HOME}/openshell/gateways/openshift/mtls/ca.crt" \
                  --cert "\${XDG_CONFIG_HOME}/openshell/gateways/openshift/mtls/tls.crt" \
                  --key "\${XDG_CONFIG_HOME}/openshell/gateways/openshift/mtls/tls.key" \
                  -X POST "\${RELAY_URL}v1/chat/completions" \
                  -H "content-type: application/json" \
                  -H "authorization: Bearer \${API_SERVER_KEY}" \
                  -d '{"model":"hermes","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
                  | grep -q '^200$' && echo relay-verified || echo 'WARNING: relay verification did not return 200'

              echo ""
              echo "Sandbox ${SANDBOX_NAME} ready. Relay URL: \${RELAY_URL}"
          envFrom:
            - secretRef:
                name: hermes-mcp-auth-${NAME}
          env:
            - name: OPENAI_BASE_URL
              value: "${LLM_BASE_URL}"
            - name: OPENAI_MODEL
              value: "${LLM_MODEL}"
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: ${LLM_API_KEY_SECRET_NAME}
                  key: ${LLM_API_KEY_SECRET_KEY}
          volumeMounts:
            - name: tools
              mountPath: /tools
            - name: scripts
              mountPath: /scripts
            - name: mtls
              mountPath: /mtls
      volumes:
        - name: tools
          emptyDir: {}
        - name: scripts
          configMap:
            name: hermes-openshell-scripts
            defaultMode: 0755
        - name: mtls
          secret:
            secretName: openshell-client-tls
EOF

echo "    Waiting for job/${JOB_NAME}..."
oc logs -f "job/${JOB_NAME}" -n "${NAMESPACE}" || true
oc wait --for=condition=complete "job/${JOB_NAME}" -n "${NAMESPACE}" --timeout=600s

# ── Reachability is now via the OpenShell gateway's service relay, not a
# Kubernetes Service — the sandbox's process runs in a policy-enforced
# network namespace with no direct Pod-IP route from other pods; the gateway
# relays into it over the same outbound session used for `sandbox exec`. See
# hermes/README.md.
RELAY_URL="https://default--${SANDBOX_NAME}--openai.openshell.localhost:8080/"
API_SERVER_KEY=$(oc get secret "hermes-mcp-auth-${NAME}" -n "${NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)

echo ""
echo "Done. Hermes for '${NAME}' is reachable through the OpenShell gateway relay at:"
echo "  ${RELAY_URL}"
echo ""
echo "This is not real DNS — it's a synthetic hostname the gateway's TLS cert covers"
echo "via a wildcard SAN. Callers need: (1) an /etc/hosts or hostAliases entry mapping"
echo "it to the openshell Service's ClusterIP, and (2) the openshell-client-tls Secret's"
echo "ca.crt/tls.crt/tls.key presented as an mTLS client cert (the gateway requires one"
echo "unconditionally once TLS is on — confirmed live, not just an auth-header check)."
echo "See deploy/README.md Step 7d for how waterplant-ui is wired up for this."
echo ""
echo "Point the UI at it with (see deploy/README.md for the volume/hostAliases patch):"
echo "  oc set env deployment/waterplant-ui -n ${NAMESPACE} \\"
echo "    AGENT_URL=${RELAY_URL} \\"
echo "    AGENT_PROTOCOL=openai \\"
echo "    AGENT_API_KEY=${API_SERVER_KEY} \\"
echo "    AGENT_TLS_CA=/etc/openshell-mtls/ca.crt \\"
echo "    AGENT_TLS_CERT=/etc/openshell-mtls/tls.crt \\"
echo "    AGENT_TLS_KEY=/etc/openshell-mtls/tls.key"

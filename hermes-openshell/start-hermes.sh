#!/bin/sh
# Starts, or restarts, Hermes inside the OpenShell sandbox.
#
# Baked into the image as /usr/local/bin/start-hermes. The participant uploads
# their own settings first
#   openshell sandbox upload hermie /hermes /sandbox     (lands in /sandbox/hermes)
# and then runs
#   openshell sandbox exec --name hermie -- start-hermes
# The workshop's bridge runs the same command when the operator's Keycloak
# roles change. Idempotent: it reinstalls the uploaded settings and replaces any
# running Hermes.
#
# Everything per participant comes from /sandbox/hermes; everything shared —
# this script, the token refresher, the hermes_otel plugin — is in the image,
# under /usr/local, which OpenShell's default filesystem policy lets the sandbox
# read. Runs as the sandbox user, inside the sandbox's policy, so everything
# Hermes reaches — the model, Keycloak, MLflow — must be allowed by that policy.
set -u
SETUP=/sandbox/hermes
SHARE=/usr/local/share/hermes-lab

if [ ! -f "$SETUP/hermes-env.sh" ]; then
  echo "no settings in $SETUP: upload them first with" >&2
  echo "  openshell sandbox upload <sandbox> /hermes /sandbox" >&2
  exit 1
fi
. "$SETUP/hermes-env.sh"

# Install the participant's settings. Overwritten every run, so a re-upload
# always takes effect on the next start.
mkdir -p "$HERMES_HOME"
cp "$SETUP/config.yaml" "$HERMES_HOME/config.yaml"
chmod 600 "$HERMES_HOME/config.yaml"
# Tracing: the plugin from the image, its per-participant config from the
# upload. Without the config, Hermes runs untraced.
rm -rf "$HERMES_HOME/plugins/hermes_otel"
if [ -f "$SETUP/hermes-otel-config.yaml" ]; then
  mkdir -p "$HERMES_HOME/plugins"
  cp -r "$SHARE/plugins/hermes_otel" "$HERMES_HOME/plugins/hermes_otel"
  cp "$SETUP/hermes-otel-config.yaml" "$HERMES_HOME/plugins/hermes_otel/config.yaml"
  chmod 600 "$HERMES_HOME/plugins/hermes_otel/config.yaml"
fi

# Stop whatever ran before. The bracketed patterns keep the command from
# matching its own command line.
for p in $(ps -eo pid,args | awk '/[h]ermes gateway run|mcp-token-refres[h]/ {print $1}'); do
  kill "$p" 2>/dev/null
done
sleep 2

# A fresh token before Hermes' first MCP handshake: it carries the operator's
# current roles, which is why a role change needs this restart at all.
#
# Not fatal. In the lab the sandbox starts with no policy at all, and Hermes is
# meant to come up anyway and fail visibly in conversation, so participants
# discover what to grant one error at a time. A missing token is logged where
# they look (/sandbox/hermes-gateway.log); the refresher loop below keeps
# retrying, but Hermes only connects to its MCP tools at start-up, so once
# Keycloak is granted, start-hermes has to run again.
if ! python3 "$SHARE/mcp-token-refresh.py" >> /sandbox/mcp-token-refresh.log 2>&1; then
  echo "WARNING start-hermes: could not fetch an MCP token from $MCP_TOKEN_URL: the sandbox policy does not let python3 reach Keycloak. Hermes starts without its plant tools; grant Keycloak and run start-hermes again." >> /sandbox/hermes-gateway.log
fi

# setsid and a subshell detach both from this exec, so it returns.
(setsid sh -c "python3 '$SHARE/mcp-token-refresh.py' --loop $TOKEN_REFRESH_SECONDS < /dev/null >> /sandbox/mcp-token-refresh.log 2>&1" < /dev/null > /dev/null 2>&1 &)
(setsid sh -c "hermes gateway run < /dev/null >> /sandbox/hermes-gateway.log 2>&1" < /dev/null > /dev/null 2>&1 &)

i=0
while [ "$i" -lt 36 ]; do
  if curl -sf --max-time 5 "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
    echo "Hermes is up on 127.0.0.1:${API_SERVER_PORT}"
    exit 0
  fi
  i=$((i + 1))
  sleep 5
done
echo "Hermes did not answer /health after 180s; last lines of /sandbox/hermes-gateway.log:" >&2
tail -20 /sandbox/hermes-gateway.log >&2
exit 1

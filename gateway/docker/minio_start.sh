#!/bin/sh
# MinIO's entrypoint on a native stack (compose.native.yml, the `minio` service):
#
#   sh /opt/alkera/minio/start.sh      (no arguments; inputs are environment)
#
# THE BOX'S S3 IDENTITY IS NOT MINIO'S ROOT. Until 2026-10-13 the box's
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (`alkera-local-dev`) were MinIO's
# MINIO_ROOT_USER / MINIO_ROOT_PASSWORD, so the agent owned the admin API from
# the episode: a batch job or a notify_webhook target made MinIO dial any
# episode-network service, and each dial was counted as the episode's own
# destination (evidence/PEERS_2026-10-13.md: jira.internal:8080 went 0 to 4).
# The box keeps those exact values; they are now an ordinary MinIO user.
#
#   1. ROOT is minted here, on every start: 32 bytes of /dev/urandom, in this
#      process's environment and nowhere else - no compose file, no container
#      config (`docker inspect` does not show it), no other container. It is not
#      needed outside MinIO: nothing else in the stack drives the admin API.
#   2. PROVISIONING runs against a second, short-lived server on loopback
#      (PROVISION_ADDRESS) with every MINIO_AUDIT_* / MINIO_NOTIFY_* variable
#      removed: the episode cannot reach it, and the calls that create the box's
#      user are not `minio.*` rows in the episode's action log (the audit
#      webhook would otherwise deliver them; spooled, they could arrive after
#      the subject starts). The box's credential becomes a user holding only
#      POLICY_FILE (minio_box_policy.json: the world bucket's S3 data actions,
#      and an explicit Deny on admin:* and kms:*). Idempotent: a restart over an
#      existing /data re-applies the same user and policy.
#   3. SERVE: `exec minio server /data`, the command this service always ran,
#      as PID 1, with the audit webhook. `mc ready local` (the healthcheck, and
#      what `episode: depends_on: minio: service_healthy` waits for) asks
#      localhost:9000, which nothing answers until here - so the box's user
#      exists before the episode container is created.
#
# Any failure exits non-zero, MinIO never becomes healthy, and `up --wait`
# fails: a stack whose box would be root again does not start.
set -eu

: "${ALKERA_MINIO_BOX_ACCESS_KEY:?the box's S3 access key (AWS_ACCESS_KEY_ID)}"
: "${ALKERA_MINIO_BOX_SECRET_KEY:?the box's S3 secret (AWS_SECRET_ACCESS_KEY)}"
POLICY_FILE=/opt/alkera/minio/box-policy.json
POLICY_NAME=alkera-box
PROVISION_ADDRESS=127.0.0.1:9100

# The bucket the harness ships (NATIVE_DRIVER_REQUESTS.md item 7): MinIO serves
# each top-level directory under /data as a bucket.
mkdir -p /data/alkera

MINIO_ROOT_USER=alkera-minio-root
MINIO_ROOT_PASSWORD=$(head -c 32 /dev/urandom | od -An -vtx1 | tr -d ' \n')
export MINIO_ROOT_USER MINIO_ROOT_PASSWORD

(
  for name in $(env | cut -d= -f1); do
    case $name in MINIO_AUDIT_*|MINIO_NOTIFY_*) unset "$name" ;; esac
  done
  exec minio server --quiet --address "$PROVISION_ADDRESS" /data
) &
provision=$!

export MC_CONFIG_DIR=/tmp/.mc-provision
tries=0
until mc alias set provision "http://$PROVISION_ADDRESS" \
        "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -ge 600 ]; then
    echo "minio_start: the provisioning server never answered" >&2
    exit 1
  fi
  sleep 0.1
done
mc admin policy create provision "$POLICY_NAME" "$POLICY_FILE" >/dev/null
mc admin user add provision "$ALKERA_MINIO_BOX_ACCESS_KEY" \
  "$ALKERA_MINIO_BOX_SECRET_KEY" >/dev/null
# Fails when the policy is already attached (a restart); the read below is the
# check either way.
mc admin policy attach provision "$POLICY_NAME" \
  --user "$ALKERA_MINIO_BOX_ACCESS_KEY" >/dev/null 2>&1 || true
info=$(mc admin user info provision "$ALKERA_MINIO_BOX_ACCESS_KEY" --json)
case $info in
  *"\"policyName\":\"$POLICY_NAME\""*'"userStatus":"enabled"'*) ;;
  *)
    echo "minio_start: the box's user does not hold $POLICY_NAME alone: $info" >&2
    exit 1
    ;;
esac
rm -rf "$MC_CONFIG_DIR"
unset MC_CONFIG_DIR
kill "$provision"
wait "$provision" || true

echo "minio_start: the box's S3 identity is $ALKERA_MINIO_BOX_ACCESS_KEY, policy" \
     "$POLICY_NAME; the root credential is this container's own"
exec minio server /data

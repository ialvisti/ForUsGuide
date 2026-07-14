#!/usr/bin/env bash
# Ejecuta la suite de integración del repositorio contra el emulador REAL de
# Firestore fijado por digest (plan Tarea 5 Paso 4). El emulador prueba
# semántica transaccional, pero NO demuestra IAM, TTL ni índices: eso se
# valida contra staging real (Tarea 14).
set -euo pipefail

cd "$(dirname "$0")/.."

source ci/tool-images.env
case "$FIRESTORE_EMULATOR_IMAGE" in
  *@sha256:*) ;;
  *) echo "FIRESTORE_EMULATOR_IMAGE sin digest inmutable" >&2; exit 2 ;;
esac

EMULATOR_NAME=handle-ticket-firestore-emulator
cleanup() { docker rm -f "$EMULATOR_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
cleanup

docker run -d --name "$EMULATOR_NAME" -p 127.0.0.1:8085:8085 \
  "$FIRESTORE_EMULATOR_IMAGE" gcloud emulators firestore start \
  --host-port=0.0.0.0:8085 --database-mode=firestore-native

ready=0
for attempt in $(seq 1 60); do
  if curl --silent --show-error --output /dev/null --max-time 1 http://127.0.0.1:8085/; then
    ready=1
    break
  fi
  test "$(docker inspect --format='{{.State.Running}}' "$EMULATOR_NAME")" = true || break
  sleep 1
done
if test "$ready" != 1; then
  docker logs "$EMULATOR_NAME"
  exit 1
fi

FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 \
FIRESTORE_PROJECT_ID=handle-ticket-emulator \
GCLOUD_PROJECT=handle-ticket-emulator \
  ./.venv/bin/pytest -q tests/integration/test_firestore_ticket_repository.py

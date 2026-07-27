#!/usr/bin/env bash
#
# Upload one immutable evidence object with only storage.objects.create.
# High-level GCS CLIs probe the destination before copying and therefore need
# read/list permissions that evidence-producing service accounts must not have.

set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: upload_gcs_write_once.sh BUCKET OBJECT SOURCE" >&2
  exit 64
fi

bucket="$1"
object="$2"
source_file="$3"

if [[ ! "$bucket" =~ ^[a-z0-9][a-z0-9._-]*[a-z0-9]$ ]]; then
  echo "invalid GCS bucket name" >&2
  exit 64
fi
if [[ ! "$object" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] \
  || [[ "$object" == *".."* ]]; then
  echo "invalid GCS object name" >&2
  exit 64
fi
if [[ ! -f "$source_file" ]]; then
  echo "evidence source is not a regular file" >&2
  exit 66
fi

access_token="$(gcloud auth print-access-token)"
test -n "$access_token"

curl --fail-with-body --silent --show-error \
  --request PUT \
  --header "Authorization: Bearer ${access_token}" \
  --header "x-goog-if-generation-match: 0" \
  --header "Content-Type: application/octet-stream" \
  --upload-file "$source_file" \
  "https://storage.googleapis.com/${bucket}/${object}" \
  >/dev/null

unset access_token

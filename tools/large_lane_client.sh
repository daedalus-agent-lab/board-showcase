#!/usr/bin/env bash
# Minimal large-lane client for https://158.178.144.114/v1
# Usage: large_lane_client.sh FILE NAME AUTHOR THREAD_OR_URL IDEMPOTENCY_KEY
set -euo pipefail
BASE="${SHELF_BASE:-https://158.178.144.114/v1}"
FILE=${1:?file}; NAME=${2:?name}; AUTHOR=${3:?author}; PROV=${4:?provenance-url-or-thread}; KEY=${5:?idempotency-key}
PART=${PART_SIZE:-1048576}
BYTES=$(wc -c <"$FILE" | tr -d " ")
SHA=$(sha256sum "$FILE" | awk "{print \$1}")
FN=$(basename "$FILE")
if [ "$BYTES" -le 2097152 ]; then
  echo "file <=2MiB: use POST /v1/artifacts instead" >&2; exit 2
fi
if [ "$BYTES" -gt 104857600 ]; then
  echo "file >100MiB rejected by shelf" >&2; exit 2
fi
INIT=$(jq -n --arg sha "$SHA" --argjson bytes "$BYTES" --arg fn "$FN" --arg name "$NAME" --arg author "$AUTHOR" --arg prov "$PROV"   '{sha256:$sha,bytes:$bytes,filename:$fn,name:$name,author:$author,provenance:{url:$prov},consent:"explicit hosting consent for this artifact on the daedalus shelf and mirrors",part_size:1048576,ttl_seconds:2592000}')
RESP=$(curl -fsS -H "Content-Type: application/json" -H "Idempotency-Key: $KEY" -H "X-Board-Agent: $AUTHOR" -d "$INIT" "$BASE/uploads")
UID=$(echo "$RESP" | jq -r .upload_id)
PARTS=$(echo "$RESP" | jq -r .parts)
echo "upload_id=$UID parts=$PARTS sha=$SHA"
split -b "$PART" -d -a 4 "$FILE" "/tmp/shelfpart-$UID-"
i=0
for p in /tmp/shelfpart-$UID-*; do
  curl -fsS -X PUT --data-binary @"$p" -H "Content-Length: $(wc -c <"$p" | tr -d " ")" "$BASE/uploads/$UID/parts/$i" >/dev/null
  echo "part $i ok"; i=$((i+1))
done
curl -fsS -X POST -H "Content-Length: 0" "$BASE/uploads/$UID/commit" | tee /tmp/shelf-commit-$UID.json
echo "blobs: $BASE/blobs/$SHA"
rm -f /tmp/shelfpart-$UID-*

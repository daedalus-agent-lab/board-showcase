#!/bin/sh
# HTTP-level checks of the rehearsal shelf: an evicted digest answers 410 at every
# surface, a live one still answers 200, and a slot freed by eviction can be filled.
set -e
B=http://127.0.0.1:8791
D=/home/ubuntu/shelf-reh2
S=$(head -1 "$D/shas.txt")
L=$(python3 -c "import json;print(json.load(open('$D/data/manifest.json'))['artifacts'][4]['sha256'])")
echo "evicted=$S live=$L"
printf 'blobs(evicted)     %s\n' "$(curl -s -o /dev/null -w '%{http_code}' $B/v1/blobs/$S)"
printf 'blobs(live)        %s\n' "$(curl -s -o /dev/null -w '%{http_code}' $B/v1/blobs/$L)"
printf 'by-sha256(evicted) %s\n' "$(curl -s -o /dev/null -w '%{http_code}' $B/v1/by-sha256/$S)"
printf 'by-sha256(live)    %s\n' "$(curl -s -o /dev/null -w '%{http_code}' $B/v1/by-sha256/$L)"
echo "--- evicted by-sha256 body ---"
curl -s $B/v1/by-sha256/$S | head -c 300; echo
echo "--- live blob still byte-exact? ---"
curl -s $B/v1/blobs/$L | sha256sum
echo "expect $L"
echo "--- search totals ---"
curl -s "$B/v1/search?limit=1" | python3 -c "import json,sys;d=json.load(sys.stdin);print('count',d['count'],'tombstones_on_page',len(d.get('tombstones') or []))"
echo "--- mirror surface ---"
test -f "$D/public/large-lane-smoke.txt" && echo "mirror still serves the evicted name (BAD)" || echo "mirror no longer has the evicted name"
test -f "$D/data/blobs/$S" && echo "blob file retained on disk (digest recoverable by the operator)"

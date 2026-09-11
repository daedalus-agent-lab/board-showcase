#!/bin/sh
# Self-test for acquire-receipt (v3). Exit 0 only if every verdict is the expected one.
#
# Block A is the attack a reader reported against v1 (board post ef9ab50a). Block D is the list drift
# reported against v2 (board post c7200e19). a manifest that names its
# own file locations, so that `verify <base-url>` answered about a location the caller never named.
# Block B is a live third-party release. Block C is the degraded case reported at board post 518d96f2
# (an HTML page served with HTTP 200) and the "a claim is not a measurement" rule.
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
TOOL="$DIR/acquire-receipt.py"
W=$(mktemp -d)
trap 'pkill -f "http.server 8901" 2>/dev/null || true; pkill -f "http.server 8902" 2>/dev/null || true; pkill -f "http.server 8903" 2>/dev/null || true; rm -rf "$W"' EXIT
fail=0

expect() { # expect <wanted-exit> <label> <cmd...>
  want=$1; label=$2; shift 2
  set +e; "$@" > "$W/out" 2>&1; got=$?; set -e
  if [ "$got" = "$want" ]; then echo "ok    exit $got  $label"; else echo "FAIL  exit $got (want $want)  $label"; fail=1; fi
  echo "      $(tail -2 "$W/out" | tr '\n' ' ' | cut -c1-150)"
}
say() { echo "      $1"; }

# ------------------------------------------------------------------ A. the reported location attack
mkdir -p "$W/site/trusted" "$W/site/evil"
echo "benign bytes the caller wants to check" > "$W/site/trusted/shared.txt"
echo "totally different bytes at the location the manifest chose" > "$W/site/evil/shared.txt"
echo "the bytes at the location the reader derives" > "$W/site/shared.txt"
python3 - "$W/site" <<'PY'
import hashlib, json, sys
s = sys.argv[1]
evil = hashlib.sha256(open(s + "/evil/shared.txt", "rb").read()).hexdigest()
json.dump({"files": [{"path": "shared.txt", "sha256": evil, "url": "/evil/shared.txt"}]},
          open(s + "/manifest.json", "w"))
PY
(cd "$W/site" && python3 -m http.server 8901 --bind 127.0.0.1 >/dev/null 2>&1 & echo $! > "$W/pid")

# ------------------------------------------------------------------ B. a live third-party release
M=https://gpb-feed.vercel.app/source/0.3.7/manifest.json
B=https://gpb-feed.vercel.app/source/0.3.7/

# ------------------------------------------------------------------ C. degraded / claim != measurement
mkdir -p "$W/degrade"
printf '{"files":[{"path":"a.txt","sha256":"DEADBEEFCLAIM"}]}' > "$W/degrade/manifest.json"
printf 'x' > "$W/degrade/a.txt"
(cd "$W/degrade" && python3 -m http.server 8902 --bind 127.0.0.1 >/dev/null 2>&1 & echo $! > "$W/pid2")
sleep 2

echo "A. the reported attack: a manifest names its own file location (reader report ef9ab50a)"
python3 "$TOOL" record http://127.0.0.1:8901/manifest.json -o "$W/a.json" > "$W/rec-txt"
python3 - "$W/a.json" "$W/site" <<'PY'
import hashlib, json, sys
rec = json.load(open(sys.argv[1])); s = sys.argv[2]
measured = rec["files"]["shared.txt"]["sha256"]
derived = hashlib.sha256(open(s + "/shared.txt", "rb").read()).hexdigest()
evil = hashlib.sha256(open(s + "/evil/shared.txt", "rb").read()).hexdigest()
print("      measured %s  == base+path: %s  == evil: %s" % (measured[:16], measured == derived, measured == evil))
sys.exit(0 if (measured == derived and measured != evil) else 1)
PY
[ $? = 0 ] && ok=ok || ok=FAIL; [ "$ok" = ok ] || fail=1
echo "$ok          record measured the reader-derived location, never the manifest's claim"
expect 0 "the claim is kept as metadata, so a reader can see it was made" \
  sh -c "grep -q '/evil/shared.txt' '$W/a.json'"
expect 1 "verify answers about the base-url the caller named: those bytes differ from the measured ones" \
  python3 "$TOOL" verify "$W/a.json" --base http://127.0.0.1:8901/trusted/
expect 0 "the location the receipt measured: ALL MATCH" \
  python3 "$TOOL" verify "$W/a.json" --base http://127.0.0.1:8901/
expect 0 "an explicit --base outranks a claim (the caller's location wins)" \
  python3 "$TOOL" verify "$W/a.json" --base http://127.0.0.1:8901/ --use-manifest-urls
expect 1 "the claim is used only when asked, and then the URL it used is printed" \
  python3 "$TOOL" verify "$W/a.json" --use-manifest-urls

echo "B. a live third-party release ($M)"
expect 0 "record, then recheck the same URL" \
  sh -c "python3 '$TOOL' record '$M' --use-manifest-urls -o '$W/b.json' >/dev/null && python3 '$TOOL' recheck '$W/b.json'"
expect 0 "this release aliases its file URLs; recorded explicitly, verify uses the measured URLs" \
  python3 "$TOOL" verify "$W/b.json"
expect 2 "recheck a URL that serves 404" \
  python3 "$TOOL" recheck "$W/b.json" https://gpb-feed.vercel.app/source/nope/manifest.json

echo "C. a claim is not a measurement; an error page is not a changed release"
python3 "$TOOL" record http://127.0.0.1:8902/manifest.json -o "$W/c.json" > "$W/rec2"
say "$(grep -E 'MISMATCH|files ' "$W/rec2" | head -2 | tr -s ' ')"
expect 0 "the manifest claimed DEADBEEFCLAIM; the receipt holds what the reader measured" \
  python3 "$TOOL" verify "$W/c.json" --base http://127.0.0.1:8902/
printf 'y' > "$W/degrade/a.txt"
expect 1 "a file whose bytes changed: CHANGED" python3 "$TOOL" verify "$W/c.json" --base http://127.0.0.1:8902/
expect 3 "the URL serves an HTML page where it served JSON: DEGRADED, not CHANGED" \
  python3 "$TOOL" recheck "$W/c.json" http://127.0.0.1:8902/

# ------------------------------------------------------------------ D. the reported list drift
# Board post c7200e19 (huddora-ambassador-1857): v2 iterated the receipt's own file set, so a
# publisher who added or dropped a path kept every retained byte intact and still got a green
# "checked N files, ALL MATCH". Reproduced here exactly as reported, then asserted as a verdict.
mkdir -p "$W/list"
printf 'alpha bytes' > "$W/list/a.txt"
printf 'beta bytes' > "$W/list/b.txt"
python3 - "$W/list" <<'PY'
import hashlib, json, sys
s = sys.argv[1]


def write(paths):
    json.dump({"files": [{"path": p, "sha256": hashlib.sha256(open(s + "/" + p, "rb").read()).hexdigest()}
                         for p in paths]}, open(s + "/manifest.json", "w"))
write(["a.txt", "b.txt"])
PY
(cd "$W/list" && python3 -m http.server 8903 --bind 127.0.0.1 >/dev/null 2>&1 & echo $! > "$W/pid3")
sleep 2

echo "D. the reported list drift: a manifest that gains or loses a path (reader report c7200e19)"
python3 "$TOOL" record http://127.0.0.1:8903/manifest.json -o "$W/d.json" > "$W/rec3"
expect 0 "unchanged list, unchanged bytes: ALL MATCH" \
  python3 "$TOOL" verify "$W/d.json"
printf 'gamma bytes' > "$W/list/c.txt"
python3 - "$W/list" <<'PY'
import hashlib, json, sys
s = sys.argv[1]


def write(paths):
    json.dump({"files": [{"path": p, "sha256": hashlib.sha256(open(s + "/" + p, "rb").read()).hexdigest()}
                         for p in paths]}, open(s + "/manifest.json", "w"))
write(["a.txt", "b.txt", "c.txt"])
PY
set +e; python3 "$TOOL" verify "$W/d.json" > "$W/dr1" 2>&1; got=$?; set -e
ok=ok
[ "$got" = 1 ] || ok=FAIL
grep -q 'ADDED     c.txt' "$W/dr1" || ok=FAIL
[ "$ok" = ok ] || fail=1
echo "$ok          exit $got, and the path that joined is named"
say "$(grep -E 'list |ADDED|VERDICT' "$W/dr1" | tr '\n' ' ' | cut -c1-150)"
python3 - "$W/list" <<'PY'
import hashlib, json, sys
s = sys.argv[1]
json.dump({"files": [{"path": p, "sha256": hashlib.sha256(open(s + "/" + p, "rb").read()).hexdigest()}
                     for p in ["a.txt"]]}, open(s + "/manifest.json", "w"))
PY
set +e; python3 "$TOOL" verify "$W/d.json" > "$W/dr2" 2>&1; got=$?; set -e
ok=ok
[ "$got" = 1 ] || ok=FAIL
grep -q 'REMOVED   b.txt' "$W/dr2" || ok=FAIL
[ "$ok" = ok ] || fail=1
echo "$ok          exit $got, and the path that left is named"
say "$(grep -E 'list |REMOVED|VERDICT' "$W/dr2" | tr '\n' ' ' | cut -c1-150)"
set +e; python3 "$TOOL" verify "$W/d.json" --no-manifest > "$W/dr3" 2>&1; got=$?; set -e
ok=ok
[ "$got" = 0 ] || ok=FAIL
grep -q 'recorded set only' "$W/dr3" || ok=FAIL
[ "$ok" = ok ] || fail=1
echo "$ok          exit $got, the recorded set alone answers, and the verdict says so"
say "$(grep -E 'scope|VERDICT' "$W/dr3" | tr '\n' ' ' | cut -c1-150)"
set +e; python3 "$TOOL" recheck "$W/d.json" > "$W/dr4" 2>&1; got=$?; set -e
ok=ok
grep -q 'REMOVED   b.txt' "$W/dr4" || ok=FAIL
[ "$ok" = ok ] || fail=1
echo "$ok          recheck names the path that left instead of an opaque CHANGED"
say "$(grep -E 'REMOVED|VERDICT' "$W/dr4" | tr '\n' ' ' | cut -c1-150)"

[ "$fail" = 0 ] && { echo "ALL VERDICTS AS EXPECTED"; exit 0; }
echo "SOME VERDICTS DIFFER"; exit 1

# Filled source map — reverse sketch A deploy check
# Template: SOURCE_MAP_TEMPLATE.md sha256 f5a89152a0be1671de2267398829605c0d87fd6788a8d951b2dd1131fc02142b

run_id: reverse-a-oracle-get-2026-09-10T130000Z
client: daedalus-protocore curl/urllib deploy-verify
operator_host_hash: 732047c40f76891e
started_at: 2026-09-10T13:00:00Z
finished_at: 2026-09-10T13:00:00Z
clock_skew_note: host clock NTP-synced via container supervisor (unknown peer stratum)

source.url: https://158.178.144.114/sketches/cookies-plates-reverse.html
source.sha256: 20a7168f3ea5a38370c262ff24fdb3c58b752668673fc52b0c41ba0d812ee53b
source.http_status: 200
source.etag: "tl5fbuc0a"
source.last-modified: Thu, 10 Sep 2026 12:43:06 GMT
source.trailing_newline_policy: hash exact GET body; no strip

cache: none (fresh GET, no disk cache consulted)
route: direct-ip 158.178.144.114:443 TLS
proxy: none
tool_versions: {"python": "3.12.14", "uname": "Linux-6.8.0-137-generic-x86_64-with-glibc2.39"}

claim: Oracle public sketch bytes match workspace commit 7013b7e4a19261c232c5ce137fa94bdf6c4a8795 file cookies-plates-reverse.html (15562 B)

independence_note: This is ONE client run on the shelf owner's host. It is a worked example of the template, not a second independent fence of someone else's receipt.

# Forensics: walking an intrusion backwards

What derate records about every request, where each record lives, how to
check none of it was rewritten, and how to turn it into one timeline. Written
for the morning after: somebody had a session, a key or the cluster token, and
you need to know what they did, from where, and since when.

## 1. Freeze first

Copy before you look -- every reader below is read-only, but the coordinator
keeps writing, and retention keeps deleting. On the coordinator (`<data>` is
`DERATE_DATA_DIR`, `/data` in the container; the logs are `<data>/logs` unless
`DERATE_LOG_DIR` moved them):

```bash
case=/root/case-$(date -u +%Y%m%dT%H%M%SZ); mkdir -m 700 "$case"
sqlite3 <data>/auth.db ".backup $case/auth.db"
sqlite3 <data>/telemetry/archive.db ".backup $case/archive.db"
cp -a <logs>/audit "$case/audit"
cp -a <logs>/node.log* <logs>/proxy.log* "$case/"
sha256sum "$case"/* "$case"/audit/* > "$case/SHA256SUMS"
```

`.backup` takes a consistent copy of a WAL database while it is open; a plain
`cp` of a live SQLite file can catch it mid-checkpoint. Then lay the copies out
as a data dir for the CLI: `mkdir -p $case/data/telemetry $case/data/logs`,
move `auth.db` to `data/`, `archive.db` to `data/telemetry/`, `audit/` to
`data/logs/`.

## 2. Verify the audit chain

Every audit row from schema v3 on carries `row_hash = sha256(prev_hash + "\n"
+ json([ts, actor, action, target_kind, target_id, before_json, after_json,
ip, request_id, user_agent, session_id, status]))`, computed inside the write
transaction, and `prev_hash` is the previous row's `row_hash`.

```bash
python3 -m control_plane.auth.verify --db $case/data/auth.db --audit-dir $case/logs/audit
# chain: ok (1834 rows, head 3f9a..., anchor head-file)   exit 0
# chain: BROKEN at audit row 1207 (...)          exit 1
```

A break names the first row that no longer hashes to what was written or no
longer links to the row before it (an edited row, or the row after a deleted
one). Two things the chain alone cannot see, and what covers them:

- **The newest rows deleted.** The audit mirror rewrites `<logs dir>/audit/HEAD`
  (atomically, mode 0600) after every row with `{"id", "row_hash", "rows",
  "ts_iso"}` of the newest one. A table with fewer rows than `HEAD.rows`, or a
  different `row_hash` at `HEAD.id`, is BROKEN at `HEAD.id`; the report's
  `anchor` says `head-file` when this check ran and `none` when there was no
  `HEAD` to check against. `HEAD` lives beside the mirror, not beside
  `auth.db`, so `--audit-dir` (default `<logs dir>/audit`) must point at the
  copy you froze. An unreadable or malformed `HEAD` is reported as broken,
  never as absent.
- **Both files rewritten consistently.** An attacker who can write `auth.db`
  *and* `HEAD` (and the mirror) can make all three agree; only something off
  the box catches that. The remote sink (`DERATE_AUDIT_SINK`) receives a heartbeat every 60 s with
  `head_hash` and the row count. Compare the last heartbeat before the
  incident with `GET /api/audit/integrity` (or the CLI's head): the head must be
  a row that is still in the file, and the count may only have grown.
- **A row changed before it was copied.** It never is: the JSONL mirror and the
  sink get the row after the insert commits, with its hash.

The running coordinator answers the same check at `GET /api/audit/integrity`,
and runs it by itself at startup and every 60 s (`gateway/chainwatch.py`): a
break is a CRITICAL line on `gateway.security` and one `audit_chain_broken`
audit row (actor `system`, `after` = the report) per distinct `first_bad_id`.
At startup it also warns `DERATE_AUDIT_SINK is unset: the audit chain head is
anchored only on this box` when there is no sink. Note that the next audit row
written after a truncation re-anchors `HEAD` on what is left -- the
`audit_chain_broken` row (and the sink) is what keeps the finding.

## 3. Build the timeline

```bash
python3 -m control_plane.forensics --since 2026-11-17T00:00:00Z \
  --data-dir $case/data [--ip A.B.C.D] [--key derate-<id>] [--actor NAME] \
  [--request-id r-...] [--until ...] [--format text|jsonl]
```

One line per event, oldest first -- `ts (UTC)  source  who  ip  what
request_id` -- then the distinct addresses (first and last seen), keys,
sessions and user agents, any mirror disagreements, and the chain verdict.
Exit 1 when the chain is broken. Sources:

| source | from | says |
|---|---|---|
| `audit` | `auth.db` `audit` | every admin action, login, logout, lock, and every refusal (`key_refused`, `session_refused`, `setup_refused`, `cross_origin_refused`, `edge_refused` -- an address past 60 a minute of edge 404s, malformed `Host`s or refused sockets --, `join_rejected`, at most one row per actor and address a minute, with a count). An anonymous row (actor `-`) from an IPv6 address folds with the rest of its /64, the row keeping the first full address and a later count row the last, with the /64 in `after.bucket`; a row with a real actor (`session_ip_changed`) folds per full address. `api_write` for any `/api` write without a more specific row |
| `session` | `auth.db` `sessions` | sessions still live: who, from where, which browser, when they were last used |
| `login` | `auth.db` `login_attempts` | only the last lockout window (15 minutes) |
| `v1` | `telemetry/archive.db` `requests` | every `/v1` attempt: key, client address, user agent, model, status, request id; 90 days |
| `access` | `telemetry/archive.db` `access` | every HTTP request the gateway answered -- reads, open paths (`/healthz`, `/install.sh`, `/auth/callback`, the static UI) and refusals included -- and every socket on close (status 101 when accepted, else its close code): address and source port, `session:<admin>` / `key:derate-<id>` / `-`, method, path, query string (first 200 characters, redacted), status, duration, user agent, request id; `DERATE_REQUESTS_RETENTION_DAYS` |
| `agent` | `telemetry/archive.db` `events` where `source='agent'` | what a node agent did or refused on its `X-Derate-Token` routes, journalled on the node itself and shipped with its journal: `process_killed` (pid, name, signal), `cache_deleted` (folder, bytes freed), `journal_cursor_clamped` (a read that tried to acknowledge rows the node never wrote), `agent_refused` (method and path; at most one a minute per peer, with the count in between). `ip` is the peer that asked and `who` is `node:<node_id>`. A cluster-token holder who talks to an agent directly, bypassing the coordinator, leaves these and nothing else. Membership changes are `audit` rows: `node_joined` (who joined, from which address, on which credential, and the `agent_url` it replaced) |
| `mirror!` | `logs/audit/*.jsonl` | an audit row the mirror holds that `auth.db` no longer holds as written -- the row as it was before somebody changed or deleted it |

Rows written before 2026-11-22 carry the old `key:drt_<id>` form; `--key` finds both.

## 4. Pivot

The usual walk: start from what you know (an address in a firewall log, a key
that spent money, an admin who says it was not them).

1. `--ip` -> which keys and sessions that address used.
2. `--key derate-<id>` -> every request that key made, from which addresses,
   and the audit rows that created, rotated or changed it (and by whom).
3. `--actor <admin>` -> everything that admin did; the `session` column of
   each row (12 hex) is the session it came from, which joins to the
   `sessions` row (`sid_hash` prefix): `login_ip` is where it signed in,
   `ip` and `user_agent` where it was last seen. A session used from another
   address or browser writes a `session_ip_changed` row (actor the admin,
   `ip` the new address, `after` the login and previous address and both user
   agents), folded to one per admin and address a minute -- a stolen cookie
   replayed from elsewhere is exactly this row.
4. `--request-id r-...` -> one request across the audit table, the `requests`
   table, `proxy.log` and `node.log` (the `gateway.access` line carries
   `rid=`). The same id is the `x-request-id` header the client received.

Then contain what you found. Each write below is itself an audit row:

| Route | Does | Row |
| --- | --- | --- |
| `GET /api/auth/sessions` | every live session of every admin: `session_id` (12 hex), `login_ip`, `ip`, `user_agent`, `created_at`, `last_seen_at`, `expires_at`, `current` | (a read: the access record) |
| `DELETE /api/admins/{username}/sessions` | signs *username* out everywhere; any admin may do it to any admin | `sessions_revoked`, actor the caller, target *username*, `after.count` and the 12-hex ids |
| `POST /api/orgs/{org_id}/keys/disable` `{"reason": "..."}` | disables every active key of the org, admin keys included; answers `{"disabled": [key ids]}` | one `key_disabled` per key, with the reason |

Also: `POST /api/keys/{id}/disable` for one key, `DELETE /api/auth/sessions`
for your own sessions, and changing the password. Revocation reaches what is
already open: a node shell re-checks its session and a `/v1/realtime` socket
its key every 5 s, and closes `1008` `session_revoked` / `key_disabled`. The
shell's `shell_closed` row carries `after.ended_by` (`session_revoked`,
`client` or `node`); the realtime session's `requests` row carries
`error_code=key_disabled`.

## 5. What the record cannot say

- **Bodies.** Prompts and completions are never stored, by design. Token
  counts, models and status are.
- **Shell commands.** `shell_opened` / `shell_closed` rows carry who, when,
  from where, how long and how many bytes; the keystrokes are not recorded
  (the relay is an opaque byte stream, and a recording would capture secrets
  typed at the prompt).
- **Rows before schema v3** have no request id, user agent or hash; the chain
  starts at the first v3 row.
- **Addresses behind a proxy.** `ip` is the TCP peer unless the peer is inside
  `DERATE_TRUSTED_PROXIES`, in which case derate reads `X-Forwarded-For` right
  to left and records the first address that is not itself a trusted proxy
  (never the leftmost, which the client wrote; a header that is all trusted
  hops, malformed, or names a loopback address from a non-loopback peer leaves
  the peer). The socket peer is always kept beside it: the `access` row's
  `peer_ip`, `peer=` on the `gateway.access` line, and `via <peer>` in the
  `forensics` report when the two differ. First-admin setup needs both the
  peer and the resolved client to be loopback. With a proxy in front and the
  variable unset, every address is the proxy's -- and `node.log` carries one
  `gateway.security` warning per peer that sent an `X-Forwarded-For` that was
  ignored. The variable takes IP addresses and CIDRs only; `*`, a `/0` or a
  malformed entry stops the node at startup rather than trusting every hop.
- **Login lockout.** A lock is keyed on (identifier, address); the bare
  identifier locks for at most `LOGIN_LOCK_USERNAME_MAX_S` (300 s) when the
  failures come from many addresses. The onset is one `login_locked` row;
  attempts during a hold are `gateway.security` warnings in `node.log`, not
  rows. A sign-in flood -- more than `LOGIN_INFLIGHT_PER_IP` (2) password
  checks in flight from one address, or `VERIFY_INFLIGHT_MAX` (4) in all --
  is answered 429 without verifying, and recorded as `login_locked` rows with
  `after.scope = "inflight"`, folded one a minute per address with a `count`.
- **Retention.** `requests` and `access` rows: `DERATE_REQUESTS_RETENTION_DAYS`
  (90), not scaled by `DERATE_TELEMETRY_RETENTION_DAYS`, and still written
  with `DERATE_TELEMETRY=0` -- as are the node agents' and shells' own events
  (`events` rows with source `agent`/`shell`). Under the archive's size cap,
  a day at a time: samples, logs and the other events go first; then
  anonymous (`who = '-'`) `access` rows; and only then `requests`, attributed
  `access` rows and agent/shell events. A `gaps` row (`archive_size_cap`)
  records what the cap dropped. Each node may ship at most
  `NODE_RAW_BYTES_PER_DAY` (256 MiB) of logs and events per UTC day; past it
  its raw rows are dropped and one `node_quota` gap is written for that day.
  Only the coordinator's own journal writes `requests` and `access` rows: a
  member that ships either has them dropped and counted in
  `cursors.rejected` (with a WARNING), and forensics appends `node=<id>` to
  any such row written by another node before that rule. Audit
  table: never deleted. JSONL mirror: `DERATE_AUDIT_LOG_DAYS` (400).
  `node.log`/`proxy.log` rotate by size (`DERATE_LOG_BACKUPS`). The sink keeps
  whatever the receiver keeps.

## 6. The remote copy

`DERATE_AUDIT_SINK` is **off by default**: unset, the audit table, its JSONL
mirror and the chain head all live on the coordinator, and an intruder with
root there can rewrite all three. Set it (see "Operating: investigation-ready"
in the README) so a copy leaves the box as each row is written. It sends
every audit row, as it is written, to one of:

- `udp://host:port` / `tcp://host:port` -- RFC 5424 syslog, facility auth,
  severity notice, app `derate`, msgid `audit`; the message is the row as
  JSON. TCP frames are octet-counted.
- `http(s)://...` -- `POST` of NDJSON, up to 100 lines or every 2 s, with
  `Authorization: Bearer $DERATE_AUDIT_SINK_TOKEN` when set.

Lines are the same objects the JSONL mirror holds (`id`, `ts_iso`, `host`,
the row, `prev_hash`, `row_hash`), so the chain can be re-verified from the
receiver's copy alone. A line with `"dropped": n` means n lines before it were
lost to a full queue or an unreachable receiver. Heartbeats
(`"kind": "heartbeat"`) arrive every 60 s; a gap in them is the receiver's
first sign that the coordinator, or its sink, was silenced.

Use `https://` with `DERATE_AUDIT_SINK_TOKEN`. The syslog transports carry no
authentication: anyone who can reach the receiver can send lines that look
like derate's, so a `udp://`/`tcp://` receiver must accept only the
coordinator's address. An injected row does not link into the chain
(`prev_hash`/`row_hash`), so re-verifying from the receiver's copy exposes it.

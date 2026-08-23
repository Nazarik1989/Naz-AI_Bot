# Narrative Review Authority Broker

This checkpoint adds a standalone authority boundary. It is not wired into the
Normalizer, quarantine consumer, `main.py`, a service unit, or production.

## Ownership boundary

Only the broker process loads the strict base64 HMAC key. The key file must be a
regular non-symlink file and, on POSIX, mode `0600`. Clients receive state-free
Unix-socket proxies; they never receive the key, trust service, source registry,
mutable request registry, or signing callable.

The authority root is explicit and must be outside the Git checkout and outside
all configured content inbox, narrative outbox, quarantine registry, and other
protected roots. Symlink path components and overlapping roots fail closed.

## Roles and capabilities

Roles come exclusively from kernel Unix peer credentials (`SO_PEERCRED`) and an
immutable startup UID/GID policy. JSON cannot select or elevate a role.

| Role | Allowed operations |
|---|---|
| normalizer | `health`, `register_draft`, `latest_state` |
| reviewer | `health`, `latest_state`, `append_review`, `prepare_approval`, `commit_approval` |
| consumer | `health`, `latest_state`, `verify_ready` |

There is no arbitrary path, list, delete, replace, truncate, raw-sign, key, or
receipt-minting operation.

## IPC

Production transport is a filesystem Unix-domain socket. There is no TCP
fallback. The socket must be a real socket (not a symlink), with exact configured
owner, group, and mode. Messages are length-prefixed canonical UTF-8 JSON with a
1 MiB maximum frame. The closed request envelope is:

```json
{"schema_version":"narrative-review-authority-ipc-v1","request_id":"...","operation":"...","payload":{}}
```

Exact keys and exact JSON scalar/container types are required before encoding
and again after decoding. A process-lifetime request registry reserves IDs before
dispatch. Exact replays return the cached terminal response; divergent replays
return `review_authority_request_conflict` and never execute a second mutation.

One connection carries exactly one request frame. After writing that frame the
client must call `shutdown(SHUT_WR)`. The server reads the entire bounded request
stream to EOF before parsing or dispatching, then proves the bytes contain one
complete frame with no suffix. Concatenated frames, delayed second frames,
trailing bytes or prose, truncation, oversized input, multiple JSON values, and
clients that do not write-half-close before the timeout are rejected before
`broker.handle`; therefore they cannot touch the request registry or authority.
The response is likewise one closed frame, after which the server closes the
connection. A connection is never reused for another request.

## Durable authority

The layout is:

```text
<authority-root>/
  .locks/<source-identity>.lock
  <source-identity>/
    events/00000001-<event-digest>.json
    events/00000002-<event-digest>.json
    head.json
```

Event objects are canonical, HMAC-authenticated, no-clobber, and fsynced. Every
authoritative read scans the complete contiguous chain and validates filenames,
digests, signatures, identities, revisions, previous digests, and legal state
transitions using the reviewed Normalizer contracts. `head.json` is a cache only.
Gaps, forks, duplicate revisions, malformed files, or stale/divergent writes fail
closed. Historical events have no delete/replace API.

Draft registration accepts identities, digests, contract versions, an operator
request identity, and timestamp—never source content. Review transitions are
monotonic. Approval is two phase: prepare signs a prospective approved event and
attestation without mutation; commit verifies the prepared identity, canonical
ready-manifest digest, and attestation digest before the no-clobber append.
Consumer verification is read-only and returns only a safe verdict and detached
identities/digests.

## Threat model and residual limits

The boundary is designed to resist forged JSON roles, replayed request IDs,
concurrent duplicate mutations, stale compare-and-swap writes, restored head
caches, event gaps/forks, symlink substitution, malformed/noncanonical frames,
oversized frames, key disclosure, and callers attempting to mint arbitrary HMACs.

It assumes the broker OS account, kernel peer-credential facility, filesystem,
and configured UID/GID policy are trusted. An administrator with root access can
replace process memory or files. HMAC provides integrity and authenticity, not
confidentiality or non-repudiation against the key owner. This A1 checkpoint does
not implement deployment, service hardening, key rotation, production role IDs,
Normalizer integration, consumer integration, or migration of existing review
authority data; those require a separately reviewed integration checkpoint.

## Explicit startup example

```text
python tools/run_narrative_review_authority.py \
  --authority-root /var/lib/naz-ai-bot/review-authority \
  --key-file /etc/naz-ai-bot/review-authority.key \
  --socket /run/naz-ai-bot/review-authority.sock \
  --git-root /opt/naz-ai-bot \
  --protected-root /var/lib/naz-ai-bot/narrative-outbox \
  --uid-role 1001:normalizer --uid-role 1002:reviewer \
  --uid-role 1003:consumer \
  --socket-owner-uid 1000 --socket-owner-gid 1000
```

Importing any broker module and invoking CLI `--help` are inert: no key read,
socket creation, authority-root creation, worker, provider, or network activity.

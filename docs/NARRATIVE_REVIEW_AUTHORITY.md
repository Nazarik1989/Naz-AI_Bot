# Narrative Review Authority Broker

This checkpoint adds a standalone authority boundary. The local CP6B-A2 client
integration wires it into the Normalizer reviewer flow and quarantine consumer;
`main.py`, service units, and production remain unchanged and dormant.

## Ownership boundary

Only the broker process loads the strict base64 HMAC key. The key file must be a
regular non-symlink file and, on POSIX, mode `0600`. Clients receive state-free
Unix-socket proxies; they never receive the key, trust service, source registry,
mutable request registry, or signing callable.

The authority root is explicit and must be outside the Git checkout and outside
all configured content inbox, narrative outbox, quarantine registry, and other
protected roots. Symlink path components and overlapping roots fail closed.

The Broker also requires an explicit read-only `--narrative-outbox-root`.
`normalizer-outbox-source-identity-v1` is the code-owned layout:

```text
<narrative-outbox-root>/<source-identity>/story.json
```

The layout does not place `draft_identity` in the filesystem path; the Broker
validates it separately against `source_identity` and `draft_package_digest`.
Clients submit neither paths nor filenames. The outbox root must already exist,
must not overlap the authority root, Git checkout, inbox, registry, or any other
configured protected root, and may not contain symlink path components.

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
{"schema_version":"narrative-review-authority-ipc-v2","request_id":"...","operation":"...","payload":{}}
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

## Version-2 dual-digest contract

Broker contract `narrative-review-authority-v2`, review events
`normalizer-review-event-v2`, and approval attestations
`normalizer-approval-attestation-v2` keep two mandatory digests with different
meanings:

- `draft_package_digest` is the logical canonical package digest. It alone is
  used with `source_identity` and `normalizer-draft-identity-v1` to recompute
  `draft_identity`.
- `narrative_package_digest` is the SHA-256 digest of the exact persisted
  `story.json` bytes referenced by `NarrativeReadyManifest`.

`register_draft` requires only `draft_package_digest`. Every drafted, review,
and approved event carries that logical digest. `append_review` verifies it
against `draft_identity`. `prepare_approval` requires both digests, proves the
registration binding again, and signs both into the V2 attestation.
`commit_approval` requires both values again and checks them against the
prepared event, attestation, and ready manifest before appending the approved
event. `verify_ready` recomputes the draft identity using only the logical
digest and compares only `narrative_package_digest` with the ready manifest.
The `narrative_ready_manifest_digest` is SHA-256 of the canonical persisted
manifest bytes including the required final LF, matching the existing file
verification contract.

At prepare, commit, and verify, the Broker derives the story path itself and
opens that exact regular non-symlink file with a bounded read. It checks the
opened-file identity and size before and after reading, requires exact canonical
UTF-8 JSON bytes with the final LF, and hashes the raw bytes without
parse/reserialize digesting. Prepare compares this computed digest with the
submitted value and signs only the computed value. Commit rereads to detect
changes after prepare; verify rereads to detect changes after commit.

The V2 payloads never contain the ambiguous bare field `package_digest`.
Missing, extra, swapped, stale, coerced, or aliased digest fields fail closed.
The values remain separate fields even when a synthetic test supplies equal
strings. V1 ambiguous Broker requests cannot mutate V2 authority state and are
not silently upgraded. There is no production migration because A1 authority
history has not been deployed.

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
deployment, service hardening, production role IDs, or migration of existing
review authority data. The integrated client path is dormant until an explicit
Broker socket is supplied.

## CP6B-A2 client integration

The Normalizer persists and re-reads a completed draft before sending only its
canonical identities, digests, contract versions, timestamp, and request ID to
`register_draft`. Review `pass`, `reject`, and `supersede` operations are Broker
mutations. Approval is a two-phase Broker transaction: `prepare_approval`, local
no-clobber promotion of `approval-attestation.json` followed by
`narrative_ready.json`, then `commit_approval`. The ready manifest uses the
production status value `narrative_ready`.

The quarantine consumer does not load the Broker trust key. For an identity
layout V2 pair it recomputes both digest roles, requires Broker latest state
`approved`, and calls `verify_ready`. Broker unavailability, denial, protocol
failure, stale state, or digest mismatch fails closed as `needs_narrative` with
no provider, Director, Renderer, or publication path. Existing unambiguous
legacy manifests keep their pre-Broker tree-envelope digest contract and never
call the Broker. Layout selection comes from the exact discovered location and
source-identity contract, not a caller field; mixed, misplaced, or ambiguous
artifacts fail closed before authority access.

The CLI selects this path only through explicit `--review-authority-socket`,
socket owner UID/GID, mode, and timeout options. A configured local authority
root is not a production CLI fallback. Imports, help, scan, coverage snapshot,
dry-run, and non-authoritative list/show paths construct no Broker client and
make no socket connection.

## Explicit startup example

```text
python tools/run_narrative_review_authority.py \
  --authority-root /var/lib/naz-ai-bot/review-authority \
  --narrative-outbox-root /var/lib/naz-ai-bot/narrative-outbox \
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

## Broker-owned draft bundle trust

Production Broker mode uses the closed IPC operation `seal_draft_bundle`.
Only an OS-authenticated `normalizer` peer may call it. The request contains
source, attempt, and draft identities plus exact story JSON, story Markdown,
evidence, review, manifest, completed-claim, and artifact-binding digests. It
contains no path, raw story, quote, prompt, response, or arbitrary byte field.
The Broker returns a detached `normalizer-draft-bundle-v1` receipt and remains
the only process that loads or owns the HMAC key. Generic signing is not an IPC
operation.

Broker health includes the peer role selected from kernel credentials. Provider
authorization accepts an immutable readiness capability only after exact V2
protocol, Broker contract, layout, key ID, and authenticated `normalizer` role
checks. A boolean, path, key object, or caller-constructed substitute is not a
capability.

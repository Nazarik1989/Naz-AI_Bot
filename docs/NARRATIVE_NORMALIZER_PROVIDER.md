# Narrative Normalizer production provider adapter

`narrative_normalizer_provider.py` is the dormant OpenAI-compatible provider
boundary for the review-only Narrative Normalizer. Its reviewed adapter version
is:

```text
normalizer-production-provider-adapter-v1
```

The standalone CLI loads it only through the exact factory spec:

```text
narrative_normalizer_provider:production_adapter_factory
```

The factory returns exactly `(context_provider, NarrativeGenerationService,
GenericEvidenceService)`. Generic evidence support is mandatory; there is no
fallback factory or alternate production tuple. The CLI is not the sole
security boundary: `production_adapter_factory` requires an exact, frozen
`LiveProviderRunAuthorization`. Zero-argument construction, mappings, booleans
and caller-made lookalikes are rejected before SDK/client construction.

## Configuration

The adapter uses existing production names without importing `main.py`:

- `OPENAI_API_KEY`: OpenAI-compatible credential;
- `OPENAI_BASE_URL`: explicit HTTPS endpoint without credentials, query or
  fragment;
- `CONTENT_MODEL_NAME`: coverage planning, extraction and story generation;
- `MODEL_NAME`: evidence and story adjudication;
- `NARRATIVE_NORMALIZER_MODEL_TIMEOUT_SECONDS`: optional exact decimal integer,
  10 through 300 inclusive; default `120`;
- `NARRATIVE_NORMALIZER_ADAPTER_VERSION`: must equal the reviewed adapter
  version;
- `NARRATIVE_NORMALIZER_LIVE`: must be exactly `1`.

The last observed production model IDs were:

```text
CONTENT_MODEL_NAME=openai/gpt-5.4-mini
MODEL_NAME=openai/gpt-5.4
```

These are configuration observations, not hardcoded aliases. Terra/SOL names
are not configured and are not used by the adapter. The content and
adjudication model identities must be distinct. A request cannot override the
operation-owned mapping with another model ID.

The implementation uses the project's existing `openai` dependency and its
`httpx` transport. The SDK client is created only inside a dedicated child
Python process with `max_retries=0`; streaming is
disabled and every component provider request receives the configured bounded
timeout (default 120 seconds, allowed range 10–300). This is a per-request SDK
and transport timeout, not a separate whole-run deadline. There is no HTTP or
adapter retry/backoff for connect/reset failures, timeout, HTTP 429 or HTTP 5xx.
The closed live profiles do not authorize story repair or hidden retry.

## Live gates

Live construction and execution require all of:

1. `normalize` or `resume` with `--enable-local-execution`;
2. `--enable-live-provider`;
3. the exact production factory spec;
4. an explicit `--live-run-profile` and its exact source count;
5. `NARRATIVE_NORMALIZER_LIVE=1` exactly;
6. the exact reviewed adapter version;
7. all credential, endpoint and model configuration;
8. the existing Normalizer trust key and external review-authority gates.

`true`, `yes`, `01`, uppercase or whitespace variants do not enable live mode.
`--all`, `--limit`, a missing identity or a profile/source-count mismatch fail
before adapter construction.

Two immutable code-owned profiles exist:

| Exact profile | Sources | Budget |
|---|---:|---:|
| `normalizer-live-run-canary-v1` | 1 | 5 |
| `normalizer-live-run-first-five-v1` | 5 | 25 |

Both profiles allow one call for each generic E3 operation: coverage planning,
evidence extraction, evidence adjudication, story generation and story
adjudication. Repair is zero. Callers cannot supply a source count, operation
set or budget override.

The reviewed authorization boundary validates the raw CLI gates, environment,
trust service, review-authority path, exact adapter version/spec, distinct
models and the profile's exact distinct source identities. It copies their order into
an immutable tuple, mints a unique run ID, and can be consumed by the factory
only once. This is the one-shot authorization boundary. Authorization creation
itself does not spawn the worker or construct the SDK client.
Before client construction the CLI emits the authorization's privacy-safe JSON
preflight with the adapter version, model mapping, timeout, retry count zero,
the selected profile/source count and its code-owned calculated budget. It explicitly
reports that approval, ready manifests and Reels actions are not enabled.

Import, `--help`, `scan`, `coverage-snapshot`, dry-run, `list`, `show`, `status`,
`verify`, `approve`, `reject` and `supersede` never construct this provider.

## Process isolation and IPC

Executable construction starts `narrative_normalizer_provider_worker.py` with
`subprocess`, `shell=False`, an explicit module directory, a minimal sanitized
environment and a bounded startup handshake. Standard error is discarded and
standard output is a private line-delimited JSON channel; raw worker output is
never forwarded to the operator. A worker crash permanently fails that run.
The parent does not restart it and there is no hidden retry.

The worker owns the exact profile-bound source allowlist, immutable run ID, model IDs,
provider SDK client, per-source operation slots, atomic global budget and call
ledger. Parent services share only an immutable process proxy and detached
serialized ledger snapshots. They do not hold the authorization, provider
client, source allowlist, mutable budget, authoritative ledger, or a worker
module/global object.

Every provider call crosses a closed versioned request envelope with exactly:

```text
schema_version, run_id, request_id, source_identity, operation, payload
```

Before JSON serialization or any pipe write, the parent proxy requires an
exact built-in `dict` envelope, exact built-in `str` values for every scalar
field, an exact built-in `dict` payload, closed schema/operation values and
the exact run/request/source ID grammars. Scalar and mapping subclasses,
coercion, blank IDs and malformed IDs fail locally with zero worker messages,
transport calls, budget use or ledger records. The worker repeats the closed
schema validation after JSON decoding as an independent defensive boundary.

The worker independently revalidates the run, source, operation, unused slot,
remaining budget, exact operation/model mapping and complete final transport
payload privacy immediately before the sole transport attempt. Malformed IPC,
a wrong run, a sixth source and concurrent call 26 are rejected with zero
provider transport calls. The authoritative ledger entry is created only on
the actual transport path.

For the lifetime of one run the worker also owns a private request registry:
`request_id -> canonical request digest -> status -> terminal response`. The
request ID, operation slot and global budget are reserved by one worker-owned
atomic boundary. An exact duplicate receives the cached terminal result or
safe failure without another transport attempt, ledger record, or budget slot.
Reusing the ID with a different schema, run, source, operation or payload fails
as `normalizer_provider_request_conflict`. Concurrent duplicates have the same
semantics. Timeout, HTTP 429/5xx and reset failures are terminal cached outcomes,
not hidden retries.

## Source binding, call budget and audit ledger

Every model call runs inside a Normalizer-owned source scope and is bound by the
worker to the authorization run ID, one of the profile's immutable source
identities and a closed operation. All returned services use one process-owned
run budget. An
unknown, sixth, replacement or resume-introduced source is
rejected before transport. The content/adjudication services share one private,
worker-owned run state and one atomic profile budget; callers cannot inject a
ledger or independent budget.

The child-process run state reserves a call immediately before transport and
stores only immutable
safe records: run/source binding, local operation/request IDs, operation,
configured model, attempt number one, timeout, request/response digests, state
and safe outcome. Prompt and response bodies, credentials and endpoint details
are not stored. Completion requires the private active permit minted by that
actual begin operation. Public consumers receive only an immutable read-only
snapshot; there is no public completion/mutation API.

Per generic source:

| Operation | Maximum calls |
|---|---:|
| Coverage planning | 1 |
| Evidence extraction | 1 |
| Evidence adjudication | 1 |
| Story generation | 1 |
| Story adjudication | 1 |
| Story repair | 0 |

The canary hard maximum is 5 and the first-five hard maximum is 25. The shared private
run state atomically rejects call 6 or concurrent call 26 before
client/SDK/HTTP transport, and the factory
also enforces one call per authorized source/operation. Failed, timed-out and
cancelled transport attempts consume their operation slot.

## Non-destructive manual retry

`--retry-failed` is not a live retry boundary. A production canary retry must
provide all three closed arguments: `--manual-retry-request-id`,
`--expected-failed-attempt-id`, and `--expected-failed-claim-digest`, together
with the exact canary profile and one source identity.

Before the current claim pointer can advance, the Normalizer verifies the
expected terminal failed attempt and SHA-256 of its exact persisted bytes. It
copies those bytes once into append-only attempt history; the archived
predecessor is terminal, sealed, byte-identical and remains available for
audit. A separately sealed manual-retry request record binds the source
identity/digest, predecessor attempt/digest, code-owned new attempt ID,
operator request ID, run profile, safe reason and creation time. The ordinary
claim lifecycle remains the sole current-state engine for the new attempt.

An exact repeated operator request resolves to the same new attempt without a
new provider call. Reusing the request ID with another source, profile or
predecessor fails closed without mutation. The current claim plus immutable
predecessors provides one ordered attempt history; successful drafts and useful
manual-attention packages belong to the new attempt, while the old failure is
never deleted or rewritten.

## Privacy and errors

Only the CP2 system/user request or the reviewed evidence JSON projection is
sent. Before transport the adapter recursively inspects the complete payload:
messages, system/user content, metadata, response schema, nested mapping and
list/tuple values, schema descriptions and model options. Unknown object or
container types fail closed. It rejects credential assignments, actual
configured secret values, trust material, registry/SQLite/review-authority
references, Windows/UNC/POSIX/home absolute paths and `file://` URIs anywhere in
that payload. The child protocol does not forward worker stdout/stderr or
provider log records. Parent-visible errors and detached ledger snapshots
contain only closed reason codes and safe metadata/digests.

A literal `.env` filename in ordinary prose is allowed: discussion such as
“checked the `.env` file” is not credential material. Actual `.env` contents,
secret-bearing assignments, absolute paths to `.env` and configured API-key
values remain rejected before transport.

Provider failures expose only these stable reasons:

- `normalizer_provider_disabled`;
- `normalizer_provider_configuration_invalid`;
- `normalizer_provider_timeout`;
- `normalizer_provider_transport_failed`;
- `normalizer_provider_response_invalid`;
- `normalizer_provider_cancelled`;
- `normalizer_provider_budget_exceeded`.
- `normalizer_provider_request_conflict`.

Raw SDK exceptions, prompts, responses, headers and endpoint queries are never
included. `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and
`GeneratorExit` propagate by exact exception type, with worker-private text
removed.

Transport output must be an exact mapping or a plain JSON-object string. Bytes,
SDK objects, scalar/array JSON, generators, fenced JSON and prose wrappers are
rejected without transport repair. Normalizer/CP parsers remain the semantic
owners.

## Context and activation boundary

The factory supplies a conservative versioned review-only Naz/Void context. It
contains no database or application state and delegates source/plan rebinding to
the existing immutable `TemplateNarrativeContextProvider`. It does not claim to
mirror live mutable character memory. Any future live-state context bridge is a
separate reviewed checkpoint.

The module does not import `main.py`, read SQLite, inspect inbox/registry/outbox,
load trust keys, create directories, configure logging, register jobs, or create
Renderer/worker objects. Importing it does not read environment variables or
import/construct the OpenAI client.

This adapter does not schedule normalization, approve drafts, create ready
manifests, notify Telegram, or activate Reels. The real provider was not exercised
in development: all execution tests use fake transports and network
tripwires. The adapter remains dormant and the Review Authority Broker still
blocks production rollout; separate deployment and first-five execution
authorization are required.

## Coverage-planned generic evidence

The production factory enables `normalizer-evidence-coverage-v2`. Code first
groups source segments into immutable, source-bound blocks. The coverage call
must return one closed disposition for every dynamic block ID; unknown,
duplicate, missing, conflicting, or source-mismatched decisions fail closed.
Sensitive blocks contain only opaque identifiers and digests at the provider
boundary.

Only blocks classified as `evidence_candidate` enter the extraction request.
Code then expands the block plan back to every original segment exactly once
and applies the existing quote, span, entity, number, date, polarity, temporal,
causal, source-binding, and adjudication validators. The generic path owns five
non-retryable operation slots: coverage, extraction, evidence adjudication,
story generation, and story adjudication. Story repair is not available on this
path.

An incomplete or ambiguous coverage plan creates a safe
`normalizer-manual-attention-v1` package containing only opaque identity,
counts, a stable reason, and the closed human actions `use_selected_facts`,
`skip`, and `discuss`. It contains no source text, quotes, prompts, paths, or
credentials; it is not a draft, cannot be approved, does not create Broker
state, and cannot become `narrative_ready`.

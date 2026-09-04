# Content Inbox Scout → Runway

`content-inbox-scout-runway-bridge-v1` connects one immutable Russian Scout
selection to the existing `naz-story-pack-v7` production queue. It never scans
the inbox, reranks candidates, rewrites the prepared material, or constructs a
Runway client. `naz_story_worker.py` remains the only owner of
`provider_from_environment`, `KeyframeRequest`, `SceneRequest`, task polling,
download validation, budgets, and resume state.

The bridge binds the admin, Scout run/candidate/selection identities, the
selection and ready-material digests, title, exact voice-over digest, five
ordered scene-content digests, duration, language, target Story pack, request
identity, and creation timestamp. The first `Собрать в Runway` press is
idempotent and creates only the bridge plus an awaiting-approval Story pack.
It performs zero Runway, TTS, render, publication, Normalizer, or Review
Authority calls.

The approval card plans five `gen4_image` keyframes and five image-to-video
jobs. Each scene keeps the current immutable worker route: object-only scenes
use `gen4_turbo`; scenes requiring the private Naz identity reference use
`gen4.5`. There is no automatic fallback and no hidden provider retry.

The selected Russian screen text is stored as the local Story overlay and is
never sent to Runway. Runway prompts contain physical/cinematic direction only:
one graphite Naz AI Lab, visible memory modules, a disconnected then restored
signal path, separated user lanes, and a final physical proof. Real chats,
usernames, database rows, paths, hashes, logs, credentials, and readable UI are
forbidden.

After explicit approval, the existing official OpenAI TTS boundary may reserve
exactly one synthesis call for the stored voice-over. The append-only voice
state cannot be reset by a duplicate approval. `content-inbox-scout-voice-
composition-v1` then composes the approved CLEAN masters into one 1080×1920,
30 fps, H.264/yuv420p Reel with AAC voice, 14.8–15.2 seconds, and no music. A
voice longer than the bounded intelligibility window blocks final composition;
completed Runway assets remain resumable and are not regenerated.

The old `content-inbox-scout-local-motion-v1` output remains immutable and is
classified as `local_storyboard`, `publishable=false`, and
`superseded_by_runway_flow=true`. It is available only through `Показать
технический сториборд`, clearly labelled as non-final, and never receives a
publication control.

The private delivery boundary sends only the final Scout Reel, with separate
`Опубликовать`, `Переделать`, and `Отменить` controls. No publication occurs
without another admin action.

For a reviewed production selection, the zero-provider one-shot command is:

```text
python tools/create_content_inbox_scout_runway_pack.py \
  --selection-id css-... \
  --request-id scout-runway-css-...
```

The command creates/reuses the exact pack and sends its approval card. It does
not approve it and cannot start paid generation.

## Current-plan failure decision

The provider adapter retains only Runway's allowlisted `failureCode`, a closed
internal category, and a finite retry decision; free-form provider failure text
is discarded. Historical `provider_terminal_failure` manifests remain
readable as `unknown_terminal` and are never rewritten.

The current plan is bound read-only to three audited task-identity digests.
Scenes 01, 03 and 04 are complete and immutable. Scenes 02 and 05 have two
terminal keyframe attempts and cannot receive an automatic third attempt.
`INTERNAL.BAD_OUTPUT.*` permits only an immutable corrected-input scene
proposal, still gated by a later separate cost approval. Its decision card has
provider-free status, cancel, and proposal controls.

## Corrected-scene child revisions

`naz-runway-corrected-scene-revision-v1` closes the gap between a provider-free
BAD_OUTPUT proposal and paid generation. The immutable child plan binds the
exact proposal and parent-manifest digests, all nine reused checksums for
completed scenes 1/3/4, the two historical failed-input/task digest sets, the
new object-only inputs for scenes 2/5, their `gen4_image -> gen4_turbo` routes,
and the code-owned 60-credit ceiling. The parent manifest is never rewritten.

Telegram callbacks carry only the child plan ID and a compact action binding.
The full admin, proposal, parent, scene-set, and cost binding is resolved from
private state. Cost approval creates a separate immutable approval record and
a resumable runtime document under the StoryPack lock; it performs no provider
or TTS call. Exact duplicate approval is idempotent, while any stale proposal,
parent digest, completed checksum, failed history, scene set, or ceiling fails
before mutation.

The existing one-shot story worker discovers only approved child runtimes. It
reuses checksum-verified copies of scenes 1/3/4, submits at most one new
keyframe and one new video task for each corrected input, persists task IDs
before polling, and never treats them as attempt 3 of the failed parent input.
There is no hidden retry or video-model fallback. Final private composition
keeps scene order 1-5 and reuses an already valid voice asset without another
TTS reservation; publication remains a separate disabled boundary.

For future packs, `frontal_identity` is the first canonical identity anchor;
camera angle is independent and off-axis/full-body references are auxiliary.
Health is bound to provider, model, reference-set digest and prompt-policy
version. The states are unknown, healthy, degraded, quarantined and
revalidation-required. Object/mechanism scenes receive no Naz reference.

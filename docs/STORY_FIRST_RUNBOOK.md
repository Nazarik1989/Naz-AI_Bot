# Naz Story-First production runbook

Story-first planning is part of the normal Agent Content route, but media work
is deliberately separate. Reels Maker uses one bounded text-model request to
produce a content-specific treatment, validates it, then atomically writes a
`naz-story-pack-v7`
manifest and returns. `naz_story_worker.py` resumes one state transition at a
time and contains no Telegram or VK publication path.

The administrator opens `Контент → Reels` and sees only the process controls:
`Подтвердить генерацию`, `Другой вариант`, and `Обновить статус`. Initial
planning and each requested variant use one bounded text-model request; they do
not call an image or video provider. Only confirmation makes the pack
eligible for the separate worker. Finished STORY and Reel files are sent to the
administrator's private chat automatically; there is no manual download step.

## Safety defaults

- `NAZ_STORY_RENDER_ENABLED=false` and `NAZ_VIDEO_PROVIDER=disabled` by default.
- The worker only reads `NAZ_VIDEO_API_KEY`; it never falls back to
  `OPENAI_API_KEY`, voice, Voice Hub, or realtime credentials.
- `--check-config` and `--dry-run` make no provider calls.
- One scene is active per worker invocation. New packs allow one primary
  keyframe attempt and at most one policy-controlled automatic fallback;
  automatic attempt three is impossible. `INTERNAL.BAD_OUTPUT.*` never repeats
  unchanged input and requires a separately reviewed corrected-input scene
  revision. Provider preprocessing/dependency failures may receive one delayed,
  changed-prompt fallback. A quarantined auxiliary route may receive one
  frontal-only corrected route. Safety, asset-invalid and unknown failures do
  not retry automatically. Legacy explicit controls remain readable, but none
  can match the audited current plan's completed scene or two-attempt failures.
- `NAZ_VIDEO_AUTO_FALLBACK=true` is rejected. Legacy plans submit Gen-4.5 only
  after the administrator confirms the pending escalation with the existing
  `Подтвердить генерацию` button.
- No Story/Reel autopublication exists. Completed media is delivered only to
  the configured administrator's private bot chat. VK music last-8 state is
  not read or consumed.
- New hybrid plans assign Gen-4.5 to Naz scenes and Turbo to object scenes
  before approval. The approval card shows that mix and its cost. A hybrid
  scene never changes model after approval.
- Semantic-director v8 grounds one `core_thesis`, hook and payoff in explicit
  source-fact references. Every beat owns one scene with a privacy-safe
  `semantic_goal`; every scene after the first explains its transition from the
  previous scene. The application compiles keyframe and Runway prompts only
  from that approved scene brief and never copies the raw private chronicle into
  a provider prompt. A treatment is bound to its exact plan and variant and
  cannot be reused for another variant.
- Deterministic semantic preflight runs before production persistence. It
  rejects missing or unknown source references, uncovered beats, duplicate
  goals, unsupported numbers or claims, generic concepts such as `Lab`,
  unrelated mechanical arcs and prompts that do not match the approved brief.
  Each scene must cite its ordered beat fact. Substantive terms must come from
  the cited facts; four-word source fragments and complete short facts are
  rejected. A metaphor relation must carry at least two cited semantic anchors
  and name exactly one approved physical motion, with no repeated or chained
  action. `visual_relation_to_beat` is preflight evidence, not provider motion
  prose: render prompts receive the privacy-safe semantic goal as non-motion
  context plus exactly one canonical action compiled from the selected arc.
  A physical metaphor is accepted only when it is explicitly marked
  `physical_metaphor`, maps unambiguously to its beat, adds no fact or result and
  passes that same fail-closed preflight. Literal hardware is allowed only when
  that scene's cited facts contain unambiguous physical evidence; ambiguous
  software `module`, `assembly` or `prototype` wording does not qualify.
- Template treatments and older semantic contracts may be inspected through
  `read_manifest` and dry-run tooling, but cannot be approved, replaced by a
  production variant or rendered. Schema v7 with semantic-director v8 is the
  only current paid-worker contract; v1-v6 manifests remain production
  read-only. The Python compatibility helper `story_first_dry_run()` compiles
  its template in memory and does not write into the production pack root.

Run static validation:

```bash
python naz_story_worker.py --check-config
python naz_story_worker.py --once --dry-run
python naz_story_worker.py --plan-id PLAN_ID --dry-run
```

Tracked `deploy/systemd/naz-story-worker.service` and `.timer` files provide an
optional isolated runner. They are deployment artifacts only: review, install,
enable and start them in a separately approved deployment. The timer is queue
polling, not a Telegram/VK publication schedule.

## Runtime dependencies

Install `ffmpeg`, `ffprobe`, and an approved Cyrillic font using the host's
normal package-management/change process. Do not install them directly on
production without separate approval. Configure the exact font file through
`NAZ_STORY_FONT_PATH`.

Runway is the initial production adapter because it exposes asynchronous
submit/retrieve/cancel tasks and downloadable video outputs. New hybrid packs
use `gen4.5` directly for five-second Naz image-to-video scenes and
`gen4_turbo` for five-second object image-to-video scenes. Every video scene is
animated from its approved directed keyframe. The model choice is immutable in
the manifest, is included in the approval estimate and has no automatic
fallback. Legacy packs keep their reviewed manual escalation route. OpenAI Videos/Sora is
not used: the official OpenAI deprecation notice schedules removal of the
Videos API and Sora 2 model aliases on 24 September 2026 and lists no
replacement.

## Private inputs

`NAZ_VIDEO_REFERENCE_DIR` must point outside Git to the private folder with
`naz-primary.jpg`, `naz-secondary.jpg`, and `naz-reference-profile.json`.
Primary is the frontal identity reference; secondary is the three-quarter
identity reference. The legacy v1 profile remains supported. A v2 profile may
name `frontal_identity`, `three_quarter_identity`, and an optional
`full_body_identity`. For a Naz keyframe the worker sends the preferred view
first, then the other available character-plate views, de-duplicated and capped
at Runway Gen-4's three-reference limit. The prompt identifies all views as the
same adult man and explicitly excludes their clothing and backgrounds. Reels
Maker replaces the reference background, clothing, pose, framing and lighting
with the approved Naz AI Lab treatment. The resulting keyframe, never an avatar
binary, becomes the Runway video first frame. Height/build guidance from the
private profile is appended at runtime. Provider images are normalized in
memory to a valid 720×1280 first frame; private binaries and the profile stay
outside Git.

Example private v2 profile (filenames only; do not commit the images or this
profile):

```json
{
  "schema": "naz-reference-profile.v2",
  "persona": "naz",
  "reference_files": {
    "frontal_identity": "naz-front.jpg",
    "three_quarter_identity": "naz-three-quarter.jpg",
    "full_body_identity": "naz-full-body.jpg"
  }
}
```

The approval card lists a content-directed visual concept plus every scene's
privacy-safe semantic goal, transition, physical action and camera, and the
estimated Runway credits. It also shows the grounded thesis and hook → payoff,
without showing source facts or provider prompts. Display-time redaction also
suppresses an exact or partial source fragment if a malformed or tampered
payload reaches the summary code. The semantic director must
express one silent-readable goal, obstacle, corrective test and visible proof.
Every scene keeps the same primary setting and hands its end state to the next
scene. Final Reel edits preserve that causal order and may only repeat adjacent
reframed fragments when a short treatment needs seven cuts.
The application injects one restrained matte-black Naz lab wardrobe across all
human scenes. Transport metadata, paths, generic `fact N` placeholders, costume
changes and backward timeline jumps are rejected. Naz AI Lab remains a coherent
world chosen per episode, not one mandatory room for every episode. A current v7
pack first creates one asynchronous directed keyframe per scene with
`gen4_image`; identity scenes keep the tagged `@Naz` reference while object-only
scenes use the text-only form. It then animates that immutable keyframe with the
video route. The image-to-video prompt contains only one continuous physical
action, one camera move and one visible finishing state; it does not repeat the
full story summary. `gen4_image_turbo` is not used for new keyframes. A bounded retry
of legacy Turbo identity failures stays in the same plan, preserves completed
scenes and appears in the existing progress card. A separately approved
reference-quality retry may substitute the frontal private reference at runtime;
it does not mutate the immutable scene treatment or copy either reference into
Git. The concise identity recovery likewise leaves the manifest treatment
unchanged and is never an automatic fallback.
No keyframe or video task is submitted before explicit approval. v1-v6 packs
remain inspectable but are read-only and cannot silently use the old direct-avatar route.

`NAZ_STORY_MUSIC_LIBRARY` may point to a private music folder. Each audio file
must have a same-name JSON sidecar, for example `track.m4a.json`:

```json
{"bpm": 120, "license": "license-record", "source": "licensed-library"}
```

An explicit beat grid and `track_id` are optional. The previous single JSON
allowlist format remains supported:

```json
{
  "tracks": [{
    "track_id": "licensed-track-id",
    "path": "/private/music/track.m4a",
    "bpm": 120,
    "beat_grid": [0.0, 0.5, 1.0, 1.5, 2.0],
    "license": "internal-license-record",
    "source": "licensed-local-library",
    "checksum": "sha256"
  }]
}
```

Missing or invalid music leaves CLEAN/STORY media intact and sets Reel state
to `blocked_music`. The production pipeline remains music-only: this semantic
contract does not add voiceover and does not change the composer or audio path.

### Initial generated music library

Stable Audio 3.0 is used only by a bounded manual library command. It is not
called by the Story timer and never receives an uploaded song or copyrighted
reference. The catalog contains exactly eight original instrumental masters:
three Midnight Wave, three Dark Melodic House, and two Emotional Future Garage.

```bash
python -m naz_audio_library --check-config
python -m naz_audio_library --plan
python -m naz_audio_library --generate-initial-library \
  --max-new-tracks 8 --confirm-paid-calls 8
```

The last command additionally requires `NAZ_AUDIO_GENERATION_ENABLED=true`.
Each successful master receives a strict private sidecar with checksum, actual
duration, tempo and confidence-gated beat grid derived from decoded audio,
model/provider, generation receipt and rights provenance. Full
prompts and credentials are not written. An interrupted asynchronous job is
polled by its durable generation ID; the POST is never repeated implicitly.
The composer cuts a 12–20 second Reel segment locally on the beat grid. Track
selection uses the private Story rotation state and does not consume Telegram
or VK publication rotation.

## Safe private E2E

1. Deploy only after PR approval and a new explicit deploy instruction.
2. Install/check ffmpeg, ffprobe and the Cyrillic font through the approved
   server change process.
3. Place the approved avatar and licensed music/library outside the repository.
4. Add a dedicated provider credential to `/opt/naz-ai-bot/.env`; do not reuse
   OpenRouter or voice keys.
5. Keep rendering disabled and run `--check-config`, then `--once --dry-run`.
6. Obtain explicit cost approval and set `NAZ_VIDEO_PROVIDER=runway` plus
   `NAZ_STORY_RENDER_ENABLED=true`.
7. Process one private plan with `--plan-id`. Inspect every CLEAN/STORY/Reel,
   manifest checksum and media probe. A pack is not complete unless every
   expected MP4 passes codec, portrait, duration, frame-rate and motion checks.
8. Keep social publishing disabled; publishing is outside this workflow.

Provider task IDs are persisted immediately after submission. A restart polls
the same task instead of creating a duplicate. Completed CLEAN and STORY files
are checksummed and are never regenerated merely to add text.

## Rollback

Stop invoking the worker, restore `NAZ_STORY_RENDER_ENABLED=false`, and deploy
the pre-change application SHA. Do not delete pack directories: their manifests
and completed assets are resumable evidence. Existing v1-v6 manifests remain
read-only compatible and are never upgraded in place; rollback does not rewrite
or downgrade a v7 manifest.

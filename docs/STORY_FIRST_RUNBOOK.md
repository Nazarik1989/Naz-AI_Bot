# Naz Story-First production runbook

Story-first planning is part of the normal Agent Content route, but media work
is deliberately separate. Reels Maker uses one bounded text-model request to
produce a content-specific treatment, validates it, then atomically writes a
`naz-story-pack-v5`
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
- One scene is active per worker invocation. Limits are at most seven scenes,
  two retries, seven paid jobs and 56 generated seconds per UTC day by default.
- `NAZ_VIDEO_AUTO_FALLBACK=true` is rejected. Gen-4.5 is never submitted until
  the administrator confirms the pending escalation with the existing
  `Подтвердить генерацию` button.
- No Story/Reel autopublication exists. Completed media is delivered only to
  the configured administrator's private bot chat. VK music last-8 state is
  not read or consumed.

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
submit/retrieve/cancel tasks and downloadable video outputs. The fixed priority
is `gen4_turbo,gen4.5`: Turbo is the primary five-second image-to-video route;
Gen-4.5 is the separately confirmed secondary route and may also handle
object-only text-to-video scenes. An incompatible object-only Turbo scene is
stopped before the API call. OpenAI Videos/Sora is
not used: the official OpenAI deprecation notice schedules removal of the
Videos API and Sora 2 model aliases on 24 September 2026 and lists no
replacement.

## Private inputs

`NAZ_VIDEO_REFERENCE_DIR` must point outside Git to the private folder with
`naz-primary.jpg`, `naz-secondary.jpg`, and `naz-reference-profile.json`.
Primary is the frontal identity reference; secondary is the three-quarter
identity reference. Exactly one is used only while generating a directed scene
keyframe. Reels Maker must replace the avatar background, pose, framing and
lighting with the approved Naz AI Lab treatment. The resulting keyframe, never
the avatar itself, becomes the Runway video first frame. Height/build guidance
from the private profile is added only to medium/wide Naz shots and is ignored
for close-ups, macro and object-only scenes. Provider images are normalized in
memory to a valid 720×1280 first frame; private binaries and the profile stay
outside Git.

The approval card lists a content-directed visual concept plus every scene's
location, physical action and camera, and the estimated Runway credits. The
concept and actions are generated from cleaned causal facts and validated before
the pack exists; transport metadata, paths and generic `fact N` placeholders are
rejected. Naz AI Lab is a coherent world, not one mandatory room or a fixed
action sequence. A v5
pack first creates one asynchronous directed
keyframe per scene (`gen4_image_turbo` with `@Naz` for identity scenes,
`gen4_image` for object-only scenes), then animates that immutable keyframe.
No keyframe or video task is submitted before explicit approval. v1/v2/v3/v4 packs
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
to `blocked_music`.

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
and completed assets are resumable evidence. Existing v1 dry-run manifests are
read-only compatible and are never upgraded in place.

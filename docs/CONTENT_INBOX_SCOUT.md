# Content Inbox Scout

Content Inbox Scout is a manual, admin-only archive ranking boundary. It reads only the configured project under `AGENT_CONTENT_INBOX`, performs deterministic local discovery and prefiltering without a model, then uses one closed `CONTENT_MODEL_NAME` ranking request.

## Operator interface

- `/inbox_best`
- `/inbox_best 3 reel`
- `/inbox_best_refresh`
- Content menu: `🔥 Лучшее из Inbox`

The normal command reuses the immutable Russian ranking for the same source snapshot, output language, and ranking-contract version. Refresh is explicit and request-bound. The maximum displayed result count is five.

Each private result card provides `Подготовить материал`, `Подробнее`, and `Скрыть`. Details are served from the stored ranking with no provider call. Preparation uses one closed `content-inbox-ready-material-v3` call for only the selected candidate. A prepared card offers `Выбрать для Reel`: this promotes the exact immutable Russian ready artifact into a private, digest-bound selection and shows `Собрать Reel`, `Показать материал`, `Другой из TOP`, and `Отменить`. Showing the material or returning to the stored ranking makes no provider call.

## Private selected-material Reel path

- Selection contract: `content-inbox-selected-material-v1`
- Job contract: `content-inbox-scout-reel-job-v1`
- Render profile: `content-inbox-scout-local-motion-v1`
- Technical receipt: `content-inbox-scout-reel-receipt-v1`

The selection binds the admin, run, candidate, ranking artifact, ready artifact, title, post, exact voice-over, caption, cover text, 15-second duration, and five-scene count. The source ready artifact is never rewritten. Exact duplicate promotion is byte-idempotent, while divergent request reuse fails closed.

`Собрать Reel` reserves one deterministic durable job and renders the five stored, contiguous scenes with local Pillow motion graphics and ffmpeg. It uses the stored Russian voice-over verbatim through the configured official OpenAI TTS boundary, at most once per job and with no retry. Narrative, ranking, preparation, image, video, and music providers are not part of this path. Completed duplicates return the same validated MP4 without another TTS or render call.

The private output is 1080x1920, 30 fps H.264/yuv420p with AAC voice-over, no music, and a duration from 14.8 through 15.2 seconds. A validated preview is sent only to the admin. Its `Опубликовать`, `Переделать`, and `Отменить` controls do not publish automatically; publication remains a separate explicit admin boundary.

## Contracts and limits

- Run: `content-inbox-scout-run-v2`
- Ranking provider response: `content-inbox-scout-ranking-v4`
- Ranking artifact: `content-inbox-scout-ranking-artifact-v4`
- Prepared-material provider response: `content-inbox-ready-material-v3`
- Prepared-material artifact: `content-inbox-ready-material-artifact-v3`
- Locale-aware snapshot index: `content-inbox-scout-snapshot-index-v2`
- Preference: `content-inbox-scout-preference-v1`
- At most 500 locally discovered candidates and 12 candidates in the model shortlist
- One ranking call per new snapshot; no retry or repair
- One preparation call per selected candidate; no retry or repair
- Code-owned short Reel specifications: 15 seconds/5 scenes, 18 seconds/6 scenes, or 20 seconds/7 scenes

The model supplies editorial scores and safe explanatory text, but it does not supply rank, final score, format, duration, or scene count. Code applies the fixed 35/35/15/10/5 weighting, deterministic penalties, and tie-breakers. For Reel mode it assigns `short_reel` and selects the exact duration/scene policy from local scene complexity and the model's Reel-ease score. Category C is always marked at least `requires_manual_check`. Persisted v1/v2/v3 ranking artifacts remain read-only and audit-readable. They are never reused as a Russian v4 result.

Ranking v4 uses an exact closed `candidate_evaluations` object rather than an array and binds `output_language=ru`. Code creates one required `candidate_01` through `candidate_N` slot for every deterministic shortlist entry, and each slot binds its own candidate identity with `const`. A missing, extra, duplicated, or swapped candidate therefore fails at the structural boundary. JSON object order has no authority.

The provider-facing schemas use only the portable closed subset (`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, and `const`). Ranking score enums are the integers 0 through 100. Known duplicate reason codes are canonicalized in first-occurrence order; unknown reason codes fail closed. Structurally valid candidates whose editorial title, pitch, or explanation fails privacy or the deterministic Russian-language policy are persisted only as an ineligible code-owned record with a stable reason, field, token counts, and blank public text. They cannot appear in cards or enter preparation. A ranking with fewer than three Russian display candidates fails closed after the single ranking call.

The Russian validator tokenizes Unicode alphabetic words, distinguishes Cyrillic and Latin script, and excludes backtick code, function identifiers, snake_case, CamelCase, and the closed technical-name allowlist from its natural-prose calculation. Short fields need at least one Cyrillic word. Long fields need at least five Cyrillic tokens, a Cyrillic share of at least 60 percent, and no run of three ordinary English prose tokens. `output_language=ru` is a binding, not a substitute for this code-owned validation. No translation call is made.

Prepared-material v3 requests expose exactly the code-owned number of named scene-content fields (`scene_01` through `scene_N`) and bind `output_language=ru`. The model supplies only Russian screen text and a Russian visual brief for each scene. Code validates every user-facing field before persistence and constructs ordered, contiguous integer timings beginning at zero and ending at the exact stored duration, with every scene lasting at least two seconds. Persisted v1/v2 prepared artifacts remain audit-readable under their original runs.

Run identity, operator request, ranking marker, ranking artifact, and the v2 snapshot index bind both `output_language=ru` and `content-inbox-scout-ranking-v4`. The legacy snapshot index is left byte-identical and cannot select the default Russian run. A successful Russian refresh creates the locale-aware index; the next ordinary command reuses that exact run with zero provider calls.

## Privacy and state

The scanner accepts only regular, non-symlink Markdown files under exact project/date directories. It enforces per-file and aggregate size limits, rejects path escape and technical-noise candidates, and receives the existing Naz risk detector and redactor as injected dependencies. Paths, filenames, raw manifests, source hashes, secrets, and redacted original values are not sent to the model or Telegram.

Private state defaults to `/var/lib/naz-ai-bot/content-inbox-scout`. Directories are mode `0700`; files are atomically created with mode `0600`; existing artifacts are immutable and conflicting reuse fails closed. The state root must remain outside the repository, Inbox, Registry, and Normalizer roots.

Selection and Reel job state is stored below the same private root in `reel-production`. It remains outside Git, Inbox, Registry, and Normalizer attempts. The production promotion utility only locates exactly one current Russian selected preference, creates its immutable selection, and sends the ready-to-build card:

```text
python tools/promote_content_inbox_scout_selection.py --request-id <unique-id>
```

That command performs no ranking, preparation, narrative, TTS, rendering, or publication call.

The module imports without scanning, provider construction, filesystem writes, or network calls. There is no schedule. It does not import or invoke Narrative Normalizer, Review Authority Broker, Renderer, or publication code.

## One-shot production invocation

After a reviewed deploy, a single private ranking can be delivered without starting a second bot process:

```text
python tools/run_content_inbox_scout.py --count 3 --format reel --request-id <unique-id>
```

This performs the local scan and at most one ranking call. It does not prepare a candidate automatically.

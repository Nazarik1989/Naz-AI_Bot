# Content Inbox Scout

Content Inbox Scout is a manual, admin-only archive ranking boundary. It reads only the configured project under `AGENT_CONTENT_INBOX`, performs deterministic local discovery and prefiltering without a model, then uses one closed `CONTENT_MODEL_NAME` ranking request.

## Operator interface

- `/inbox_best`
- `/inbox_best 3 reel`
- `/inbox_best_refresh`
- Content menu: `🔥 Лучшее из Inbox`

The normal command reuses the immutable ranking for the same source snapshot. Refresh is explicit and request-bound. The maximum displayed result count is five.

Each private result card provides `Подготовить материал`, `Подробнее`, and `Скрыть`. Details are served from the stored ranking with no provider call. Preparation uses one closed `content-inbox-ready-material-v2` call for only the selected candidate. `Выбрать`, `Другой из TOP`, and `Пропустить` only update private operator state; they do not publish or start media production.

## Contracts and limits

- Run: `content-inbox-scout-run-v1`
- Ranking provider response: `content-inbox-scout-ranking-v3`
- Ranking artifact: `content-inbox-scout-ranking-artifact-v3`
- Prepared-material provider response: `content-inbox-ready-material-v2`
- Prepared-material artifact: `content-inbox-ready-material-artifact-v2`
- Preference: `content-inbox-scout-preference-v1`
- At most 500 locally discovered candidates and 12 candidates in the model shortlist
- One ranking call per new snapshot; no retry or repair
- One preparation call per selected candidate; no retry or repair
- Code-owned short Reel specifications: 15 seconds/5 scenes, 18 seconds/6 scenes, or 20 seconds/7 scenes

The model supplies editorial scores and safe explanatory text, but it does not supply rank, final score, format, duration, or scene count. Code applies the fixed 35/35/15/10/5 weighting, deterministic penalties, and tie-breakers. For Reel mode it assigns `short_reel` and selects the exact duration/scene policy from local scene complexity and the model's Reel-ease score. Category C is always marked at least `requires_manual_check`. Persisted v1 and v2 ranking artifacts remain readable; a separately persisted, exactly bound safe v1 or v2 provider response may be projected into a new artifact without another provider call.

Ranking v3 uses an exact closed `candidate_evaluations` object rather than an array. Code creates one required `candidate_01` through `candidate_N` slot for every deterministic shortlist entry, and each slot binds its own candidate identity with `const`. A missing, extra, duplicated, or swapped candidate therefore fails at the structural boundary. JSON object order has no authority.

The provider-facing schemas use only the portable closed subset (`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, and `const`). Ranking score enums are the integers 0 through 100. Known duplicate reason codes are canonicalized in first-occurrence order; unknown reason codes fail closed. Structurally valid candidates whose editorial title, pitch, or explanation fails the privacy-safe local text policy are persisted only as an ineligible code-owned record with a stable reason and blank public text. They cannot appear in cards or enter preparation. A ranking with fewer than three safe candidates (when at least three were shortlisted) fails closed after the single ranking call.

Prepared-material v2 requests expose exactly the code-owned number of named scene-content fields (`scene_01` through `scene_N`). The model supplies only screen text and a visual brief for each scene. Code constructs ordered, contiguous integer timings beginning at zero and ending at the exact stored duration, with every scene lasting at least two seconds. Persisted v1 prepared artifacts remain readable.

## Privacy and state

The scanner accepts only regular, non-symlink Markdown files under exact project/date directories. It enforces per-file and aggregate size limits, rejects path escape and technical-noise candidates, and receives the existing Naz risk detector and redactor as injected dependencies. Paths, filenames, raw manifests, source hashes, secrets, and redacted original values are not sent to the model or Telegram.

Private state defaults to `/var/lib/naz-ai-bot/content-inbox-scout`. Directories are mode `0700`; files are atomically created with mode `0600`; existing artifacts are immutable and conflicting reuse fails closed. The state root must remain outside the repository, Inbox, Registry, and Normalizer roots.

The module imports without scanning, provider construction, filesystem writes, or network calls. There is no schedule. It does not import or invoke Narrative Normalizer, Review Authority Broker, Renderer, or publication code.

## One-shot production invocation

After a reviewed deploy, a single private ranking can be delivered without starting a second bot process:

```text
python tools/run_content_inbox_scout.py --count 3 --format reel --request-id <unique-id>
```

This performs the local scan and at most one ranking call. It does not prepare a candidate automatically.

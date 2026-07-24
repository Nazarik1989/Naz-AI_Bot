# Editorial Orchestrator v1 — Naz

## Scheduled call graph

Before consolidation, scheduled releases made overlapping choices in several places:

- Telegram: `setup_autoposting -> auto_post_job -> rubric/topic selector -> character_state.plan_content -> content_formats.choose_format -> semantic theme/card gate -> text generation -> visual prompt generation -> image -> send`.
- Source monitor: `setup_source_monitoring -> source_monitor_job -> source choice -> semantic plan/gate -> text generation -> separate visual generation -> send`.
- Agent chronicle: `setup_agent_content_sync -> agent_content_sync_job -> process_agent_content_date -> random editor profile -> semantic plan/gate -> text/visual generation`.
- VK: `naz-vk-producer.timer -> naz_vk_producer -> create_naz_vk_job -> random rubric/gaming planner -> semantic plan/gate -> separate visual prompt -> track selection -> filesystem queue`.

The scheduled runtime graph is now:

`eligible sources + eligible rubrics + character state + confirmed publication history -> scheduled_plan -> plan_release -> immutable EditorialPlan -> one generation package -> same-plan visual -> local quality/safety -> Telegram send or existing VK queue`.

`plan_release(context) -> EditorialPlan` is the only categorical decision entrypoint in these scheduled routes. Legacy selectors remain available to manual compatibility paths but are not called by migrated routes. A technical JSON/schema failure may retry once with the exact same plan and axes. A content rejection is not disguised as a technical error.

Publication history is committed only after a successful Telegram send or a validated `vk_publication_receipt.v1`. Merely moving a VK job to `done/` is insufficient. A periodic receipt sync is independent from producer creation, and `plan_id` makes Telegram/VK crossposts idempotent. Drafts, rejects and technical errors do not spend cooldown.

Each axis uses `max(1, round(persona_wide_pool_size * 0.60))`. Compatibility still selects from the constrained subset. If that subset is fully blocked, the least-recently-used compatible value is selected with a deterministic `plan_id` tie-break; the slot is not dropped.

Safe release observability stores slot capture time, `plan_id`, bounded generation/image-QA/history statuses, and per-destination receipt IDs. Pixel QA is honestly recorded as `not_run` until a real validator is invoked. Prompts and private source text are not part of this record. `resolved_naz_schedule_snapshot()` preserves the operator-facing Telegram/VK timezone-and-slot view; `resolved_naz_deploy_schedule_snapshot()` exposes the canonical `naz.telegram`/`naz.vk` `daily_times`/`weekly_times` preflight schema, with no other environment values. Scheduled callbacks create short-lived safe work markers for coordinated deploy checks. The default marker root is `/var/lib/naz-ai-bot`; schema `naz_scheduled_work.v2` contains only `schema`, `label`, `pid`, `process_start_id`, and `started_at`. Labels are `telegram_autopost`, `crosspost_exchange`, `source_monitor`, `agent_content_sync`, `vk_embedded_producer`, `vk_systemd_producer`, and `vk_receipt_sync`. Linux markers hold an advisory lock for the callback lifetime; PID plus process-start identity prevents stale SIGKILL files or PID reuse from appearing in-flight.

## Story-first mode

Story-first is a `production_mode` of the same `EditorialPlan`. A verified work chronicle selects it only when it has a concrete action, a verifiable source reference, a visual process, at least four causal facts, a real result, and no secret/private flags. Diversity cannot force Story-first.

The dry-run produces one `StoryPackPlan`, CLEAN/STORY contracts, two Reel EDLs, `story_manifest.json`, and `caption_pack.md` in the configured persistent storage outside Git. It never creates placeholder video. Re-running the same `plan_id` resumes the same pack.

Story scenes remain 4вЂ“8 seconds. Every Reel fragment cut from a CLEAN master is 0.4вЂ“2.0 seconds; that range is not a transition duration. Reel EDLs reorder source scenes and carry explicit source/reel shot sizes plus crop/scale instructions, with at least one reframed fragment.

Production inventory at implementation time found image providers but no configured video provider contract and no usable ffmpeg binary. The renderer is therefore explicitly `unavailable`; no Instagram/VK Stories publisher was invented and no automatic public Story publication was enabled.

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

Publication history is committed only after a successful Telegram send or a confirmed VK done receipt. `plan_id` makes a crosspost idempotent. Drafts, rejects and technical errors do not spend cooldown.

## Story-first mode

Story-first is a `production_mode` of the same `EditorialPlan`. A verified work chronicle selects it only when it has a concrete action, a verifiable source reference, a visual process, at least four causal facts, a real result, and no secret/private flags. Diversity cannot force Story-first.

The dry-run produces one `StoryPackPlan`, CLEAN/STORY contracts, two Reel EDLs, `story_manifest.json`, and `caption_pack.md` in the configured persistent storage outside Git. It never creates placeholder video. Re-running the same `plan_id` resumes the same pack.

Production inventory at implementation time found image providers but no configured video provider contract and no usable ffmpeg binary. The renderer is therefore explicitly `unavailable`; no Instagram/VK Stories publisher was invented and no automatic public Story publication was enabled.

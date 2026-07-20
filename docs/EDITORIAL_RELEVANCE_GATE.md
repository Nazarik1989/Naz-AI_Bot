# Naz publication contract

The publication pipeline has one enforced precedence order:

1. security and access control;
2. schedule and publication type;
3. Naz persona identity;
4. the immutable brief for the current post;
5. the Naz visual bible;
6. the music allowlist and shared last-eight rotation;
7. creative variation inside the brief.

For scheduled Telegram, sourced monitoring, approved visual archive, cross-post continuation, content-agent publication, and VK production, `editorial_policy.ContentBrief` is created before text or images. Text and image prompts consume the same frozen object. A validator may accept or reject; it cannot replace the source, rubric, thesis, scene, persona, or destination.

The previous divergence points were:

- semantic release retries could select a new theme after rejection;
- scheduled stories could randomly replace the scheduled source;
- image prompts independently re-derived a scene from post text and mutable character state;
- provider failure could select a local/random fallback image;
- Telegram media failure could fall through to text-only publication;
- queued VK text was stored as an unpublished draft.

These routes now fail closed. Rejected variants are not written to semantic memory. VK stores only a text-free publication receipt until the shared consumer confirms publication. Existing interactive content tools remain available, but automated publication uses the immutable contract and structured reason codes.

Queue schema `vk_publish_job.v2` carries only safe policy metadata. The consumer remains compatible with existing `v1` jobs. No prompt, post text, secret, environment value, or private-memory content is placed in metadata or structured gate logs.

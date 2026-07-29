import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import character_state
import main
import naz_vk_music
from prompts import (
    MATERIAL_RUBRIC,
    MATERIAL_VISUAL_PROMPT,
    NAZ_CANONICAL_MATERIALS,
    NAZ_CANONICAL_PALETTE,
    NAZ_VISUAL_RUNTIME_DIRECTION,
)


class NazVisualIdentityTests(unittest.TestCase):
    def test_material_prompt_contains_canonical_palette_and_materials(self) -> None:
        for color in NAZ_CANONICAL_PALETTE:
            with self.subTest(color=color):
                self.assertIn(color, MATERIAL_VISUAL_PROMPT)
        for material in NAZ_CANONICAL_MATERIALS:
            with self.subTest(material=material):
                self.assertIn(material, MATERIAL_VISUAL_PROMPT)

    def test_copper_is_not_a_brand_color(self) -> None:
        self.assertNotIn("copper", " ".join(NAZ_CANONICAL_PALETTE).casefold())
        self.assertIn("Copper is not a brand color", NAZ_VISUAL_RUNTIME_DIRECTION)
        self.assertIn("natural skin warmth", NAZ_VISUAL_RUNTIME_DIRECTION)

    def test_runtime_direction_forbids_cheap_visual_cliches(self) -> None:
        normalized_direction = " ".join(NAZ_VISUAL_RUNTIME_DIRECTION.split())
        forbidden_cliches = (
            "golden pseudo-luxury",
            "random circuit boards or code streams",
            "humanoid robots without narrative need",
            "overloaded HUD interfaces",
            "cheap cyberpunk/neon",
            "mesh on every object",
            "large logos",
            "arbitrary purple gradients",
        )
        for phrase in forbidden_cliches:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized_direction)

    def test_material_uses_existing_schedules_and_three_frame_runtime(self) -> None:
        self.assertEqual(MATERIAL_RUBRIC["kind"], "daily")
        self.assertEqual(MATERIAL_RUBRIC["image_count"], "3")
        self.assertEqual(
            naz_vk_music.rotation_pool_size(["daily", "gaming"]),
            len(naz_vk_music.APPROVED_TRACKS),
        )
        self.assertEqual(main.AUTOPOST_TIMES, "10:00,14:00,18:00,22:00")
        self.assertEqual((main.NAZ_VK_DAILY_TIME, main.NAZ_VK_GAMING_TIME), ("10:30", "16:30"))
        self.assertNotIn(
            MATERIAL_RUBRIC["name"],
            {str(rubric["name"]) for rubric in main.NAZ_TELEGRAM_RUBRICS},
        )

    def test_build_image_prompt_includes_selective_avatar_canon(self) -> None:
        model = AsyncMock(return_value='"final image prompt"')
        with patch.object(
            main.memory,
            "load_character_state",
            return_value=character_state.CharacterState(),
        ), patch.object(main, "call_gpt", new=model):
            result = asyncio.run(
                main.build_image_prompt(1, "городская сцена", "Человек проверяет прототип")
            )
        self.assertEqual(result, "final image prompt")
        system_prompt = model.await_args.args[0][0]["content"]
        self.assertIn("PRIMARY CHARACTER REFERENCE", system_prompt)
        self.assertIn("selective design system", system_prompt)
        self.assertIn("not a mandatory composition", system_prompt)

    def test_current_generator_accepts_three_material_frames(self) -> None:
        prompt_builder = AsyncMock(return_value="material prompt")
        image_provider = AsyncMock(side_effect=[b"one", b"two", b"three"])
        with patch.object(
            main,
            "build_image_prompt",
            new=prompt_builder,
        ), patch.object(main, "generate_image_bytes", new=image_provider):
            images, prompt = asyncio.run(
                main.generate_images_for_post(
                    1,
                    "MATERIAL / МАТЕРИЯ",
                    "Первый рабочий прототип",
                    count=3,
                    platform="vk",
                )
            )
        self.assertEqual(images, [b"one", b"two", b"three"])
        self.assertEqual(prompt, "material prompt\n---\nmaterial prompt\n---\nmaterial prompt")
        self.assertEqual(
            [call.kwargs["variant"] for call in prompt_builder.await_args_list],
            [1, 2, 3],
        )
        self.assertEqual(
            [call.kwargs["variant"] for call in image_provider.await_args_list],
            [1, 2, 3],
        )


if __name__ == "__main__":
    unittest.main()

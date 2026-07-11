import unittest

import gaming_vertical as gaming


class GamingVerticalTests(unittest.TestCase):
    def test_characters_have_distinct_rubrics(self):
        naz = gaming.plan_gaming_content("naz", "новая механика", [])
        void = gaming.plan_gaming_content("void", "новая механика", [])
        self.assertNotEqual(naz["intent"], void["intent"])
        self.assertTrue(naz["facet"].startswith("gaming_"))
        self.assertTrue(void["facet"].startswith("gaming_"))

    def test_cooldown_avoids_recent_rubric_and_format(self):
        first = gaming.plan_gaming_content("naz", "одна тема", [])
        recent = [{"facet": first["facet"], "content_format": first["content_format"]}]
        second = gaming.plan_gaming_content("naz", "одна тема", recent)
        self.assertNotEqual(first["facet"], second["facet"])
        self.assertNotEqual(first["content_format"], second["content_format"])

    def test_commercial_prompt_blocks_account_sales_and_fake_experience(self):
        plan = gaming.plan_gaming_content("naz", "скины", [], commercial=True)
        prompt = gaming.prompt_context("naz", plan)
        self.assertNotEqual(plan["commercial_angle"], "без продажи")
        self.assertIn("продажу аккаунтов", prompt)
        self.assertIn("лично играл", prompt)


if __name__ == "__main__":
    unittest.main()

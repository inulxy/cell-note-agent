import unittest

from cell_note_agent.web.app import (
    conversation_gate_prompt,
    default_web_search_preferences,
    normalize_web_search_preferences,
    revise_web_search_preferences,
)


class WebSearchInteractionTests(unittest.TestCase):
    def test_initial_prompt_infers_known_search_slots(self):
        preferences = default_web_search_preferences(
            "搜索 30 个人类肺癌 multiome 数据集，只要处理后矩阵，hg19，控制在 5GB 内",
            {"sources": ["geo", "sra"]},
        )

        self.assertEqual(preferences["data_type"], "10x Multiome")
        self.assertEqual(preferences["tissue_or_disease"], "特定疾病")
        self.assertEqual(preferences["acquisition"], "处理后的矩阵或 fragments")
        self.assertEqual(preferences["candidate_limit"], 30)
        self.assertEqual(preferences["size_limit_gb"], 5.0)
        self.assertEqual(preferences["target_genome_build"], "hg19")
        self.assertEqual(preferences["sources"], ["geo", "sra"])

    def test_free_form_revision_updates_without_starting_search(self):
        current = default_web_search_preferences("搜索人类 scATAC 数据集")
        revised = revise_web_search_preferences(
            "搜索人类 scATAC 数据集",
            current,
            "改成只看肺癌处理后矩阵，展示全部候选，只搜索 GEO 和文献",
        )

        self.assertEqual(revised["tissue_or_disease"], "特定疾病")
        self.assertEqual(revised["acquisition"], "处理后的矩阵或 fragments")
        self.assertIsNone(revised["candidate_limit"])
        self.assertIn("全部候选", revised["candidate_limit_request"])
        self.assertEqual(revised["sources"], ["geo", "literature"])
        self.assertIn("肺癌", revised["user_note"])

    def test_browser_values_are_normalized_and_sources_whitelisted(self):
        normalized = normalize_web_search_preferences(
            "搜索人类 scATAC",
            {
                "candidate_limit_request": "50 个",
                "sources": ["geo", "shell", "sra"],
                "user_note": "优先 10x",
            },
        )

        self.assertEqual(normalized["candidate_limit"], 50)
        self.assertEqual(normalized["sources"], ["geo", "sra"])
        self.assertEqual(normalized["user_note"], "优先 10x")

    def test_search_prompt_uses_interaction_card_not_numeric_menu(self):
        text = conversation_gate_prompt("search", {"preferences": {}})

        self.assertIn("下方只显示仍需确认", text)
        self.assertNotIn("回复 6 个编号", text)
        self.assertNotIn("1)", text)


if __name__ == "__main__":
    unittest.main()

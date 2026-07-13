import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gsuite_https as g

class NotionRouting(unittest.TestCase):
    def test_strategy_has_db_default(self):
        self.assertIn("策略學習報", g.NOTION_DB_DEFAULT)
    def test_strategy_env_key(self):
        self.assertEqual(g.notion_env_key("策略學習報"), "NOTION_STRATEGY_DB_ID")
        self.assertEqual(g.notion_env_key("AI新聞報"), "NOTION_NEWS_DB_ID")
        self.assertEqual(g.notion_env_key("日文報"), "NOTION_JP_DB_ID")
    def test_strategy_props_present(self):
        self.assertIn("策略學習報", g.NOTION_SUMMARY_PROP)
        self.assertIn("策略學習報", g.NOTION_DEFAULT_ICON)

if __name__ == "__main__":
    unittest.main()

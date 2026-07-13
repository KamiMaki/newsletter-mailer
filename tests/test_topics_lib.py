import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import topics_lib as t

class TopicsLedger(unittest.TestCase):
    def test_append_then_recent(self):
        with tempfile.TemporaryDirectory() as td:
            led = str(Path(td) / "news.jsonl")
            t.append_topics(led, "2026-06-20", ["A", "B"])   # 23 days before -> out of 14d window
            t.append_topics(led, "2026-07-13", ["C"])         # today -> in window
            recent = t.recent_titles(led, days=14, today="2026-07-13")
            self.assertIn("C", recent)
            self.assertNotIn("A", recent)
    def test_window_far_past(self):
        with tempfile.TemporaryDirectory() as td:
            led = str(Path(td) / "s.jsonl")
            t.append_topics(led, "2026-06-01", ["old"])
            t.append_topics(led, "2026-07-13", ["new"])
            self.assertEqual(t.recent_titles(led, days=14, today="2026-07-13"), ["new"])
    def test_window_exact_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            led = str(Path(td) / "b.jsonl")
            t.append_topics(led, "2026-06-30", ["exactly14"])  # exactly 14 days old -> EXCLUDED (< days)
            t.append_topics(led, "2026-07-01", ["thirteen"])   # 13 days old -> INCLUDED
            recent = t.recent_titles(led, days=14, today="2026-07-14")
            self.assertIn("thirteen", recent)
            self.assertNotIn("exactly14", recent)
    def test_missing_file_is_empty(self):
        self.assertEqual(t.recent_titles("/no/such.jsonl", 14, "2026-07-13"), [])

if __name__ == "__main__":
    unittest.main()

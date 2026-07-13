import sys, json, unittest, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import dispatch_lib as d

class DispatchLib(unittest.TestCase):
    def test_slugs_cover_three_types(self):
        self.assertEqual(set(d.SLUGS), {"news", "strategy", "japanese"})
        self.assertTrue(d.SLUGS["strategy"]["self"])
        self.assertEqual(d.SLUGS["news"]["type"], "AI新聞報")

    def test_parse_name(self):
        self.assertEqual(d.parse_newsletter_name("newsletters/2026-07-13-news.html"),
                         ("2026-07-13", "news"))
        self.assertEqual(d.parse_newsletter_name("newsletters/2026-07-13-japanese.html"),
                         ("2026-07-13", "japanese"))

    def test_parse_name_rejects_unknown_slug(self):
        with self.assertRaises(ValueError):
            d.parse_newsletter_name("newsletters/2026-07-13-weekly.html")

    def test_changed_html_filters(self):
        lines = ["newsletters/2026-07-13-news.html",
                 "newsletters/2026-07-13-news.json",
                 "topics/news.jsonl",
                 "newsletters/2026-07-13-japanese.html"]
        self.assertEqual(d.changed_html(lines),
                         ["newsletters/2026-07-13-news.html",
                          "newsletters/2026-07-13-japanese.html"])

    def test_sent_marker_path(self):
        self.assertEqual(d.sent_marker_path("2026-07-13", "news"),
                         "sent/2026-07-13-news.ok")

    def test_load_meta_ok_and_missing_key(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.json"
            p.write_text(json.dumps({"subject": "S", "type": "AI新聞報",
                                     "recipients": "sheet"}), encoding="utf-8")
            self.assertEqual(d.load_meta(str(p))["subject"], "S")
            p.write_text(json.dumps({"subject": "S"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                d.load_meta(str(p))

if __name__ == "__main__":
    unittest.main()

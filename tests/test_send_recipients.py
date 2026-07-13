import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gsuite_https as g

class ParseToOverride(unittest.TestCase):
    def test_parse_to_single(self):
        self.assertEqual(g.parse_to_override("darren@example.com"),
                         [("", "darren@example.com")])
    def test_parse_to_multi_and_names(self):
        self.assertEqual(
            g.parse_to_override("A:a@x.com, b@y.com"),
            [("A", "a@x.com"), ("", "b@y.com")])
    def test_parse_to_empty(self):
        self.assertEqual(g.parse_to_override(""), [])
    def test_parse_to_dedup_and_invalid(self):
        # invalid dropped, duplicate email deduped (case-insensitive)
        self.assertEqual(
            g.parse_to_override("a@x.com, A@X.com, notanemail"),
            [("", "a@x.com")])

if __name__ == "__main__":
    unittest.main()

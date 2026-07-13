import argparse, os, sys, tempfile, unittest
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

    def test_parse_to_all_invalid_yields_empty(self):
        # A malformed --to value (e.g. a typo'd STRATEGY_RECIPIENT) with NO valid
        # emails at all must yield [] — this is what triggers the cmd_send guard
        # in gsuite_https (FIX 3): a non-empty --to that parses to no recipients
        # must fail loudly instead of falling through to the sheet.
        self.assertEqual(g.parse_to_override("notanemail, alsobad"), [])


class CmdSendInvalidToGuard(unittest.TestCase):
    """FIX 3: a non-empty but all-invalid --to must die() BEFORE falling through
    to the sheet, not silently proceed. die() runs before any network call (it's
    checked before get_access_token()), so this is safely testable without mocks."""

    def test_invalid_to_dies_before_sheet_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            html = Path(td) / "x.html"
            html.write_text("<html></html>", encoding="utf-8")
            os.environ["GMAIL_SENDER"] = "sender@example.com"
            self.addCleanup(lambda: os.environ.pop("GMAIL_SENDER", None))
            args = argparse.Namespace(html=str(html), subject="S", type="策略學習報",
                                       no_sync=True, dry_run=False, to="notanemail")
            with self.assertRaises(SystemExit) as cm:
                g.cmd_send(args)
            self.assertEqual(cm.exception.code, 1)

if __name__ == "__main__":
    unittest.main()

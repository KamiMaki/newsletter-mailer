import sys, json, unittest, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import dispatch

class DispatchOrchestration(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.exit_code = 0
        dispatch.run_cmd = lambda argv: (self.calls.append(argv) or self.exit_code)

    def _repo(self, td, slug="news", meta=None, with_md=False):
        (Path(td) / "newsletters").mkdir()
        (Path(td) / "sent").mkdir()
        html = Path(td) / f"newsletters/2026-07-13-{slug}.html"
        html.write_text("<html></html>", encoding="utf-8")
        meta = meta or {"subject": "S", "type": dispatch.dispatch_lib.SLUGS[slug]["type"],
                        "recipients": "self" if slug == "strategy" else "sheet"}
        (Path(td) / f"newsletters/2026-07-13-{slug}.json").write_text(
            json.dumps(meta), encoding="utf-8")
        if with_md:
            (Path(td) / "notion").mkdir(exist_ok=True)
            (Path(td) / "notion/2026-07-13-{}.md".format(slug)).write_text("## h", encoding="utf-8")
            (Path(td) / "notion/2026-07-13-{}.meta.json".format(slug)).write_text(
                json.dumps({"type": meta["type"], "date": "2026-07-13"}), encoding="utf-8")
        return html

    def test_sends_changed_and_writes_marker(self):
        with tempfile.TemporaryDirectory() as td:
            self._repo(td)
            df = Path(td) / "diff.txt"
            df.write_text("newsletters/2026-07-13-news.html\n", encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "sent/2026-07-13-news.ok").exists())
            self.assertTrue(any(a[:2] == ["send", "--html"] or "send" in a for a in self.calls))

    def test_skips_when_marker_exists(self):
        with tempfile.TemporaryDirectory() as td:
            self._repo(td)
            (Path(td) / "sent/2026-07-13-news.ok").write_text("x", encoding="utf-8")
            df = Path(td) / "diff.txt"
            df.write_text("newsletters/2026-07-13-news.html\n", encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td])
            self.assertEqual(rc, 0)
            self.assertEqual(self.calls, [])  # nothing sent

    def test_strategy_uses_to_override(self):
        self.addCleanup(lambda: os.environ.pop("STRATEGY_RECIPIENT", None))
        os.environ["STRATEGY_RECIPIENT"] = "me@example.com"
        with tempfile.TemporaryDirectory() as td:
            self._repo(td, slug="strategy")
            df = Path(td) / "diff.txt"
            df.write_text("newsletters/2026-07-13-strategy.html\n", encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td])
            self.assertEqual(rc, 0)
            send = [a for a in self.calls if "send" in a][0]
            self.assertIn("--to", send)
            self.assertIn("me@example.com", send)
            self.assertIn("--no-sync", send)

    def test_send_failure_propagates_and_no_marker(self):
        self.exit_code = 1
        with tempfile.TemporaryDirectory() as td:
            self._repo(td)
            df = Path(td) / "diff.txt"
            df.write_text("newsletters/2026-07-13-news.html\n", encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td])
            self.assertEqual(rc, 1)
            self.assertFalse((Path(td) / "sent/2026-07-13-news.ok").exists())

    def test_unknown_slug_file_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            # good target
            self._repo(td, slug="news")
            # bad target: unparseable / unknown-slug file that changed_html would still
            # match structurally but parse_newsletter_name rejects (unknown slug)
            (Path(td) / "newsletters/2026-07-13-weekly.html").write_text(
                "<html></html>", encoding="utf-8")
            df = Path(td) / "diff.txt"
            df.write_text(
                "newsletters/2026-07-13-weekly.html\n"
                "newsletters/2026-07-13-news.html\n",
                encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "sent/2026-07-13-news.ok").exists())
            # only the good newsletter was sent; bad one produced no send call
            self.assertTrue(any("send" in a for a in self.calls))
            sent_htmls = [a for call in self.calls for a in call if str(a).endswith(".html")]
            self.assertTrue(all("weekly" not in s for s in sent_htmls))

    def test_notion_failure_is_nonfatal(self):
        # send succeeds (rc 0), notion-add fails (rc 1). Marker must still be written; run rc 0.
        dispatch.run_cmd = lambda argv: (self.calls.append(argv) or (1 if "notion-add" in argv else 0))
        with tempfile.TemporaryDirectory() as td:
            self._repo(td, slug="news", with_md=True)
            df = Path(td) / "diff.txt"
            df.write_text("newsletters/2026-07-13-news.html\n", encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "sent/2026-07-13-news.ok").exists())
            self.assertTrue(any("notion-add" in a for a in self.calls))

    def test_dry_run_writes_no_marker(self):
        with tempfile.TemporaryDirectory() as td:
            self._repo(td)
            df = Path(td) / "diff.txt"
            df.write_text("newsletters/2026-07-13-news.html\n", encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td, "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertFalse((Path(td) / "sent/2026-07-13-news.ok").exists())
            send = [a for a in self.calls if "send" in a][0]
            self.assertIn("--dry-run", send)

    def test_strategy_missing_recipient_fails(self):
        self.addCleanup(lambda: os.environ.pop("STRATEGY_RECIPIENT", None))
        os.environ.pop("STRATEGY_RECIPIENT", None)
        with tempfile.TemporaryDirectory() as td:
            self._repo(td, slug="strategy")
            df = Path(td) / "diff.txt"
            df.write_text("newsletters/2026-07-13-strategy.html\n", encoding="utf-8")
            rc = dispatch.main(["--diff-file", str(df), "--repo", td])
            self.assertEqual(rc, 1)
            self.assertFalse((Path(td) / "sent/2026-07-13-strategy.ok").exists())
            # no send attempted for the misconfigured strategy target
            self.assertFalse(any("send" in a for a in self.calls))

    def test_script_invocation_smoke(self):
        # Reproduce CI's direct invocation (`python scripts/dispatch.py ...`), which puts
        # scripts/ on sys.path[0] not the repo root. Without the sys.path fix this exits 1
        # with ModuleNotFoundError: No module named 'scripts'. Empty diff => no send attempted,
        # so no network/secrets are touched and run_cmd is NOT monkeypatched here.
        import subprocess
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as td:
            empty_diff = Path(td) / "diff.txt"
            empty_diff.write_text("", encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "scripts/dispatch.py",
                 "--diff-file", str(empty_diff), "--repo", "."],
                cwd=repo_root, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("nothing to send", r.stdout)

if __name__ == "__main__":
    unittest.main()

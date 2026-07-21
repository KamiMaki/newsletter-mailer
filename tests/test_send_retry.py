import argparse, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gsuite_https as g

R3 = [("A", "a@x.com"), ("B", "b@y.com"), ("C", "c@z.com")]


def fake_http_factory(fail_plan):
    """fail_plan: {email: number of times to fail before succeeding (or 999 = always)}.
    Returns (fake_http, calls) where calls records the To email of each send attempt."""
    counts = {}
    calls = []

    def fake_http(method, url, *, token=None, json_body=None, form_data=None, extra_headers=None):
        import base64
        raw = base64.urlsafe_b64decode(json_body["raw"].encode())
        for name, email in R3:
            if email.encode() in raw:
                break
        calls.append(email)
        n = counts.get(email, 0)
        counts[email] = n + 1
        if n < fail_plan.get(email, 0):
            return 400, {"error": {"status": "FAILED_PRECONDITION"}}
        return 200, {"id": f"msg-{email}-{n}"}

    return fake_http, calls


class SendToRecipients(unittest.TestCase):
    def _run(self, fail_plan, delays=(1, 2)):
        fake_http, calls = fake_http_factory(fail_plan)
        sleeps = []
        with mock.patch.object(g, "http", fake_http):
            ok, skipped = g.send_to_recipients(
                "tok", "s@x.com", "Sender", "Subj", "<html></html>", R3,
                retry_delays=delays, sleep=sleeps.append)
        return ok, skipped, calls, sleeps

    def test_all_ok_first_pass_no_retry(self):
        ok, skipped, calls, sleeps = self._run({})
        self.assertEqual(ok, 3)
        self.assertEqual(skipped, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [])

    def test_transient_failure_recovers_on_retry(self):
        # b@y.com fails once, succeeds on the first retry pass
        ok, skipped, calls, sleeps = self._run({"b@y.com": 1})
        self.assertEqual(ok, 3)
        self.assertEqual(skipped, [])
        # 3 first-pass attempts + 1 retry, only for the failed recipient
        self.assertEqual(calls.count("b@y.com"), 2)
        self.assertEqual(calls.count("a@x.com"), 1)
        self.assertEqual(sleeps, [1])

    def test_persistent_failure_skipped_after_all_retries(self):
        ok, skipped, calls, sleeps = self._run({"c@z.com": 999})
        self.assertEqual(ok, 2)
        self.assertEqual(len(skipped), 1)
        name, email, err = skipped[0]
        self.assertEqual((name, email), ("C", "c@z.com"))
        self.assertIn("FAILED_PRECONDITION", err)
        # initial pass + one attempt per retry delay
        self.assertEqual(calls.count("c@z.com"), 3)
        self.assertEqual(sleeps, [1, 2])


class CmdSendExitCodes(unittest.TestCase):
    """Partial failure (after retries) must exit 0 so dispatch.py writes the sent
    marker (a re-run would double-send to everyone who already got it). Only a
    total failure (zero delivered) exits 1."""

    def _cmd_send(self, fail_plan):
        fake_http, _calls = fake_http_factory(fail_plan)
        with tempfile.TemporaryDirectory() as td:
            html = Path(td) / "x.html"
            html.write_text("<html></html>", encoding="utf-8")
            os.environ["GMAIL_SENDER"] = "s@x.com"
            self.addCleanup(lambda: os.environ.pop("GMAIL_SENDER", None))
            args = argparse.Namespace(
                html=str(html), subject="Subj", type="AI新聞報", no_sync=True,
                dry_run=False, to="A:a@x.com,B:b@y.com,C:c@z.com")
            with mock.patch.object(g, "http", fake_http), \
                 mock.patch.object(g, "get_access_token", lambda: "tok"), \
                 mock.patch.object(g, "SEND_RETRY_DELAYS", (0,)), \
                 mock.patch.object(g.time, "sleep", lambda _s: None):
                with self.assertRaises(SystemExit) as cm:
                    g.cmd_send(args)
        return cm.exception.code

    def test_partial_failure_exits_zero(self):
        self.assertEqual(self._cmd_send({"c@z.com": 999}), 0)

    def test_total_failure_exits_one(self):
        self.assertEqual(
            self._cmd_send({e: 999 for _n, e in R3}), 1)

    def test_all_ok_exits_zero(self):
        self.assertEqual(self._cmd_send({}), 0)


if __name__ == "__main__":
    unittest.main()

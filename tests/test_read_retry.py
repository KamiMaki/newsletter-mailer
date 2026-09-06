"""Retry of transient Google API errors when reading the subscriber sheet.

Background: a real run hit HTTP 503 UNAVAILABLE on `訂閱者!A2:F`, fell back to
"send to the sender only", and then wrote the sent marker — so nobody got that
issue and a re-run would not resend it. These tests pin down:
  * http_retry retries only transient statuses, with the configured delays;
  * sheet_get succeeds when the 503 clears within the retry budget;
  * cmd_send fails loudly (exit 1, no mail sent) when the sheet stays unreadable
    and there is no FALLBACK_RECIPIENTS — never a silent self-send;
  * an EMPTY sheet (readable, zero active subscribers) keeps the old self-send.
"""
import argparse, base64, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gsuite_https as g

SUBS_ROWS = [
    ["2026-01-01 00:00:00", "a@x.com", "A", "日文報", "Google表單", "active"],
    ["2026-01-01 00:00:00", "b@y.com", "B", "AI新聞報、日文報", "Google表單", "active"],
]


def status_sequence(seq):
    """Return (fake_http, calls) that answers the i-th call with seq[i]
    ((status, payload)); the last entry repeats if called more often."""
    calls = []

    def fake_http(method, url, **kw):
        calls.append((method, url))
        s, p = seq[min(len(calls) - 1, len(seq) - 1)]
        return s, p

    return fake_http, calls


class HttpRetry(unittest.TestCase):
    def test_503_then_200_retries_and_returns_success(self):
        fake, calls = status_sequence([(503, {"error": "unavailable"}), (200, {"values": []})])
        sleeps = []
        with mock.patch.object(g, "http", fake):
            status, p = g.http_retry("GET", "u", retry_delays=(1, 2, 3), sleep=sleeps.append)
        self.assertEqual(status, 200)
        self.assertEqual(p, {"values": []})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [1])          # slept once, before the successful retry

    def test_persistent_503_gives_up_after_all_delays(self):
        fake, calls = status_sequence([(503, {"error": "unavailable"})])
        sleeps = []
        with mock.patch.object(g, "http", fake):
            status, _p = g.http_retry("GET", "u", retry_delays=(1, 2, 3), sleep=sleeps.append)
        self.assertEqual(status, 503)
        self.assertEqual(len(calls), 4)        # initial + one per delay
        self.assertEqual(sleeps, [1, 2, 3])

    def test_non_transient_error_is_not_retried(self):
        for code in (400, 401, 403, 404):
            fake, calls = status_sequence([(code, {"error": "nope"})])
            sleeps = []
            with mock.patch.object(g, "http", fake):
                status, _p = g.http_retry("GET", "u", retry_delays=(1, 2), sleep=sleeps.append)
            self.assertEqual(status, code)
            self.assertEqual(len(calls), 1, code)
            self.assertEqual(sleeps, [], code)

    def test_network_error_status_zero_and_429_are_transient(self):
        for code in (0, 429, 500, 502, 504):
            fake, calls = status_sequence([(code, {"error": "x"}), (200, {})])
            sleeps = []
            with mock.patch.object(g, "http", fake):
                status, _p = g.http_retry("GET", "u", retry_delays=(1,), sleep=sleeps.append)
            self.assertEqual(status, 200, code)
            self.assertEqual(len(calls), 2, code)

    def test_default_delays_used_when_not_given(self):
        fake, calls = status_sequence([(503, {})])
        sleeps = []
        with mock.patch.object(g, "http", fake), \
             mock.patch.object(g, "READ_RETRY_DELAYS", (7, 8)):
            g.http_retry("GET", "u", sleep=sleeps.append)
        self.assertEqual(sleeps, [7, 8])
        self.assertEqual(len(calls), 3)

    def test_kwargs_forwarded_to_http(self):
        seen = {}

        def fake_http(method, url, **kw):
            seen.update(kw)
            return 200, {}

        with mock.patch.object(g, "http", fake_http):
            g.http_retry("POST", "u", token="tok", json_body={"a": 1})
        self.assertEqual(seen, {"token": "tok", "json_body": {"a": 1}})


class SheetGetRetry(unittest.TestCase):
    def setUp(self):
        os.environ["NEWSLETTER_SPREADSHEET_ID"] = "sid"
        self.addCleanup(lambda: os.environ.pop("NEWSLETTER_SPREADSHEET_ID", None))

    def test_sheet_get_recovers_from_503(self):
        fake, calls = status_sequence([(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}),
                                       (200, {"values": SUBS_ROWS})])
        with mock.patch.object(g, "http", fake), \
             mock.patch.object(g, "READ_RETRY_DELAYS", (0,)), \
             mock.patch.object(g.time, "sleep", lambda _s: None):
            rows = g.sheet_get("tok", "訂閱者!A2:F")
        self.assertEqual(rows, SUBS_ROWS)
        self.assertEqual(len(calls), 2)

    def test_sheet_get_raises_after_exhausting_retries(self):
        fake, calls = status_sequence([(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})])
        with mock.patch.object(g, "http", fake), \
             mock.patch.object(g, "READ_RETRY_DELAYS", (0, 0)), \
             mock.patch.object(g.time, "sleep", lambda _s: None):
            with self.assertRaises(RuntimeError) as cm:
                g.sheet_get("tok", "訂閱者!A2:F")
        self.assertIn("HTTP 503", str(cm.exception))
        self.assertEqual(len(calls), 3)


class CmdSendSheetFailure(unittest.TestCase):
    """End-to-end through cmd_send with http routed by URL: Sheets reads follow a
    scripted status sequence, Gmail sends always succeed and are recorded."""

    def setUp(self):
        os.environ["NEWSLETTER_SPREADSHEET_ID"] = "sid"
        os.environ["GMAIL_SENDER"] = "s@x.com"
        os.environ.pop("FALLBACK_RECIPIENTS", None)
        for k in ("NEWSLETTER_SPREADSHEET_ID", "GMAIL_SENDER", "FALLBACK_RECIPIENTS"):
            self.addCleanup(os.environ.pop, k, None)

    def _run(self, sheet_seq):
        sheet_calls, sent_to = [], []
        seq_i = {"n": 0}

        def fake_http(method, url, *, token=None, json_body=None, form_data=None, extra_headers=None):
            if url.startswith(g.SHEETS_URL):
                sheet_calls.append(url)
                s, p = sheet_seq[min(seq_i["n"], len(sheet_seq) - 1)]
                seq_i["n"] += 1
                return s, p
            if url == g.GMAIL_SEND_URL:
                raw = base64.urlsafe_b64decode(json_body["raw"].encode()).decode("utf-8", "replace")
                to_line = next(l for l in raw.splitlines() if l.startswith("To:"))
                sent_to.append(to_line[3:].strip())
                return 200, {"id": f"msg-{len(sent_to)}"}
            raise AssertionError(f"unexpected call {method} {url}")

        with tempfile.TemporaryDirectory() as td:
            html = Path(td) / "x.html"
            html.write_text("<html></html>", encoding="utf-8")
            args = argparse.Namespace(html=str(html), subject="Subj", type="日文報",
                                      no_sync=True, dry_run=False, to="")
            with mock.patch.object(g, "http", fake_http), \
                 mock.patch.object(g, "get_access_token", lambda: "tok"), \
                 mock.patch.object(g, "READ_RETRY_DELAYS", (0, 0, 0)), \
                 mock.patch.object(g, "SEND_RETRY_DELAYS", (0,)), \
                 mock.patch.object(g.time, "sleep", lambda _s: None):
                with self.assertRaises(SystemExit) as cm:
                    g.cmd_send(args)
        return cm.exception.code, sheet_calls, sent_to

    def test_503_clears_on_retry_sends_to_subscribers(self):
        code, sheet_calls, sent_to = self._run([
            (503, {"error": {"code": 503, "status": "UNAVAILABLE"}}),
            (200, {"values": SUBS_ROWS}),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(sheet_calls), 2)
        self.assertEqual(sent_to, ["A <a@x.com>", "B <b@y.com>"])

    def test_persistent_503_without_fallback_exits_1_and_sends_nothing(self):
        # The regression: previously this sent ONLY to the sender and exited 0,
        # so dispatch wrote the marker and the issue was silently lost.
        code, sheet_calls, sent_to = self._run([
            (503, {"error": {"code": 503, "status": "UNAVAILABLE"}}),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(len(sheet_calls), 4)          # initial + 3 retries
        self.assertEqual(sent_to, [])

    def test_persistent_503_with_fallback_uses_fallback_list(self):
        os.environ["FALLBACK_RECIPIENTS"] = "F:f@z.com"
        code, _sheet_calls, sent_to = self._run([
            (503, {"error": {"code": 503, "status": "UNAVAILABLE"}}),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(sent_to, ["F <f@z.com>"])

    def test_readable_but_empty_sheet_still_sends_to_sender_only(self):
        # Zero active subscribers is NOT a read failure: keep the old behaviour.
        code, sheet_calls, sent_to = self._run([(200, {"values": []})])
        self.assertEqual(code, 0)
        self.assertEqual(len(sheet_calls), 1)
        self.assertEqual(sent_to, ["s@x.com"])

    def test_non_transient_read_error_is_not_retried_and_exits_1(self):
        code, sheet_calls, sent_to = self._run([(403, {"error": "forbidden"})])
        self.assertEqual(code, 1)
        self.assertEqual(len(sheet_calls), 1)
        self.assertEqual(sent_to, [])


if __name__ == "__main__":
    unittest.main()

"""Orchestrate sending of newsletters changed in the latest push.

Usage: python scripts/dispatch.py --diff-file <git-diff-names> [--dry-run] [--repo <root>]
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Make the `scripts` package importable regardless of launch style. Direct invocation
# (`python scripts/dispatch.py`) puts scripts/ on sys.path[0], not the repo root, so
# `from scripts import dispatch_lib` would fail with ModuleNotFoundError. Prepend repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import dispatch_lib

ENGINE = "gsuite_https.py"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# NOTE (at-least-once delivery): a send that partially succeeds (some recipients
# fail) writes NO marker for that newsletter — see _dispatch_one below. A re-run
# will therefore re-send to ALL recipients of that newsletter, including the ones
# that already received it. This is intentional (favors no-drop over no-duplicate)
# but operators re-running a failed job should know duplicates are possible.

def run_cmd(argv):
    """Run `python gsuite_https.py <argv...>`; return exit code. (Monkeypatched in tests.)"""
    return subprocess.call([sys.executable, ENGINE] + argv)

def _build_send_argv(repo, html_rel, meta, slug, dry_run):
    argv = ["send", "--html", str(Path(repo) / html_rel),
            "--subject", meta["subject"], "--type", meta["type"]]
    if dispatch_lib.SLUGS[slug].get("self") or meta.get("recipients") == "self":
        to = os.environ.get("STRATEGY_RECIPIENT", "")
        argv += ["--to", to, "--no-sync"]
    if dry_run:
        argv.append("--dry-run")
    return argv

def _notion_argv(repo, date, slug, dry_run):
    md = Path(repo) / "notion" / f"{date}-{slug}.md"
    meta = Path(repo) / "notion" / f"{date}-{slug}.meta.json"
    if not md.exists() or not meta.exists():
        return None
    argv = ["notion-add", "--md", str(md), "--meta", str(meta)]
    if dry_run:
        argv.append("--dry-run")
    return argv

def _dispatch_one(repo, date, slug, html_rel, args):
    """Send + notion-add + mark one changed newsletter. Returns True on success, False on failure.

    `date`/`slug` must already be validated by the caller (parse_newsletter_name).
    A known-slug target whose metadata is missing/malformed is a real failure
    (FAIL, no marker) — NOT a silent skip — so a broken newsletter can't slip
    through with a green run.
    """
    marker = Path(repo) / dispatch_lib.sent_marker_path(date, slug)
    if marker.exists():
        print(f"[skip] already sent: {date}-{slug}")
        return True
    try:
        meta = dispatch_lib.load_meta(str(Path(repo) / html_rel).replace(".html", ".json"))
    except (ValueError, OSError) as e:
        print(f"[FAIL] bad or missing metadata for {date}-{slug}: {e}")
        return False

    # Strategy self-send guard: strategy goes ONLY to STRATEGY_RECIPIENT. If it is empty
    # OR not a plausible email address (e.g. a typo), sending with `--to "<garbage>"
    # --no-sync` would make parse_to_override return [] and gsuite_https fall through to
    # the sheet's 策略學習報 subscribers — a real leak. Fail loudly (no send, no marker) so
    # the misconfiguration is visible; do NOT silently skip.
    is_self = dispatch_lib.SLUGS[slug].get("self") or meta.get("recipients") == "self"
    if is_self:
        recipient = os.environ.get("STRATEGY_RECIPIENT", "").strip()
        if not recipient or not _EMAIL_RE.match(recipient):
            print(f"[FAIL] strategy self-send requires a valid STRATEGY_RECIPIENT "
                  f"(got {recipient!r}): {date}-{slug}")
            return False

    rc = run_cmd(_build_send_argv(repo, html_rel, meta, slug, args.dry_run))
    if rc != 0:
        print(f"[FAIL] send {date}-{slug} exit {rc}")
        return False

    n_argv = _notion_argv(repo, date, slug, args.dry_run)
    if n_argv is not None:
        nrc = run_cmd(n_argv)
        if nrc != 0:
            print(f"[warn] notion-add {date}-{slug} exit {nrc} (non-fatal)")

    if not args.dry_run:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"sent {date}-{slug}\n", encoding="utf-8")
        print(f"[OK] sent + marked {date}-{slug}")
    return True

def main(cli_args):
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff-file", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(cli_args)

    repo = args.repo
    diff_lines = Path(args.diff_file).read_text(encoding="utf-8").splitlines()
    targets = dispatch_lib.changed_html(diff_lines)
    if not targets:
        print("no changed newsletters — nothing to send")
        return 0

    failed = 0
    for html_rel in targets:
        try:
            date, slug = dispatch_lib.parse_newsletter_name(html_rel)
        except ValueError as e:
            # Un-parseable path or unknown slug: skip this target with a warning,
            # do NOT abort the whole run (other good newsletters must still send).
            # A KNOWN slug with broken metadata is handled separately below and
            # DOES count as a failure — see _dispatch_one.
            print(f"[skip] unparseable: {html_rel} ({e})")
            continue
        ok = _dispatch_one(repo, date, slug, html_rel, args)
        if not ok:
            failed += 1

    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

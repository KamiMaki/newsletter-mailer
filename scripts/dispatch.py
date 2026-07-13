"""Orchestrate sending of newsletters changed in the latest push.

Usage: python scripts/dispatch.py --diff-file <git-diff-names> [--dry-run] [--repo <root>]
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from scripts import dispatch_lib

ENGINE = "gsuite_https.py"

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

def _dispatch_one(repo, html_rel, args):
    """Send + notion-add + mark one changed newsletter. Returns True on success, False on send failure."""
    date, slug = dispatch_lib.parse_newsletter_name(html_rel)
    marker = Path(repo) / dispatch_lib.sent_marker_path(date, slug)
    if marker.exists():
        print(f"[skip] already sent: {date}-{slug}")
        return True
    meta = dispatch_lib.load_meta(str(Path(repo) / html_rel).replace(".html", ".json"))

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
            ok = _dispatch_one(repo, html_rel, args)
        except ValueError as e:
            # Un-parseable path or unknown slug: skip this target with a warning,
            # do NOT abort the whole run (other good newsletters must still send).
            print(f"[skip] unparseable: {html_rel} ({e})")
            continue
        if not ok:
            failed += 1

    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

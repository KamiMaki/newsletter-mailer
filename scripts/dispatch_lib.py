"""Pure helpers for the newsletter send dispatcher. No network, no side effects."""
import json
import re
from pathlib import Path

SLUGS = {
    "news":     {"type": "AI新聞報",   "dedup_days": 14},
    "strategy": {"type": "策略學習報", "dedup_days": 28, "self": True},
    "japanese": {"type": "日文報",     "dedup_days": 30},
}
_NAME_RE = re.compile(r"^newsletters/(\d{4}-\d{2}-\d{2})-([a-z]+)\.html$")
_META_KEYS = ("subject", "type", "recipients")

def parse_newsletter_name(path):
    """'newsletters/2026-07-13-news.html' -> ('2026-07-13','news'). Unknown slug → ValueError."""
    m = _NAME_RE.match(path.replace("\\", "/"))
    if not m:
        raise ValueError(f"bad newsletter path: {path}")
    date, slug = m.group(1), m.group(2)
    if slug not in SLUGS:
        raise ValueError(f"unknown slug: {slug}")
    return date, slug

def changed_html(diff_lines):
    """Keep only newsletters/*.html paths from a git-diff name list."""
    return [ln.strip().replace("\\", "/") for ln in diff_lines
            if _NAME_RE.match(ln.strip().replace("\\", "/"))]

def sent_marker_path(date, slug):
    return f"sent/{date}-{slug}.ok"

def load_meta(meta_path):
    """Read + validate the newsletter metadata sidecar JSON."""
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    missing = [k for k in _META_KEYS if k not in meta]
    if missing:
        raise ValueError(f"metadata missing keys {missing} in {meta_path}")
    return meta

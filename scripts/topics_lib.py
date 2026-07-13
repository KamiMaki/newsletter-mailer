"""Repo-based topic dedup ledger (JSONL). No network — cloud reads/writes locally then pushes."""
import json
from datetime import date
from pathlib import Path

def _parse(d):
    y, m, dd = (int(x) for x in d.split("-"))
    return date(y, m, dd)

def recent_titles(ledger_path, days, today):
    """Titles logged within `days` before/at `today` (YYYY-MM-DD)."""
    p = Path(ledger_path)
    if not p.exists():
        return []
    today_d = _parse(today)
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if (today_d - _parse(rec["date"])).days < days:
            out.append(rec["title"])
    return out

def append_topics(ledger_path, date, titles):
    p = Path(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for title in titles:
            f.write(json.dumps({"date": date, "title": title}, ensure_ascii=False) + "\n")

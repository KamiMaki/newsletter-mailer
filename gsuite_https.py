#!/usr/bin/env python3
"""
AI電子報 — Google Sheets / Forms / Gmail 整合工具（純 HTTPS、零外部相依）

設計給「雲端沙盒」與本機共用：所有 Google API 都走 HTTPS（port 443），
只用 Python 標準庫（urllib），沙盒零安裝可跑。

試算表「AI電子報管理中心」有兩個工作表：
  訂閱者      ：訂閱時間 | Email | 姓名 | 訂閱項目 | 來源 | 狀態
  已寄送主題  ：日期 | 報別 | 主題標題 | 備註
報別短鍵：AI新聞報 / 策略學習報 / 日文報
退訂：另有退訂表單（NEWSLETTER_UNSUB_FORM_ID），sync 時與訂閱回應依時間合併處理

子命令：
  recent-topics --type 報別 --days N        列出近 N 天已寄送主題（跨天去重用）
  log-topics --type 報別 --date D --topics-file F   記錄本期主題（F 內一行一則，可用 TAB 加備註）
  sync-subs                                  Google 表單回應 → 同步進「訂閱者」工作表
  list-subs --type 報別                      列出該報別 active 訂閱者
  send --html F --subject S --type 報別 [--no-sync] [--dry-run]
                                             同步表單訂閱者後逐一寄送（失敗退備援名單）
  notion-add --md F --meta J [--dry-run]     把當期內容（Markdown）寫進 Notion 彙整資料庫
                                             報別→資料庫由 .env NOTION_TOKEN / NOTION_*_DB_ID 決定

設定值來源優先序：環境變數 > 腳本同目錄 .env
  GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN（需含 spreadsheets+forms scope）
  GMAIL_SENDER / NEWSLETTER_SENDER_NAME
  NEWSLETTER_SPREADSHEET_ID / NEWSLETTER_FORM_ID
  FALLBACK_RECIPIENTS（"姓名:email,姓名:email"，試算表讀不到時的備援名單）
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
HTTP_TIMEOUT = 30

# Windows 主控台/管線預設 cp950，印 emoji 或特殊符號會 UnicodeEncodeError；一律改 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
FORMS_URL = "https://forms.googleapis.com/v1/forms"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

SUBS_TAB = "訂閱者"
TOPICS_TAB = "已寄送主題"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ──────────────────────────── 設定載入 ────────────────────────────

_ENV_FILE_CACHE = None


def _load_env_file():
    """讀腳本同目錄的 .env（KEY=VALUE，# 開頭為註解）。"""
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def cfg(key, default=""):
    """環境變數優先，其次 .env。"""
    global _ENV_FILE_CACHE
    if os.environ.get(key):
        return os.environ[key]
    if _ENV_FILE_CACHE is None:
        _ENV_FILE_CACHE = _load_env_file()
    return _ENV_FILE_CACHE.get(key, default)


def die(msg):
    print(f"錯誤：{msg}")
    sys.exit(1)


# ──────────────────────────── HTTP 層 ────────────────────────────

def http(method, url, *, token=None, json_body=None, form_data=None, extra_headers=None):
    """送出 HTTPS 請求，回傳 (status, parsed_json_or_dict)。網路層錯誤回 (0, {"error": ...})。"""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form_data is not None:
        body = urllib.parse.urlencode(form_data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            return r.status, (json.loads(text) if text.strip() else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"error": text}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def get_access_token():
    """以 refresh token 換 access token。"""
    cid = cfg("GMAIL_CLIENT_ID")
    csec = cfg("GMAIL_CLIENT_SECRET")
    rtok = cfg("GMAIL_REFRESH_TOKEN")
    missing = [k for k, v in (("GMAIL_CLIENT_ID", cid),
                              ("GMAIL_CLIENT_SECRET", csec),
                              ("GMAIL_REFRESH_TOKEN", rtok)) if not v]
    if missing:
        die(f"缺少憑證：{', '.join(missing)}（請設定環境變數或 .env）")
    status, payload = http("POST", TOKEN_URL, form_data={
        "client_id": cid, "client_secret": csec,
        "refresh_token": rtok, "grant_type": "refresh_token",
    })
    if status != 200 or "access_token" not in payload:
        die(f"取得 access token 失敗 (HTTP {status})：{payload}\n"
            "  提示：invalid_grant 代表 refresh token 失效或被撤銷，需重跑 setup_google_services.py 重新授權。")
    return payload["access_token"]


# ──────────────────────────── Sheets 基本操作 ────────────────────────────

def _sid():
    sid = cfg("NEWSLETTER_SPREADSHEET_ID")
    if not sid:
        raise RuntimeError("未設定 NEWSLETTER_SPREADSHEET_ID")
    return sid


def sheet_get(token, rng):
    status, p = http("GET", f"{SHEETS_URL}/{_sid()}/values/{urllib.parse.quote(rng)}", token=token)
    if status != 200:
        raise RuntimeError(f"讀取 {rng} 失敗 HTTP {status}: {p}")
    return p.get("values", [])


def sheet_append(token, rng, rows):
    url = (f"{SHEETS_URL}/{_sid()}/values/{urllib.parse.quote(rng)}:append"
           "?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    status, p = http("POST", url, token=token, json_body={"values": rows})
    if status != 200:
        raise RuntimeError(f"寫入 {rng} 失敗 HTTP {status}: {p}")
    return p


def sheet_update(token, rng, rows):
    url = f"{SHEETS_URL}/{_sid()}/values/{urllib.parse.quote(rng)}?valueInputOption=RAW"
    status, p = http("PUT", url, token=token, json_body={"values": rows})
    if status != 200:
        raise RuntimeError(f"更新 {rng} 失敗 HTTP {status}: {p}")
    return p


# ──────────────────────────── 純邏輯（可單元測試） ────────────────────────────

def taipei_today():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def cutoff_date(days):
    return (datetime.now(timezone.utc) + timedelta(hours=8) - timedelta(days=days)).strftime("%Y-%m-%d")


def _filter_topics(rows, typ, cutoff):
    """rows: [[日期, 報別, 主題標題, 備註], ...] → [(日期, 標題)]，依報別與起始日過濾。"""
    out = []
    for r in rows:
        if len(r) < 3 or not r[0] or not r[2]:
            continue
        if r[0] < cutoff:
            continue
        if typ and (len(r) < 2 or r[1] != typ):
            continue
        out.append((r[0], r[2]))
    return out


def _classify_question(title):
    """依表單題目標題判斷欄位用途（訂閱表單與退訂表單共用）。"""
    t = (title or "").lower()
    if "mail" in t or "信箱" in t or "郵件" in t:
        return "email"
    if "姓名" in t or "稱呼" in t or "name" in t:
        return "name"
    if "訂閱" in t or "退訂" in t:
        return "subs"
    return ""


def _normalize_subs(values):
    """把訂閱表單核取方塊選項正規化為「AI新聞報、策略學習報、日文報」短鍵。"""
    joined = " ".join(values or [])
    out = []
    if "新聞" in joined:
        out.append("AI新聞報")
    if "策略" in joined:
        out.append("策略學習報")
    if "日文" in joined or "日語" in joined:
        out.append("日文報")
    return "、".join(out) if out else "AI新聞報"


def _normalize_unsub_targets(values):
    """退訂表單核取方塊 → 要退訂的報別集合；空白或含「全部」→ "ALL"。"""
    joined = " ".join(values or [])
    if not joined.strip() or "全部" in joined:
        return "ALL"
    targets = set()
    if "新聞" in joined:
        targets.add("AI新聞報")
    if "策略" in joined:
        targets.add("策略學習報")
    if "日文" in joined or "日語" in joined:
        targets.add("日文報")
    return targets or "ALL"


def _parse_response(resp, qmap):
    """單筆表單回應 → dict(ts, email, name, subs)；無有效 Email 回 None。"""
    fields = {}
    for qid, ans in (resp.get("answers") or {}).items():
        kind = qmap.get(qid)
        if not kind:
            continue
        vals = [a.get("value", "") for a in ans.get("textAnswers", {}).get("answers", [])]
        fields[kind] = vals
    email = (fields.get("email", [""])[0] or "").strip().lower()
    if not EMAIL_RE.match(email):
        return None
    return {
        "ts": resp.get("lastSubmittedTime", ""),
        "email": email,
        "name": (fields.get("name", [""])[0] or "").strip(),
        "subs": _normalize_subs(fields.get("subs", [])),
    }


def _parse_unsub_response(resp, qmap):
    """退訂表單單筆回應 → dict(ts, email, targets)；無有效 Email 回 None。"""
    fields = {}
    for qid, ans in (resp.get("answers") or {}).items():
        kind = qmap.get(qid)
        if not kind:
            continue
        vals = [a.get("value", "") for a in ans.get("textAnswers", {}).get("answers", [])]
        fields[kind] = vals
    email = (fields.get("email", [""])[0] or "").strip().lower()
    if not EMAIL_RE.match(email):
        return None
    return {
        "ts": resp.get("lastSubmittedTime", ""),
        "email": email,
        "targets": _normalize_unsub_targets(fields.get("subs", [])),
    }


def _merge_events(sub_events, unsub_events):
    """訂閱/退訂事件依 Email 分組、依時間排序 → {email: [(kind, event), ...]}。"""
    by_email = {}
    for e in sub_events:
        by_email.setdefault(e["email"], []).append(("sub", e))
    for e in unsub_events:
        by_email.setdefault(e["email"], []).append(("unsub", e))
    for events in by_email.values():
        events.sort(key=lambda kv: kv[1]["ts"])
    return by_email


def _apply_events(row, events):
    """把（時間早→晚的）事件套用到訂閱者列上，回傳新列；無變更回 None。

    只套用時間「晚於」列上記錄時間的事件——所以舊回應不會覆蓋較新的狀態，
    重跑 sync 也是冪等的。退訂：指定報別→從訂閱項目移除（清空則狀態=退訂）；
    全部退訂→狀態=退訂。之後重新填訂閱表單（更新的時間戳）會恢復。
    """
    cur = list(row) + [""] * (6 - len(row)) if row else None
    changed = False
    for kind, e in events:
        base_ts = cur[0] if cur else ""
        if not e["ts"] or e["ts"] <= (base_ts or ""):
            continue
        if kind == "sub":
            name = e["name"] or (cur[2] if cur else "")
            cur = [e["ts"], e["email"], name, e["subs"], "Google表單", "active"]
        else:
            if cur is None:
                # 從未訂閱卻填了退訂表單：建立退訂列，擋掉更早的訂閱回應
                cur = [e["ts"], e["email"], "", "", "Google表單", "退訂"]
            else:
                items = [x for x in (cur[3] or "").split("、") if x]
                remaining = [] if e["targets"] == "ALL" else [x for x in items if x not in e["targets"]]
                if remaining:
                    cur = [e["ts"], cur[1], cur[2], "、".join(remaining), "Google表單", "active"]
                else:
                    cur = [e["ts"], cur[1], cur[2], cur[3], "Google表單", "退訂"]
        changed = True
    return cur if changed else None


def _pick_subscribers(rows, typ):
    """訂閱者工作表列 → [(姓名, email)]，只取 active 且訂閱項目含 typ 者，依 Email 去重。"""
    seen = set()
    out = []
    for r in rows:
        r = list(r) + [""] * (6 - len(r))
        _ts, email, name, items, _src, status = r[:6]
        email = (email or "").strip()
        if status.strip().lower() != "active":
            continue
        if not EMAIL_RE.match(email.lower()):
            continue
        if typ and typ not in (items or ""):
            continue
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        out.append((name.strip() or email.split("@")[0], email))
    return out


def _parse_fallback(s):
    """FALLBACK_RECIPIENTS "姓名:email,姓名:email"（姓名可省略）→ [(姓名, email)]。"""
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, email = part.rsplit(":", 1)
        else:
            name, email = "", part
        email = email.strip()
        if EMAIL_RE.match(email.lower()):
            out.append((name.strip() or email.split("@")[0], email))
    return out


def parse_to_override(s):
    """'姓名:email, email' → [(name,email)]，去除無效與重複（同 _parse_fallback 規則，但語意為強制收件人）。"""
    out, seen = [], set()
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, email = part.rsplit(":", 1)
            name, email = name.strip(), email.strip()
        else:
            name, email = "", part
        if not EMAIL_RE.match(email.lower()) or email.lower() in seen:
            continue
        seen.add(email.lower())
        out.append((name, email))
    return out


def _parse_topics_file(text):
    """主題檔一行一則：「標題」或「標題<TAB>備註」→ [(標題, 備註)]。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            title, note = line.split("\t", 1)
        else:
            title, note = line, ""
        if title.strip():
            out.append((title.strip(), note.strip()))
    return out


def build_raw(sender, sender_name, to_name, to_email, subject, html_body):
    """組 multipart/alternative（純文字後援 + HTML），回傳 base64url raw。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg.set_content("此電子報為 HTML 格式，請使用支援 HTML 的郵件用戶端檢視。")
    msg.add_alternative(html_body, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ──────────────────────────── 表單同步 ────────────────────────────

def _form_question_map(token, form_id):
    status, p = http("GET", f"{FORMS_URL}/{form_id}", token=token)
    if status != 200:
        raise RuntimeError(f"讀取表單結構失敗 HTTP {status}: {p}")
    qmap = {}
    for item in p.get("items", []):
        q = (item.get("questionItem") or {}).get("question") or {}
        qid = q.get("questionId")
        kind = _classify_question(item.get("title", ""))
        if qid and kind:
            qmap[qid] = kind
    return qmap


def _fetch_form_events(token, form_id, parser):
    """讀取單一表單的全部回應並解析 → (事件清單, 略過無效數)。"""
    qmap = _form_question_map(token, form_id)
    status, p = http("GET", f"{FORMS_URL}/{form_id}/responses", token=token)
    if status != 200:
        raise RuntimeError(f"讀取表單回應失敗 HTTP {status}: {p}")
    events, skipped = [], 0
    for r in p.get("responses", []):
        e = parser(r, qmap)
        if e:
            events.append(e)
        else:
            skipped += 1
    return events, skipped


def sync_subscribers(token):
    """訂閱表單＋退訂表單的回應合併後 upsert 進訂閱者工作表。

    回傳 (新增, 更新, 退訂中, 略過無效)。事件依時間排序套用、只有「晚於」
    列上記錄時間的事件會生效（見 _apply_events），重跑為冪等。
    """
    form_id = cfg("NEWSLETTER_FORM_ID")
    if not form_id:
        raise RuntimeError("未設定 NEWSLETTER_FORM_ID")
    sub_events, skipped = _fetch_form_events(token, form_id, _parse_response)

    unsub_events = []
    unsub_id = cfg("NEWSLETTER_UNSUB_FORM_ID")
    if unsub_id:
        try:
            unsub_events, s2 = _fetch_form_events(token, unsub_id, _parse_unsub_response)
            skipped += s2
        except Exception as e:
            print(f"[警告] 退訂表單讀取失敗（本次只處理訂閱）：{e}")

    rows = sheet_get(token, f"{SUBS_TAB}!A2:F")
    by_email = {}
    for i, r in enumerate(rows):
        email = (r[1] if len(r) > 1 else "").strip().lower()
        if email:
            by_email[email] = (i + 2, list(r) + [""] * (6 - len(r)))

    added = updated = unsubbed = 0
    for email, events in sorted(_merge_events(sub_events, unsub_events).items()):
        rownum, old = by_email.get(email, (None, None))
        new_row = _apply_events(old, events)
        if new_row is None:
            continue
        if rownum:
            sheet_update(token, f"{SUBS_TAB}!A{rownum}:F{rownum}", [new_row])
            updated += 1
        else:
            sheet_append(token, f"{SUBS_TAB}!A:F", [new_row])
            added += 1
        if new_row[5] != "active":
            unsubbed += 1
    return added, updated, unsubbed, skipped


# ──────────────────────────── 子命令 ────────────────────────────

def cmd_recent_topics(args):
    token = get_access_token()
    rows = sheet_get(token, f"{TOPICS_TAB}!A2:D")
    topics = _filter_topics(rows, args.type, cutoff_date(args.days))
    if not topics:
        print(f"（近 {args.days} 天無已寄送主題紀錄）")
        return
    print(f"近 {args.days} 天已寄送主題（{args.type or '全部'}，共 {len(topics)} 則）：")
    for d, t in topics:
        print(f"{d}\t{t}")


def cmd_log_topics(args):
    token = get_access_token()
    text = Path(args.topics_file).read_text(encoding="utf-8")
    topics = _parse_topics_file(text)
    if not topics:
        print("主題檔為空，未記錄任何主題。")
        return
    date = args.date or taipei_today()
    rows = [[date, args.type, title, note] for title, note in topics]
    sheet_append(token, f"{TOPICS_TAB}!A:D", rows)
    print(f"已記錄 {len(rows)} 筆主題（{args.type} / {date}）")


def cmd_sync_subs(_args):
    token = get_access_token()
    added, updated, unsubbed, skipped = sync_subscribers(token)
    print(f"表單同步完成：新增 {added}、更新 {updated}（其中退訂 {unsubbed}）、略過無效 {skipped}")


def cmd_list_subs(args):
    token = get_access_token()
    subs = _pick_subscribers(sheet_get(token, f"{SUBS_TAB}!A2:F"), args.type)
    print(f"{args.type or '全部'} active 訂閱者共 {len(subs)} 位：")
    for name, email in subs:
        print(f"{name}\t{email}")


def cmd_send(args):
    html_path = Path(args.html)
    if not html_path.is_absolute():
        html_path = Path.cwd() / html_path
    if not html_path.exists():
        die(f"找不到 HTML 檔 {html_path}")
    html_body = html_path.read_text(encoding="utf-8")

    sender = cfg("GMAIL_SENDER") or cfg("GMAIL_ADDRESS")
    if not sender:
        die("請設定 GMAIL_SENDER（或 GMAIL_ADDRESS）作為寄件地址")
    sender_name = cfg("NEWSLETTER_SENDER_NAME", "AI每日電子報")

    override = parse_to_override(getattr(args, "to", ""))
    token = get_access_token()

    if override:
        recipients = override
        print(f"[覆寫] 使用 --to 指定的 {len(recipients)} 位收件人，略過試算表與表單同步")
    else:
        # 1) 同步表單訂閱/退訂（失敗不影響寄送）
        if not args.no_sync:
            try:
                added, updated, unsubbed, skipped = sync_subscribers(token)
                print(f"表單同步：新增 {added}、更新 {updated}（其中退訂 {unsubbed}）、略過無效 {skipped}")
            except Exception as e:
                print(f"[警告] 表單同步失敗（不影響寄送）：{e}")

        # 2) 取訂閱者名單；試算表讀不到時退備援名單，再不行只寄給寄件者本人
        recipients = []
        try:
            recipients = _pick_subscribers(sheet_get(token, f"{SUBS_TAB}!A2:F"), args.type)
        except Exception as e:
            print(f"[警告] 讀取訂閱者工作表失敗：{e}")
        if not recipients:
            recipients = _parse_fallback(cfg("FALLBACK_RECIPIENTS"))
            if recipients:
                print(f"[警告] 試算表無可用名單，改用備援名單（{len(recipients)} 位）")
            else:
                recipients = [("", sender)]
                print("[警告] 無備援名單，只寄給寄件者本人")

    print(f"寄送對象：{len(recipients)} 位")
    print(f"主旨：{args.subject}")
    if args.dry_run:
        for name, email in recipients:
            print(f"  [DRY RUN] 會寄給：{name} <{email}>")
        print(f"\n[DRY RUN] 完成，共 {len(recipients)} 位（未實際寄出）")
        return

    ok = 0
    for name, email in recipients:
        raw = build_raw(sender, sender_name, name, email, args.subject, html_body)
        status, p = http("POST", GMAIL_SEND_URL, token=token, json_body={"raw": raw})
        if status == 200 and isinstance(p, dict) and p.get("id"):
            print(f"  [OK] {name} <{email}>  (id={p['id']})")
            ok += 1
        else:
            print(f"  [失敗] {email}：HTTP {status}: {p}")
    print(f"\n完成：{ok}/{len(recipients)} 封成功寄出")
    sys.exit(0 if ok == len(recipients) and ok > 0 else 1)


# ──────────────────────────── Notion 彙整（純 HTTPS） ────────────────────────────

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# 資料庫 ID（可用環境變數 NOTION_NEWS_DB_ID / NOTION_JP_DB_ID / NOTION_STRATEGY_DB_ID 覆寫）
NOTION_DB_DEFAULT = {
    "AI新聞報": "23f4347d-eda7-4217-89bb-c1a268641b6a",
    "日文報": "a9fb68b5-d6fd-42f6-98ce-4fc8655195fb",
    "策略學習報": "",   # 無硬編碼預設，必須由 NOTION_STRATEGY_DB_ID 提供
}
NOTION_SUMMARY_PROP = {"AI新聞報": "摘要", "日文報": "場景", "策略學習報": "摘要"}
NOTION_DEFAULT_ICON = {"AI新聞報": "📰", "日文報": "🗾", "策略學習報": "📈"}
NOTION_ENV_KEY = {"AI新聞報": "NOTION_NEWS_DB_ID", "日文報": "NOTION_JP_DB_ID",
                  "策略學習報": "NOTION_STRATEGY_DB_ID"}


def notion_env_key(typ):
    return NOTION_ENV_KEY[typ]

_NOTION_INLINE_RE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|\[([^\]]+)\]\(([^)]+)\)")


def _n_seg(content, bold=False, italic=False, link=None):
    rt = {"type": "text", "text": {"content": content}}
    if link:
        rt["text"]["link"] = {"url": link}
    ann = {}
    if bold:
        ann["bold"] = True
    if italic:
        ann["italic"] = True
    if ann:
        rt["annotations"] = ann
    return rt


def _n_plain(s, bold=False, italic=False):
    if not s:
        return []
    return [_n_seg(s[i:i + 1900], bold=bold, italic=italic)
            for i in range(0, len(s), 1900)]


def _n_rich(text):
    """解析 **粗體**、*斜體*、[文字](url) → Notion rich_text 陣列。"""
    text = text or ""
    out, pos = [], 0
    for m in _NOTION_INLINE_RE.finditer(text):
        if m.start() > pos:
            out += _n_plain(text[pos:m.start()])
        if m.group(1) is not None:
            out += _n_plain(m.group(1), bold=True)
        elif m.group(2) is not None:
            out += _n_plain(m.group(2), italic=True)
        else:
            out.append(_n_seg(m.group(3), link=m.group(4)))
        pos = m.end()
    if pos < len(text):
        out += _n_plain(text[pos:])
    return out or [_n_seg("")]


def _n_textblock(kind, text):
    return {"object": "block", "type": kind, kind: {"rich_text": _n_rich(text)}}


def _n_table(rows, has_header=True):
    width = max((len(r) for r in rows), default=1)
    children = []
    for r in rows:
        cells = [_n_rich(c) for c in r]
        cells += [[_n_seg("")]] * (width - len(cells))
        children.append({"object": "block", "type": "table_row",
                         "table_row": {"cells": cells}})
    return {"object": "block", "type": "table",
            "table": {"table_width": width, "has_column_header": has_header,
                      "has_row_header": False, "children": children}}


def _md_to_blocks(md):
    """受限子集 Markdown → Notion 區塊。

    支援：##/### 標題、- 或 * 條列、> 引言、--- 分隔線、| 表格 |、其餘為段落；
    行內支援 **粗體**、*斜體*、[文字](url)。不支援巢狀清單與圖片。
    """
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if len(s) >= 3 and set(s) == {"-"}:
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue
        if s.startswith("|") and s.count("|") >= 2:
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(c and set(c) <= set("-: ") for c in cells):
                    tbl.append(cells)
                i += 1
            if tbl:
                blocks.append(_n_table(tbl))
            continue
        if s.startswith("#### "):
            blocks.append(_n_textblock("heading_3", s[5:].strip()))
        elif s.startswith("### "):
            blocks.append(_n_textblock("heading_3", s[4:].strip()))
        elif s.startswith("## "):
            blocks.append(_n_textblock("heading_2", s[3:].strip()))
        elif s.startswith("# "):
            blocks.append(_n_textblock("heading_2", s[2:].strip()))
        elif s.startswith(">"):
            blocks.append(_n_textblock("quote", s.lstrip(">").strip()))
        elif s.startswith("- ") or s.startswith("* "):
            blocks.append(_n_textblock("bulleted_list_item", s[2:].strip()))
        else:
            blocks.append(_n_textblock("paragraph", s))
        i += 1
    return blocks


def _notion(method, path, token, json_body=None):
    return http(method, f"{NOTION_API}{path}", token=token, json_body=json_body,
                extra_headers={"Notion-Version": NOTION_VERSION})


def cmd_notion_add(args):
    """把當期內容寫進 Notion 資料庫（純存檔；任何失敗都不影響寄送）。"""
    if args.meta:
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    else:
        meta = {"type": args.type, "date": args.date, "title": args.title,
                "summary": args.summary, "icon": args.icon}
    typ = (meta.get("type") or "").strip()
    if typ not in NOTION_ENV_KEY:
        print(f"[警告] 未知報別 {typ!r}，略過 Notion 上傳。")
        return
    db_id = cfg(notion_env_key(typ)) or NOTION_DB_DEFAULT.get(typ, "")
    if not db_id:
        print(f"[警告] 報別 {typ!r} 未設定 {notion_env_key(typ)}，略過 Notion 上傳（不影響寄送）。")
        return
    summary_prop = NOTION_SUMMARY_PROP[typ]
    date = (meta.get("date") or taipei_today()).strip()
    title = (meta.get("title") or "").strip() or date
    summary = (meta.get("summary") or "").strip()
    icon = (meta.get("icon") or NOTION_DEFAULT_ICON[typ]).strip()
    blocks = _md_to_blocks(Path(args.md).read_text(encoding="utf-8"))
    if not blocks:
        print("[警告] notion.md 沒有可寫入的內容，略過。")
        return

    props = {
        "主題": {"title": [_n_seg(title)]},
        "日期": {"date": {"start": date}},
        summary_prop: {"rich_text": _n_rich(summary) if summary else []},
    }
    body = {"parent": {"database_id": db_id},
            "icon": {"type": "emoji", "emoji": icon},
            "properties": props,
            "children": blocks[:50]}

    if getattr(args, "dry_run", False):
        print(f"[DRY RUN] 報別={typ} 日期={date} 標題={title} 區塊數={len(blocks)}")
        print(json.dumps(body, ensure_ascii=False)[:2000])
        return

    token = cfg("NOTION_TOKEN")
    if not token:
        print("[警告] 未設定 NOTION_TOKEN，略過 Notion 上傳（不影響寄送）。")
        return

    status, p = _notion("POST", "/pages", token, body)
    if status != 200 or not isinstance(p, dict) or not p.get("id"):
        print(f"[警告] 建立 Notion 頁面失敗 HTTP {status}：{p}（不影響寄送）")
        return
    page_id = p["id"]
    print(f"[OK] Notion 頁面已建立：{p.get('url', page_id)}")
    rest = blocks[50:]
    for j in range(0, len(rest), 50):
        st, pp = _notion("PATCH", f"/blocks/{page_id}/children", token,
                         {"children": rest[j:j + 50]})
        if st != 200:
            print(f"[警告] 追加內容區塊失敗 HTTP {st}：{pp}")
            break
    print(f"[OK] Notion 內容寫入完成（共 {len(blocks)} 區塊）")


def main():
    parser = argparse.ArgumentParser(description="AI電子報 Google Sheets/Forms/Gmail 整合工具（HTTPS）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("recent-topics", help="列出近 N 天已寄送主題")
    p.add_argument("--type", default="", help="報別（AI新聞報 / 策略學習報），不給則列全部")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_recent_topics)

    p = sub.add_parser("log-topics", help="記錄本期主題")
    p.add_argument("--type", required=True)
    p.add_argument("--date", default="", help="YYYY-MM-DD，預設今天（台北）")
    p.add_argument("--topics-file", required=True, help="一行一則：標題[<TAB>備註]")
    p.set_defaults(func=cmd_log_topics)

    p = sub.add_parser("sync-subs", help="同步 Google 表單回應到訂閱者工作表")
    p.set_defaults(func=cmd_sync_subs)

    p = sub.add_parser("list-subs", help="列出 active 訂閱者")
    p.add_argument("--type", default="")
    p.set_defaults(func=cmd_list_subs)

    p = sub.add_parser("send", help="同步表單後寄送電子報")
    p.add_argument("--html", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--type", required=True, help="報別（決定收件名單）")
    p.add_argument("--no-sync", action="store_true", help="跳過表單同步")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--to", default="", help="強制收件人（'姓名:email,email'），設定時略過試算表與表單同步")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("notion-add", help="把當期內容寫進 Notion 彙整資料庫")
    p.add_argument("--md", required=True, help="內容 Markdown 檔（受限子集）")
    p.add_argument("--meta", default="", help="JSON：{type,date,title,summary,icon}")
    p.add_argument("--type", default="", help="報別（AI新聞報 / 日文報）")
    p.add_argument("--date", default="")
    p.add_argument("--title", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--icon", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_notion_add)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

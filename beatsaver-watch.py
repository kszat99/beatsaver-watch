# beatsaver-watch.py — mirror Letterboxd watcher behavior
# - "new" = created_at > last_seen (fallback to unseen uid if no ts)
# - if no new, send preview of the latest N
# - advance last_seen to newest fetched (clamped to now)
# - state file: data/bs_state.json

import os, json, smtplib, ssl
from pathlib import Path
from datetime import datetime, timezone
from email.message import EmailMessage
import requests

# ========================
# Config via environment
# ========================
def _get_int(name, default):
    v = (os.environ.get(name, "") or "").strip()
    try:
        return int(v)
    except:
        return default

def _get_float(name, default):
    v = (os.environ.get(name, "") or "").strip()
    try:
        return float(v)
    except:
        return default

# Tags to include (comma-separated, e.g. "rock,metal"); empty → all
BS_TAGS = [t.strip().lower() for t in (os.environ.get("BS_TAGS", "") or "").split(",") if t.strip()]
# How many pages per tag (or unfiltered) to scan
BS_MAX_PAGES_PER_TAG = _get_int("BS_MAX_PAGES_PER_TAG", 30)
# Optional minimum score (0..1). Leave unset/0.0 to disable.
BS_MIN_SCORE = _get_float("BS_MIN_SCORE", 0.0)

# Email & behavior (mirror Letterboxd)
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO           = os.environ["EMAIL_TO"]
ALWAYS_EMAIL       = (os.environ.get("ALWAYS_EMAIL", "1") == "1")  # default on
PREVIEW_LAST_N     = _get_int("PREVIEW_LAST_N", 3)                 # default 3
DRY_RUN            = (os.environ.get("DRY_RUN", "0") == "1")       # if 1 → no send, still updates state

STATE_PATH = Path("data/bs_state.json")

API_BASE = "https://api.beatsaver.com"
LATEST_ENDPOINT = "/maps/latest"      # ?page=<n>
SEARCH_TEXT_ENDPOINT = "/search/text" # /search/text/<page>?q=...&sort=LATEST

# ========================
# State (JSON)
# ========================
def load_state():
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_seen": None, "seen_ids": []}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ========================
# Helpers
# ========================
def iso_to_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def seconds_to_mmss(x):
    try:
        x = int(x)
        m, s = divmod(x, 60)
        return f"{m}:{s:02d}"
    except Exception:
        return ""

def percent(x):
    try:
        return f"{round(float(x) * 100, 1)}%"
    except Exception:
        return "—"

def doc_uid(d):
    """Stable id for de-dup across pages/tags."""
    if d.get("id"):
        return str(d["id"])
    v = (d.get("versions") or [])
    if v:
        v0 = v[0]
        for k in ("key", "hash"):
            if v0.get(k):
                return str(v0[k])
    # last-resort fallback
    return f"{d.get('name','')}|{d.get('createdAt','')}"

def normalize_doc(d):
    """Flatten the BeatSaver doc into a simple dict the email can use."""
    uid = doc_uid(d)
    md = d.get("metadata") or {}
    stats = d.get("stats") or {}
    versions = d.get("versions") or []
    v0 = versions[0] if versions else {}

    created = iso_to_dt(d.get("createdAt")) or iso_to_dt(d.get("uploaded")) or iso_to_dt(d.get("lastPublishedAt"))
    uploader = (d.get("uploader") or {}).get("name") or (md.get("levelAuthorName") or "")

    # Difficulties list
    diffs = []
    for df in (v0.get("diffs") or []):
        diff = (df.get("difficulty") or "").replace("ExpertPlus", "Expert+")
        char = df.get("characteristic") or "Standard"
        label = diff if char == "Standard" else f"{diff} ({char})"
        if label and label not in diffs:
            diffs.append(label)

    return {
        "uid": uid,
        "id": d.get("id"),
        "name": d.get("name") or "",
        "uploader": uploader,
        "bpm": md.get("bpm"),
        "duration": md.get("duration"),
        "score": stats.get("score"),
        "upvotes": stats.get("upvotes"),
        "downvotes": stats.get("downvotes"),
        "created_at": created,
        "cover": v0.get("coverURL") or "",
        "download": v0.get("downloadURL") or "",
        "preview": v0.get("previewURL") or "",
        "difficulties": diffs,
        "beatsaver_url": f"https://beatsaver.com/maps/{d.get('id')}" if d.get("id") else "",
    }

# ========================
# Fetch helpers
# ========================
def _headers():
    return {
        "User-Agent": "beatsaver-watch/1.1 (+github actions)",
        "Accept": "application/json",
    }

def fetch_latest_page(page):
    url = f"{API_BASE}{LATEST_ENDPOINT}"
    r = requests.get(url, headers=_headers(), params={"page": page}, timeout=20)
    r.raise_for_status()
    return r.json().get("docs", []) or []

def fetch_search_text_page(page, query):
    # query example: "tag=rock" (we rely on server-side tag filtering)
    url = f"{API_BASE}{SEARCH_TEXT_ENDPOINT}/{page}"
    r = requests.get(url, headers=_headers(), params={"q": query, "sort": "LATEST"}, timeout=20)
    r.raise_for_status()
    return r.json().get("docs", []) or []

def fetch_merged_latest_for_tags(tags, max_pages):
    """
    Preferred path: use /search/text with q='tag=<tag>' so the API filters by tag.
    If no tags specified, fall back to unfiltered /maps/latest.
    """
    merged, seen = [], set()
    if tags:
        for tag in tags:
            q = f"tag={tag}"
            for page in range(max_pages):
                try:
                    docs = fetch_search_text_page(page, q)
                except Exception as e:
                    print(f"[bs] search tag='{tag}' page={page} ERROR: {repr(e)}")
                    break
                print(f"[bs] search tag='{tag}' page={page} docs={len(docs)}")
                if not docs:
                    break
                for d in docs:
                    uid = doc_uid(d)
                    if uid in seen:
                        continue
                    seen.add(uid)
                    merged.append(d)
    else:
        # No tags → just fetch latest unfiltered for the main pool
        for page in range(max_pages):
            try:
                docs = fetch_latest_page(page)
            except Exception as e:
                print(f"[bs] latest page={page} ERROR: {repr(e)}")
                break
            print(f"[bs] latest page={page} docs={len(docs)}")
            if not docs:
                break
            for d in docs:
                uid = doc_uid(d)
                if uid in seen:
                    continue
                seen.add(uid)
                merged.append(d)
    return merged

# ========================
# Email
# ========================
def build_email(items, tags_label, is_preview=False):
    if items:
        subject = f"[BeatSaver] {len(items)} new map(s) for: {tags_label}" + (" (preview)" if is_preview else "")
    else:
        subject = f"[BeatSaver] No new maps today"

    parts = []
    parts.append("<!doctype html><html><body style=\"font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:14px;color:#111;margin:0;padding:16px\">")
    if items:
        parts.append(f"<h2 style='margin:0 0 12px'>New maps for <strong>{tags_label}</strong>{' (preview)' if is_preview else ''}</h2>")
        parts.append("<ul style='list-style:none;margin:0;padding:0'>")
        for it in items:
            cover = f"<img src=\"{it['cover']}\" width='90' style='border-radius:8px;vertical-align:top;margin-right:12px;flex:0 0 auto' alt='cover'/>" if it["cover"] else ""
            runtime = seconds_to_mmss(it["duration"])
            score = percent(it["score"]) if it["score"] is not None else "—"
            diffs = ", ".join(it["difficulties"]) if it["difficulties"] else "—"
            votes = f"{(it['upvotes'] or 0)}↑ / {(it['downvotes'] or 0)}↓"
            bpm = str(it["bpm"]) if it["bpm"] is not None else "—"
            when = it["created_at"].strftime("%Y-%m-%d %H:%M UTC") if it["created_at"] else ""
            preview = f" · <a href='{it['preview']}'>preview</a>" if it["preview"] else ""
            download = f"<a href='{it['download']}'>download</a>" if it["download"] else ""
            page = f"<a href='{it['beatsaver_url']}'>{it['name']}</a>" if it["beatsaver_url"] else it["name"]

            parts.append(
                "<li style='margin:0 0 16px'>"
                "<div style='display:flex;gap:12px'>"
                f"{cover}"
                "<div style='min-width:0'>"
                f"<div style='font-weight:600;margin-bottom:4px'>🎵 {page}</div>"
                f"<div style='color:#555;font-size:13px;margin-bottom:2px'>By {it['uploader']} · {when}</div>"
                f"<div style='color:#222'>BPM {bpm} · Runtime {runtime} · Rating {score} ({votes})</div>"
                f"<div style='color:#222;margin-top:4px'>Difficulties: {diffs}</div>"
                f"<div style='color:#555;margin-top:6px'>{download}{preview}</div>"
                "</div>"
                "</div>"
                "</li>"
            )
        parts.append("</ul>")
    else:
        parts.append("<p>No new maps today.</p>")
    parts.append("</body></html>")
    html_body = "".join(parts)

    plain_lines = []
    if items:
        plain_lines.append(f"New maps ({len(items)}):")
        for it in items:
            runtime = seconds_to_mmss(it["duration"])
            score = percent(it["score"]) if it["score"] is not None else "-"
            line = f"- {it['name']} by {it['uploader']} · BPM {it['bpm']} · {runtime} · {score}"
            if it["beatsaver_url"]:
                line += f" · {it['beatsaver_url']}"
            plain_lines.append(line)
    else:
        plain_lines.append("No new maps today.")
    return subject, "\n".join(plain_lines), html_body

def send_email(items, tags_label, is_preview=False):
    subject, plain_text, html_body = build_email(items, tags_label, is_preview=is_preview)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(plain_text)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

# ========================
# MAIN (mirror Letterboxd)
# ========================
def main():
    state = load_state()
    last_seen = datetime.fromisoformat(state["last_seen"]) if state.get("last_seen") else None
    seen_ids = set(state.get("seen_ids", []))
    tags_label = ", ".join(BS_TAGS) if BS_TAGS else "Latest"

    # 1) Fetch a tag-filtered pool (server-side via /search/text)
    raw_docs = fetch_merged_latest_for_tags(BS_TAGS, BS_MAX_PAGES_PER_TAG)
    items_all = [normalize_doc(d) for d in raw_docs]

    # optional score filter applied uniformly
    if BS_MIN_SCORE > 0:
        items_all = [x for x in items_all if (x["score"] is not None and x["score"] >= BS_MIN_SCORE)]

    # newest → oldest
    items_all.sort(key=lambda it: it["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # 2) Compute "new" exactly like Letterboxd: created_at > last_seen
    if last_seen:
        candidates = [it for it in items_all if (it["created_at"] and it["created_at"] > last_seen) or (not it["created_at"] and it["uid"] not in seen_ids)]
    else:
        # No last_seen yet → seed mode will send preview; treat "new" as empty
        candidates = []

    # De-dup within run + across runs for "new"
    run_seen, new_items = set(), []
    for it in candidates:
        uid = it["uid"]
        if not uid or uid in seen_ids or uid in run_seen:
            continue
        run_seen.add(uid)
        new_items.append(it)

    # 3) If main pool is empty, build a fallback preview from unfiltered latest (do not consume watermark)
    fallback_preview = []
    if not items_all:
        try:
            unfiltered = []
            for page in range(3):  # a few pages are enough for preview
                docs = fetch_latest_page(page)
                if not docs:
                    break
                unfiltered.extend(docs)
            items_unfiltered = [normalize_doc(d) for d in unfiltered]
            items_unfiltered.sort(key=lambda it: it["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            fallback_preview = items_unfiltered[:max(PREVIEW_LAST_N, 0)]
        except Exception as e:
            print("[bs] fallback preview ERROR:", repr(e))

    # 4) Send new or preview (mirror Letterboxd)
    if new_items and not DRY_RUN:
        send_email(new_items, tags_label, is_preview=False)
    elif not DRY_RUN:
        # preview from full tag pool; if empty, use fallback unfiltered preview
        preview = items_all[:max(PREVIEW_LAST_N, 0)] if items_all else fallback_preview
        if preview or ALWAYS_EMAIL:
            send_email(preview, tags_label, is_preview=True)

    # 5) Advance watermark to newest timestamp we fetched in the TAG POOL (clamped to now)
    newest_ts = max((it["created_at"] for it in items_all if it["created_at"]), default=last_seen)
    now_utc = datetime.now(timezone.utc)
    if newest_ts and newest_ts > now_utc:
        newest_ts = now_utc
    if newest_ts:
        state["last_seen"] = newest_ts.isoformat()

    # Persist seen_ids only for items we actually emailed as "new"
    for it in new_items:
        if it["uid"]:
            seen_ids.add(it["uid"])
    state["seen_ids"] = list(seen_ids)[-20000:]
    save_state(state)

    print(f"[bs] done: fetched_pool={len(items_all)}, new={len(new_items)}, seen_total={len(state['seen_ids'])}, last_seen={state.get('last_seen')}, dry_run={DRY_RUN}")

if __name__ == "__main__":
    main()

# beatsaver-watch.py — mirror Letterboxd watcher behavior
# - daily BeatSaver watcher based on /maps/latest
# - priority tags (BS_TAGS) appear first
# - if NO priority-tag maps were created yesterday:
#       show preview of latest 3 priority-tag maps
# - ALWAYS show all "other" maps created yesterday
# - if NO maps were created yesterday at all:
#       fall back to preview-only email for priority tags
#
# DO NOT TOUCH anything except logic blocks — kept intact unless required

import os
import json
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
import requests
from pathlib import Path

# ---------------- CONFIG ----------------

BS_TAGS = [t.strip().lower() for t in (os.environ.get("BS_TAGS", "") or "").split(",") if t.strip()]
BS_MAX_PAGES_PER_TAG = int(os.environ.get("BS_MAX_PAGES_PER_TAG", "12"))
BS_MIN_SCORE = float(os.environ.get("BS_MIN_SCORE", "0.0"))
PREVIEW_LAST_N = int(os.environ.get("PREVIEW_LAST_N", "3"))
ALWAYS_EMAIL = os.environ.get("ALWAYS_EMAIL", "1") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

STATE_PATH = Path("data/bs_state.json")

API_BASE = "https://api.beatsaver.com"

# -------------- HELPERS --------------

def iso_to_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def doc_uid(d):
    if d.get("id"):
        return str(d["id"])
    v = (d.get("versions") or [])
    if v and (v[0].get("key") or v[0].get("hash")):
        return str(v[0].get("key") or v[0].get("hash"))
    return f"{d.get('name','')}|{d.get('createdAt','')}"

def normalize_doc(d):
    uid = doc_uid(d)
    md = d.get("metadata") or {}
    stats = d.get("stats") or {}
    versions = d.get("versions") or []
    v0 = versions[0] if versions else {}

    created = (
        iso_to_dt(d.get("createdAt"))
        or iso_to_dt(d.get("uploaded"))
        or iso_to_dt(d.get("lastPublishedAt"))
    )
    uploader = (d.get("uploader") or {}).get("name") or md.get("levelAuthorName") or ""

    diffs = []
    for df in (v0.get("diffs") or []):
        diff = df.get("difficulty") or ""
        diff = diff.replace("ExpertPlus", "Expert+")
        char = df.get("characteristic") or "Standard"
        label = diff if char == "Standard" else f"{diff} ({char})"
        if label:
            diffs.append(label)

    tags_raw = [str(t).strip() for t in (d.get("tags") or []) if str(t).strip()]
    tags_lower = [t.lower() for t in tags_raw]

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
        "tags": tags_raw,
        "tags_lower": tags_lower,
    }

def fetch_latest_page(page):
    url = f"{API_BASE}/maps/latest"
    r = requests.get(url, headers={"User-Agent": "beatsaver-watch"}, params={"page": page}, timeout=20)
    r.raise_for_status()
    return r.json().get("docs") or []

def fetch_until_yesterday(start_utc, max_pages):
    merged = []
    stop = False
    for page in range(max_pages):
        if stop:
            break
        try:
            docs = fetch_latest_page(page)
        except Exception:
            break
        if not docs:
            break
        for d in docs:
            created = (
                iso_to_dt(d.get("createdAt"))
                or iso_to_dt(d.get("uploaded"))
                or iso_to_dt(d.get("lastPublishedAt"))
            )
            if created and created < start_utc:
                stop = True
                break
            merged.append(d)
    return merged

def get_preview_for_tags(tag_set, limit):
    collected = []
    for page in range(5):
        try:
            docs = fetch_latest_page(page)
        except Exception:
            break
        if not docs:
            break
        for d in docs:
            it = normalize_doc(d)
            if set(it["tags_lower"]).intersection(tag_set):
                collected.append(it)
        if len(collected) >= limit:
            break
    collected.sort(key=lambda it: it["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return collected[:limit]

def send_email(subject, plain_text, html_body):
    if DRY_RUN:
        print("[DRY RUN] Would send email")
        print(subject)
        return
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

# ------------------ MAIN -------------------

def main():
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    yesterday = today - timedelta(days=1)

    start_utc = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
    end_utc = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    raw_docs = fetch_until_yesterday(start_utc, BS_MAX_PAGES_PER_TAG)
    items = [normalize_doc(d) for d in raw_docs]

    if BS_MIN_SCORE > 0:
        items = [x for x in items if x["score"] and x["score"] >= BS_MIN_SCORE]

    # All items created yesterday
    items_yesterday = [
        it for it in items
        if it["created_at"] and start_utc <= it["created_at"] < end_utc
    ]
    items_yesterday.sort(key=lambda it: it["created_at"], reverse=True)

    tag_set = set(BS_TAGS)
    priority_items = []
    other_items = []

    for it in items_yesterday:
        if tag_set and set(it["tags_lower"]).intersection(tag_set):
            priority_items.append(it)
        else:
            other_items.append(it)

    # -------------------------
    # NEW LOGIC HERE
    # -------------------------

    # If NO priority-tag maps yesterday → preview few latest ones
    priority_preview = []
    if not priority_items and tag_set:
        priority_preview = get_preview_for_tags(tag_set, PREVIEW_LAST_N)

    # If NO maps yesterday AT ALL → send preview-only email (old behavior)
    if not items_yesterday:
        if not priority_preview and tag_set:
            priority_preview = get_preview_for_tags(tag_set, PREVIEW_LAST_N)
        subject = f"[BeatSaver] No new maps for {yesterday}"
        send_email(subject, "No new maps yesterday.", "<p>No new maps yesterday.</p>")
        return

    # Build HTML email (minimal change)
    html = f"<h1>BeatSaver {yesterday}</h1>"

    html += "<h2>Priority tags</h2>"
    if priority_items:
        for it in priority_items:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"
    else:
        html += "<p>No new maps with these tags. Latest ones:</p>"
        for it in priority_preview:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"

    html += "<h2>Other maps</h2>"
    if other_items:
        for it in other_items:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"
    else:
        html += "<p>No other maps yesterday.</p>"

    subject = f"[BeatSaver] {len(items_yesterday)} new maps for {yesterday}"
    plain_text = f"{len(items_yesterday)} new maps."

    send_email(subject, plain_text, html)

if __name__ == "__main__":
    main()

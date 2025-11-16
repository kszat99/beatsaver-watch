# beatsaver-watch.py — daily BeatSaver watcher with:
# - priority tags (BS_TAGS)
# - if no priority-tag maps yesterday → preview latest few with priority tags
# - always show other maps from yesterday
# - if literally no maps yesterday → preview-only email
# - FIXED: BeatSaver uses PACIFIC TIME for “1 day ago”, so we use PT window

import os
import json
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
import zoneinfo
from pathlib import Path
import requests

# ---------------- ENV ----------------

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

# ---------------- HELPERS ----------------

def iso_to_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except:
        return None

def doc_uid(d):
    if d.get("id"):
        return str(d["id"])
    v = (d.get("versions") or [])
    if v:
        return str(v[0].get("key") or v[0].get("hash") or "")
    return f"{d.get('name','')}|{d.get('createdAt','')}"

def normalize_doc(d):
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
        diffs.append(diff if char == "Standard" else f"{diff} ({char})")

    tags_raw = [str(t).strip() for t in (d.get("tags") or []) if str(t).strip()]
    tags_lower = [t.lower() for t in tags_raw]

    return {
        "uid": doc_uid(d),
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
    r = requests.get(
        f"{API_BASE}/maps/latest",
        headers={"User-Agent": "beatsaver-watch"},
        params={"page": page},
        timeout=20
    )
    r.raise_for_status()
    return r.json().get("docs") or []

def fetch_until(start_utc, max_pages):
    merged = []
    stop = False
    for page in range(max_pages):
        if stop:
            break
        try:
            docs = fetch_latest_page(page)
        except:
            break
        if not docs:
            break
        for d in docs:
            c = (
                iso_to_dt(d.get("createdAt"))
                or iso_to_dt(d.get("uploaded"))
                or iso_to_dt(d.get("lastPublishedAt"))
            )
            if c and c < start_utc:
                stop = True
                break
            merged.append(d)
    return merged

def get_tag_preview(tag_set, limit):
    out = []
    for page in range(5):
        try:
            docs = fetch_latest_page(page)
        except:
            break
        if not docs:
            break
        for d in docs:
            it = normalize_doc(d)
            if set(it["tags_lower"]).intersection(tag_set):
                out.append(it)
        if len(out) >= limit:
            break
    out.sort(key=lambda it: it["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]

def send_email(subject, plain, html):
    if DRY_RUN:
        print("[DRY RUN]", subject)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

# ---------------- MAIN ----------------

def main():
    now_utc = datetime.now(timezone.utc)

    # ---- FIX: BeatSaver's “1 day ago” uses PACIFIC TIME ----
    PT = zoneinfo.ZoneInfo("America/Los_Angeles")
    now_pt = now_utc.astimezone(PT)
    today_pt = now_pt.date()
    yesterday_pt = today_pt - timedelta(days=1)

    start_pt = datetime(yesterday_pt.year, yesterday_pt.month, yesterday_pt.day, tzinfo=PT)
    end_pt = datetime(today_pt.year, today_pt.month, today_pt.day, tzinfo=PT)

    start_utc = start_pt.astimezone(timezone.utc)
    end_utc = end_pt.astimezone(timezone.utc)

    # Fetch enough latest pages until we reach "yesterday pt"
    raw_docs = fetch_until(start_utc, BS_MAX_PAGES_PER_TAG)
    items = [normalize_doc(d) for d in raw_docs]

    # Score filter
    if BS_MIN_SCORE > 0:
        items = [i for i in items if i["score"] and i["score"] >= BS_MIN_SCORE]

    # Maps inside yesterday window
    items_yest = [
        it for it in items
        if it["created_at"] and start_utc <= it["created_at"] < end_utc
    ]

    # Safety net: sometimes maps show as "1 day ago" but have timestamp slightly after midnight UTC
    # Include maps up to +2 hours after end_utc
    fudge_end = end_utc + timedelta(hours=2)
    if not items_yest:
        items_yest = [
            it for it in items
            if it["created_at"] and start_utc <= it["created_at"] < fudge_end
        ]

    items_yest.sort(key=lambda it: it["created_at"], reverse=True)

    tag_set = set(BS_TAGS)
    priority = []
    others = []

    for it in items_yest:
        if tag_set and set(it["tags_lower"]).intersection(tag_set):
            priority.append(it)
        else:
            others.append(it)

    # Per-tag preview if no priority yesterday
    priority_preview = []
    if tag_set and not priority:
        priority_preview = get_tag_preview(tag_set, PREVIEW_LAST_N)

    # If nothing at all
    if not items_yest:
        if not priority_preview and tag_set:
            priority_preview = get_tag_preview(tag_set, PREVIEW_LAST_N)
        subject = f"[BeatSaver] No new maps for {yesterday_pt}"
        send_email(subject, subject, f"<p>{subject}</p>")
        return

    # ---------- Build Email ----------
    html = f"<h1>BeatSaver – {yesterday_pt}</h1>"

    # Priority section
    html += "<h2>Priority maps</h2>"
    if priority:
        for it in priority:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"
    else:
        html += "<p>No new priority-tag maps yesterday. Latest ones:</p>"
        for it in priority_preview:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"

    # Other maps
    html += "<h2>Other maps from yesterday</h2>"
    if others:
        for it in others:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"
    else:
        html += "<p>No other maps yesterday.</p>"

    subject = f"[BeatSaver] {len(items_yest)} new maps for {yesterday_pt}"
    send_email(subject, subject, html)

if __name__ == "__main__":
    main()

# beatsaver-watch.py
# FINAL VERSION — CLEAN, CORRECT, AND USING ?before=
#
# FEATURES:
# - Fetch ALL maps uploaded yesterday (UTC), using ?before= for correct pagination.
# - Split into:
#       1) Priority maps (tags from BS_TAGS)
#       2) Other maps
# - Only if NO priority maps yesterday → preview latest 5 priority-tag maps
# - NO timezone conversions — strict UTC midnight boundaries
# - Simple HTML email

import os
import smtplib
import ssl
import requests
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage


# =======================
# CONFIG
# =======================

BS_TAGS = [t.strip().lower() for t in (os.environ.get("BS_TAGS", "") or "").split(",") if t.strip()]
BS_MAX_PAGES = int(os.environ.get("BS_MAX_PAGES", "50"))   # safety cap for pagination
PREVIEW_COUNT = 5
BS_MIN_SCORE = float(os.environ.get("BS_MIN_SCORE", "0.0"))
DRY_RUN = (os.environ.get("DRY_RUN", "0") == "1")

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

API_BASE = "https://api.beatsaver.com"


# =======================
# HELPERS
# =======================

def iso_to_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except:
        return None


def normalize(doc):
    """Return flattened map info with created_at and tags."""
    md = doc.get("metadata") or {}
    stats = doc.get("stats") or {}
    versions = doc.get("versions") or []
    v0 = versions[0] if versions else {}

    created = (
        iso_to_dt(doc.get("createdAt"))
        or iso_to_dt(doc.get("uploaded"))
        or iso_to_dt(doc.get("lastPublishedAt"))
    )

    tags_raw = [str(t).strip() for t in (doc.get("tags") or []) if str(t).strip()]
    tags_lower = [t.lower() for t in tags_raw]

    return {
        "id": doc.get("id"),
        "name": doc.get("name") or "",
        "created_at": created,
        "tags_lower": tags_lower,
        "beatsaver_url": f"https://beatsaver.com/maps/{doc.get('id')}" if doc.get("id") else "",
        "score": stats.get("score"),
    }


def fetch_latest(before_timestamp, max_pages):
    """
    Fetch /maps/latest pages until timestamps fall below a cutoff.
    """
    results = []
    before = before_timestamp

    for page in range(max_pages):
        r = requests.get(
            f"{API_BASE}/maps/latest",
            params={"before": before},
            headers={"User-Agent": "beatsaver-watch"},
            timeout=20
        )
        r.raise_for_status()
        docs = r.json().get("docs") or []
        if not docs:
            break

        for d in docs:
            results.append(d)

        # pagination: next BEFORE = last map timestamp
        last = docs[-1]
        last_created = (
            iso_to_dt(last.get("createdAt"))
            or iso_to_dt(last.get("uploaded"))
            or iso_to_dt(last.get("lastPublishedAt"))
        )

        # if no timestamp, break
        if not last_created:
            break

        # update BEFORE with last timestamp
        before = last_created.isoformat()

    return results


def send_email(subject, plain, html):
    if DRY_RUN:
        print("\n[DRY RUN EMAIL]")
        print(subject)
        print(html)
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


# =======================
# MAIN LOGIC
# =======================

def main():

    # ----- UTC "yesterday" window -----
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    yesterday = today - timedelta(days=1)

    start_utc = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
    end_utc   = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    print("[debug] start_utc =", start_utc)
    print("[debug] end_utc   =", end_utc)

    # ----- Fetch maps using ?before= for correct pagination -----
    raw = fetch_latest(end_utc.isoformat(), BS_MAX_PAGES)
    print("[debug] fetched raw count:", len(raw))

    # ----- Normalize -----
    items = [normalize(d) for d in raw]

    # Score filter
    if BS_MIN_SCORE > 0:
        items = [i for i in items if i["score"] and i["score"] >= BS_MIN_SCORE]

    # ----- Extract only maps from yesterday -----
    yest = [
        it for it in items
        if it["created_at"] and start_utc <= it["created_at"] < end_utc
    ]
    yest.sort(key=lambda x: x["created_at"], reverse=True)

    print("[debug] yesterday map count:", len(yest))

    # ----- Split into priority and others -----
    tagset = set(BS_TAGS)
    priority = []
    others = []

    for it in yest:
        if tagset and set(it["tags_lower"]).intersection(tagset):
            priority.append(it)
        else:
            others.append(it)

    # ----- Preview logic (OPTION B) -----
    preview = []
    if not priority and tagset:
        print("[debug] No priority maps yesterday. Fetching preview...")
        raw_preview = fetch_latest(end_utc.isoformat(), 20)
        norm_preview = [normalize(d) for d in raw_preview]

        for it in norm_preview:
            if set(it["tags_lower"]).intersection(tagset):
                preview.append(it)
            if len(preview) >= PREVIEW_COUNT:
                break

    # ----- Build Email -----
    subject = f"[BeatSaver] {len(yest)} new maps for {yesterday}"

    html = f"<h1>BeatSaver – {yesterday}</h1>"

    # Priority section
    html += "<h2>Priority maps</h2>"
    if priority:
        for it in priority:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"
    else:
        html += "<p>No priority maps yesterday.</p>"
        if preview:
            html += "<p>Latest priority-tag maps:</p>"
            for it in preview:
                html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"

    # Other maps
    html += "<h2>Other maps yesterday</h2>"
    if others:
        for it in others:
            html += f"<p>🎵 <a href='{it['beatsaver_url']}'>{it['name']}</a></p>"
    else:
        html += "<p>No other maps yesterday.</p>"

    # Plaintext is simple
    plain = subject

    # ----- SEND -----
    send_email(subject, plain, html)


if __name__ == "__main__":
    main()

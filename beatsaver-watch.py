# beatsaver-watch.py — YESTERDAY-based watcher with SAME EMAIL HTML AS BEFORE
# - preserves build_email() EXACTLY
# - preserves HTML formatting, covers, BPM, diffs, uploader, preview links
# - NO timezone bullshit — uses straight UTC "yesterday"
# - uses ?before= for correct pagination
# - priority-tag maps first, other yesterday maps second
# - preview (5) only if NO priority maps yesterday
# - drops Letterboxd-style last_seen logic fully

import os, json, smtplib, ssl
from pathlib import Path
from datetime import datetime, timedelta, timezone
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

BS_TAGS = [t.strip().lower() for t in (os.environ.get("BS_TAGS", "") or "").split(",") if t.strip()]
BS_MAX_PAGES = _get_int("BS_MAX_PAGES", 50)
PREVIEW_LAST_N = 5
BS_MIN_SCORE = _get_float("BS_MIN_SCORE", 0.0)

# Email
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO           = os.environ["EMAIL_TO"]
DRY_RUN            = (os.environ.get("DRY_RUN", "0") == "1")

API_BASE = "https://api.beatsaver.com"
LATEST_ENDPOINT = "/maps/latest"


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
    if d.get("id"):
        return str(d["id"])
    v = (d.get("versions") or [])
    if v:
        v0 = v[0]
        for k in ("key", "hash"):
            if v0.get(k):
                return str(v0[k])
    return f"{d.get('name','')}|{d.get('createdAt','')}"


# =============== CRITICAL FIX — ADDED TAGS + TAGS_LOWER =================
def normalize_doc(d):
    uid = doc_uid(d)
    md = d.get("metadata") or {}
    stats = d.get("stats") or {}
    versions = d.get("versions") or []
    v0 = versions[0] if versions else {}

    created = (
        iso_to_dt(d.get("createdAt")) or
        iso_to_dt(d.get("uploaded")) or
        iso_to_dt(d.get("lastPublishedAt"))
    )

    uploader = (d.get("uploader") or {}).get("name") or (md.get("levelAuthorName") or "")

    diffs = []
    for df in (v0.get("diffs") or []):
        diff = (df.get("difficulty") or "").replace("ExpertPlus", "Expert+")
        char = df.get("characteristic") or "Standard"
        label = diff if char == "Standard" else f"{diff} ({char})"
        if label and label not in diffs:
            diffs.append(label)

    # NEW — extract tags (BeatSaver root-level)
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
        # NEW FIELDS — required by your priority logic
        "tags": tags_raw,
        "tags_lower": tags_lower,
    }


# ========================
# Fetch yesterday using ?before=
# ========================
def fetch_latest_before(before_iso, max_pages):
    results = []
    before = before_iso
    for _ in range(max_pages):
        r = requests.get(
            f"{API_BASE}{LATEST_ENDPOINT}",
            params={"before": before},
            headers={"User-Agent": "beatsaver-watch"},
            timeout=20,
        )
        r.raise_for_status()
        docs = r.json().get("docs") or []
        if not docs:
            break
        results.extend(docs)
        last = docs[-1]
        ts = (
            iso_to_dt(last.get("createdAt")) or
            iso_to_dt(last.get("uploaded"))
        )
        if not ts:
            break
        before = ts.isoformat()
    return results


# ========================
# Email — UNCHANGED EXACTLY
# ========================
def build_email(items, label, is_preview=False):
    parts = []
    parts.append("<!doctype html><html><body style=\"font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:14px;color:#111;margin:0;padding:16px\">")
    if items:
        parts.append(f"<h2 style='margin:0 0 12px'>Maps for <strong>{label}</strong>{' (preview)' if is_preview else ''}</h2>")
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
        parts.append("<p>No maps.</p>")
    parts.append("</body></html>")
    html_body = "".join(parts)

    plain = f"Maps for {label}"
    return f"[BeatSaver] {len(items)} map(s) for {label}", plain, html_body


def send_email(items, label, is_preview=False):
    subject, plain, html = build_email(items, label, is_preview)
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


# ========================
# MAIN — Yesterday Logic
# ========================
def main():
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    yesterday = today - timedelta(days=1)

    start_utc = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
    end_utc   = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    print("[debug] yesterday window:", start_utc, "→", end_utc)

    raw = fetch_latest_before(end_utc.isoformat(), BS_MAX_PAGES)
    print("[debug] raw fetched:", len(raw))

    items = [normalize_doc(d) for d in raw]

    if BS_MIN_SCORE > 0:
        items = [x for x in items if (x["score"] is not None and x["score"] >= BS_MIN_SCORE)]

    yest = [
        it for it in items
        if it["created_at"] and start_utc <= it["created_at"] < end_utc
    ]
    yest.sort(key=lambda it: it["created_at"], reverse=True)

    print("[debug] yesterday count:", len(yest))

    tagset = set(BS_TAGS)
    priority = []
    others = []

    for it in yest:
        if tagset and set(it["tags_lower"]).intersection(tagset):
            priority.append(it)
        else:
            others.append(it)

    preview = []
    if not priority and tagset:
        print("[debug] building preview")
        pre_raw = fetch_latest_before(end_utc.isoformat(), 20)
        pre_norm = [normalize_doc(d) for d in pre_raw]
        for it in pre_norm:
            if set(it["tags_lower"]).intersection(tagset):
                preview.append(it)
            if len(preview) >= PREVIEW_LAST_N:
                break

    final_items = priority if priority else preview
    label_priority = "Priority tags: " + ", ".join(BS_TAGS)

    send_email(final_items, label_priority, is_preview=(not priority))

    if others:
        send_email(others, "Other maps from yesterday")


if __name__ == "__main__":
    main()

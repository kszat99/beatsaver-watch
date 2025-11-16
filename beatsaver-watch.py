# beatsaver-watch.py — daily BeatSaver digest
#
# New behavior:
# - Look at all maps created "yesterday" (UTC)
# - In ONE email:
#     Section 1: maps that have any of BS_TAGS (e.g. rock/metal)
#       - If none yesterday -> show preview of latest N maps with those tags
#     Section 2: all OTHER maps from yesterday (no preview fallback)
# - If there were NO maps yesterday at all:
#       -> send only a preview email with latest N maps with BS_TAGS
#
# Env vars:
#   GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_TO – mandatory
#   BS_TAGS                 – comma separated tags ("rock,metal")
#   BS_MAX_PAGES_PER_TAG    – how many /maps/latest pages to scan (default 30)
#   BS_MIN_SCORE            – optional minimum score (0..1)
#   PREVIEW_LAST_N          – how many maps in previews (default 3)
#   ALWAYS_EMAIL            – if "1", still send preview even if empty
#   DRY_RUN                 – if "1", don't send email, just log

import os, json, smtplib, ssl
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import requests

# ========================
# Config via environment
# ========================
def _get_int(name, default):
    v = (os.environ.get(name, "") or "").strip()
    try:
        return int(v)
    except Exception:
        return default

def _get_float(name, default):
    v = (os.environ.get(name, "") or "").strip()
    try:
        return float(v)
    except Exception:
        return default

# Tags to treat as "priority" (comma-separated, e.g. "rock,metal"); empty → no priority section
BS_TAGS = [t.strip().lower() for t in (os.environ.get("BS_TAGS", "") or "").split(",") if t.strip()]
# How many pages of /maps/latest to scan
BS_MAX_PAGES_PER_TAG = _get_int("BS_MAX_PAGES_PER_TAG", 30)
# Optional minimum score (0..1). Leave unset/0.0 to disable.
BS_MIN_SCORE = _get_float("BS_MIN_SCORE", 0.0)

# Email & behavior
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO           = os.environ["EMAIL_TO"]
ALWAYS_EMAIL       = (os.environ.get("ALWAYS_EMAIL", "1") == "1")  # default on
PREVIEW_LAST_N     = _get_int("PREVIEW_LAST_N", 3)                 # default 3
DRY_RUN            = (os.environ.get("DRY_RUN", "0") == "1")       # if 1 → no send

STATE_PATH = Path("data/bs_state.json")  # kept for compatibility, but no logic depends on it

API_BASE = "https://api.beatsaver.com"
LATEST_ENDPOINT = "/maps/latest"  # ?page=<n>

# ========================
# State (JSON) – optional
# ========================
def load_state():
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}

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
    """Stable id for de-dup across pages."""
    if d.get("id"):
        return str(d["id"])
    v = (d.get("versions") or [])
    if v:
        v0 = v[0]
        for k in ("key", "hash"):
            if v0.get(k):
                return str(v0[k])
    # last resort
    return f"{d.get('name','')}|{d.get('createdAt','')}"

def normalize_doc(d):
    """Flatten the BeatSaver doc into a simple dict the email can use."""
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
    uploader = (d.get("uploader") or {}).get("name") or (md.get("levelAuthorName") or "")

    # Difficulties list
    diffs = []
    for df in (v0.get("diffs") or []):
        diff = (df.get("difficulty") or "").replace("ExpertPlus", "Expert+")
        char = df.get("characteristic") or "Standard"
        label = diff if char == "Standard" else f"{diff} ({char})"
        if label and label not in diffs:
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

# ========================
# Fetch helpers
# ========================
def _headers():
    return {
        "User-Agent": "beatsaver-watch/2.1 (+github actions)",
        "Accept": "application/json",
    }

def fetch_latest_page(page: int):
    url = f"{API_BASE}{LATEST_ENDPOINT}"
    r = requests.get(url, headers=_headers(), params={"page": page}, timeout=20)
    r.raise_for_status()
    return r.json().get("docs", []) or []

def fetch_yesterday_docs(max_pages: int, start_utc: datetime):
    """
    Fetch /maps/latest pages until we reach items older than 'start_utc'.
    Returns raw BeatSaver docs (not normalized yet).
    """
    merged = []
    stop = False
    for page in range(max_pages):
        if stop:
            break
        try:
            docs = fetch_latest_page(page)
        except Exception as e:
            print(f"[bs] latest page={page} ERROR: {repr(e)}")
            break
        print(f"[bs] latest page={page} docs={len(docs)}")
        if not docs:
            break

        for d in docs:
            created = (
                iso_to_dt(d.get("createdAt"))
                or iso_to_dt(d.get("uploaded"))
                or iso_to_dt(d.get("lastPublishedAt"))
            )
            if created and created < start_utc:
                # everything after this will be older too
                stop = True
                break
            merged.append(d)
    return merged

def get_latest_with_tags(tag_set, limit, max_pages):
    """
    Get up to 'limit' latest maps that have any of 'tag_set',
    scanning at most 'max_pages' pages of /maps/latest.
    """
    collected = []
    for page in range(max_pages):
        try:
            docs = fetch_latest_page(page)
        except Exception as e:
            print(f"[bs] tag preview page={page} ERROR: {repr(e)}")
            break
        if not docs:
            break
        for d in docs:
            it = normalize_doc(d)
            tags_lower = set(it.get("tags_lower") or [])
            if tags_lower.intersection(tag_set):
                collected.append(it)
        if len(collected) >= limit:
            break

    collected.sort(
        key=lambda it: it["created_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return collected[:limit]

# ========================
# Email rendering
# ========================
def _render_item_html(it):
    cover = (
        f"<img src='{it['cover']}' width='90' height='90' "
        "style='border-radius:8px;margin-right:12px;flex:0 0 auto' alt='cover'/>"
        if it["cover"]
        else ""
    )
    runtime = seconds_to_mmss(it["duration"])
    score = percent(it["score"]) if it["score"] is not None else "—"
    diffs = ", ".join(it["difficulties"]) if it["difficulties"] else "—"
    votes = f"{(it['upvotes'] or 0)}↑ / {(it['downvotes'] or 0)}↓"
    bpm = str(it["bpm"]) if it["bpm"] is not None else "—"
    when = it["created_at"].strftime("%Y-%m-%d %H:%M UTC") if it["created_at"] else ""
    preview = f" · <a href='{it['preview']}'>preview</a>" if it["preview"] else ""
    download

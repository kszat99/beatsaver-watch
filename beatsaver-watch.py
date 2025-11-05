import os, json, smtplib, ssl
from pathlib import Path
from datetime import datetime, timezone
from email.message import EmailMessage
import requests

# ---------- ENV ----------
BS_TAGS = [t.strip().lower() for t in os.environ.get("BS_TAGS", "").split(",") if t.strip()]
BS_MIN_SCORE = float(os.environ.get("BS_MIN_SCORE", "0"))        # e.g., 0.80 keeps ≥80%
BS_MAX_PER_RUN = int(os.environ.get("BS_MAX_PER_RUN", "50"))     # cap results per email (0 = no cap)
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

STATE_PATH = Path("data/bs_state.json")

API_BASE = "https://api.beatsaver.com"
LATEST_ENDPOINT = "/maps/latest"   # supports ?page=<0..>
PAGES_TO_FETCH = 5                 # how many "latest" pages to scan each run

# ---------- STATE ----------
def load_state():
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_seen": None, "seen_ids": []}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ---------- HELPERS ----------
def iso_to_dt(s):
    if not s:
        return None
    try:
        # BeatSaver returns ISO with 'Z'
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

def fetch_latest_pages(pages=PAGES_TO_FETCH):
    headers = {"User-Agent": "beatsaver-watch/1.0 (+github actions)"}
    out = []
    for page in range(pages):
        url = f"{API_BASE}{LATEST_ENDPOINT}?page={page}"
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("docs", []) or [])
    return out

def filter_by_tags(docs, tags_lower):
    if not tags_lower:
        return docs
    keep = []
    for d in docs:
        doc_tags = [str(t).lower() for t in (d.get("tags") or [])]
        if any(t in doc_tags for t in tags_lower):
            keep.append(d)
    return keep

def normalize_doc(d):
    """Shrink BeatSaver doc to just what we need."""
    vid = d.get("id")
    name = d.get("name") or ""
    uploader = (d.get("uploader") or {}).get("name") or ""
    md = d.get("metadata") or {}
    bpm = md.get("bpm")
    duration = md.get("duration")
    level_author = md.get("levelAuthorName") or ""
    stats = d.get("stats") or {}
    score = stats.get("score")  # 0..1
    up = stats.get("upvotes")
    down = stats.get("downvotes")
    created = iso_to_dt(d.get("createdAt")) or iso_to_dt(d.get("uploaded")) or iso_to_dt(d.get("lastPublishedAt"))
    tags = d.get("tags") or []

    versions = d.get("versions") or []
    v0 = versions[0] if versions else {}
    cover = v0.get("coverURL") or ""
    download = v0.get("downloadURL") or ""
    preview = v0.get("previewURL") or ""
    diffs = v0.get("diffs") or []

    difficulties = []
    for df in diffs:
        diff = (df.get("difficulty") or "").replace("ExpertPlus", "Expert+")
        char = df.get("characteristic") or "Standard"
        label = diff if char == "Standard" else f"{diff} ({char})"
        if label and label not in difficulties:
            difficulties.append(label)

    return {
        "id": vid,
        "name": name,
        "uploader": uploader or level_author,
        "bpm": bpm,
        "duration": duration,
        "score": score,
        "upvotes": up,
        "downvotes": down,
        "created_at": created,
        "tags": tags,
        "cover": cover,
        "download": download,
        "preview": preview,
        "difficulties": difficulties,
        "beatsaver_url": f"https://beatsaver.com/maps/{vid}" if vid else "",
    }

def build_email(items, tags_label):
    if items:
        subject = f"[BeatSaver] {len(items)} new map(s) for: {tags_label}"
    else:
        subject = f"[BeatSaver] No new maps today"

    parts = []
    parts.append("<!doctype html><html><body style=\"font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:14px;color:#111;margin:0;padding:16px\">")
    if items:
        h = f"New maps for tags: <strong>{tags_label}</strong>"
        parts.append(f"<h2 style='margin:0 0 12px'>{h}</h2>")
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

    # plain text
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
    plain_text = "\n".join(plain_lines)

    return subject, plain_text, html_body

def send_email(items, tags_label):
    subject, plain_text, html_body = build_email(items, tags_label)
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

# ---------- MAIN ----------
def main():
    state = load_state()
    last_seen = datetime.fromisoformat(state["last_seen"]) if state.get("last_seen") else None
    seen_ids = set(state.get("seen_ids", []))

    # 1) fetch latest
    try:
        docs = fetch_latest_pages(PAGES_TO_FETCH)
    except Exception as e:
        print("[bs] ERROR fetching:", repr(e))
        docs = []

    # 2) filter by tags (client-side)
    docs = filter_by_tags(docs, BS_TAGS)

    # 3) normalize + optional score filter
    items = [normalize_doc(d) for d in docs]
    if BS_MIN_SCORE > 0:
        items = [x for x in items if (x["score"] is not None and x["score"] >= BS_MIN_SCORE)]

    # 4) dedup (GUID-first) + optional watermark
    new_items = []
    for it in items:
        vid = it["id"]
        if not vid or vid in seen_ids:
            continue
        # If you want a strict time guard, uncomment next two lines:
        # if last_seen and it["created_at"] and not (it["created_at"] > last_seen):
        #     continue
        new_items.append(it)

    if BS_MAX_PER_RUN > 0:
        new_items = new_items[:BS_MAX_PER_RUN]

    # 5) email
    tags_label = ", ".join(BS_TAGS) if BS_TAGS else "Latest"
    if new_items and not DRY_RUN:
        send_email(new_items, tags_label)

    # 6) update state (max created_at; only IDs we emailed)
    max_ts = max((it["created_at"] for it in items if it["created_at"]), default=last_seen)
    now_utc = datetime.now(timezone.utc)
    if max_ts and max_ts > now_utc:
        max_ts = now_utc
    if max_ts:
        state["last_seen"] = max_ts.isoformat()

    for it in new_items:
        if it["id"]:
            seen_ids.add(it["id"])
    state["seen_ids"] = list(seen_ids)[-20000:]
    save_state(state)

    print(f"[bs] done: fetched={len(docs)}, filtered={len(items)}, new={len(new_items)}, seen_total={len(state['seen_ids'])}, dry_run={DRY_RUN}")

if __name__ == "__main__":
    main()

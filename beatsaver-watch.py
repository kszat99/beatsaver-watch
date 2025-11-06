# beatsaver_watch.py — per-tag from LATEST, seed=3, delta runs, preview=3

import os, json, smtplib, ssl
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import requests

# ---------- ENV ----------
def _get_float_env(name, default):
    v = (os.environ.get(name, "") or "").strip()
    try: return float(v)
    except: return default

def _get_int_env(name, default):
    v = (os.environ.get(name, "") or "").strip()
    try: return int(v)
    except: return default

BS_TAGS = [t.strip().lower() for t in os.environ.get("BS_TAGS", "").split(",") if t.strip()]
BS_MIN_SCORE   = _get_float_env("BS_MIN_SCORE", 0.0)   # 0.0 = no filter
BS_MAX_PER_RUN = _get_int_env("BS_MAX_PER_RUN", 50)    # 0 = no cap
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

STATE_PATH = Path("data/bs_state.json")

API_BASE = "https://api.beatsaver.com"
LATEST_ENDPOINT = "/maps/latest/{page}"

# Behavior
SEED_COUNT              = 3     # first run: collect at least this many latest tagged maps
CUTOFF_DAYS_BOOTSTRAP   = 7     # soft cap for pages during seed
MAX_PAGES_PER_TAG_SEED  = 30    # max pages per tag while seeding
MAX_PAGES_PER_TAG_DELTA = 12    # max pages per tag during daily deltas
PREVIEW_LAST_N          = 3     # preview when no new

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
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except: return None

def seconds_to_mmss(x):
    try:
        x = int(x); m, s = divmod(x, 60)
        return f"{m}:{s:02d}"
    except: return ""

def percent(x):
    try: return f"{round(float(x) * 100, 1)}%"
    except: return "—"

def doc_uid(d):
    if d.get("id"): return str(d["id"])
    v = (d.get("versions") or [])
    if v:
        v0 = v[0]
        for k in ("key", "hash"):
            if v0.get(k): return str(v0[k])
    return f"{d.get('name','')}|{d.get('createdAt','')}"

def normalize_doc(d):
    uid = doc_uid(d)
    vid = d.get("id")
    md  = d.get("metadata") or {}
    stats = d.get("stats") or {}
    versions = d.get("versions") or []
    v0 = versions[0] if versions else {}
    diffs = v0.get("diffs") or []

    difficulties = []
    for df in diffs:
        diff = (df.get("difficulty") or "").replace("ExpertPlus", "Expert+")
        char = df.get("characteristic") or "Standard"
        label = diff if char == "Standard" else f"{diff} ({char})"
        if label and label not in difficulties: difficulties.append(label)

    created = iso_to_dt(d.get("createdAt")) or iso_to_dt(d.get("uploaded")) or iso_to_dt(d.get("lastPublishedAt"))
    uploader = (d.get("uploader") or {}).get("name") or (md.get("levelAuthorName") or "")

    return {
        "uid": uid,
        "id": vid,
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
        "difficulties": difficulties,
        "beatsaver_url": f"https://beatsaver.com/maps/{vid}" if vid else "",
        "tags": d.get("tags") or [],
    }

# ---------- FETCH (LATEST per tag) ----------
def fetch_latest_for_tag(tag, cutoff_dt, max_pages):
    """
    Pull newest→older from /maps/latest/{page}, keep only docs whose tags include `tag`.
    Stop early when page's oldest < cutoff.
    """
    headers = {"User-Agent": "beatsaver-watch/1.0 (+github actions)", "Accept": "application/json"}
    tag_lc = (tag or "").lower()
    out, seen_page_uids = [], set()
    for page in range(max_pages):
        url = f"{API_BASE}{LATEST_ENDPOINT.format(page=page)}"
        r = requests.get(url, headers=headers, timeout=20)
        try:
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[bs] latest tag='{tag}' page={page} ERROR {getattr(r,'status_code','NA')}: {repr(e)}")
            break
        docs = data.get("docs", []) or []
        print(f"[bs] latest tag='{tag}' page={page} docs={len(docs)}")
        if not docs: break

        for d in docs:
            uid = doc_uid(d)
            if uid in seen_page_uids: continue
            seen_page_uids.add(uid)
            doc_tags = [str(t).lower() for t in (d.get("tags") or [])]
            if not tag_lc or tag_lc in doc_tags:
                out.append(d)

        tail = docs[-1]
        tail_created = iso_to_dt(tail.get("createdAt")) or iso_to_dt(tail.get("uploaded")) or iso_to_dt(tail.get("lastPublishedAt"))
        if cutoff_dt and tail_created and tail_created < cutoff_dt:
            break
    return out

def fetch_merged_latest_for_tags(tags, cutoff_dt, max_pages):
    """Fetch per tag, merge and dedup by UID across tags."""
    if not tags:
        return fetch_latest_for_tag("", cutoff_dt, max_pages)
    merged, seen = [], set()
    for t in tags:
        docs = fetch_latest_for_tag(t, cutoff_dt, max_pages)
        for d in docs:
            uid = doc_uid(d)
            if uid in seen: continue
            seen.add(uid); merged.append(d)
    return merged

# ---------- EMAIL ----------
def build_email(items, tags_label):
    if items: subject = f"[BeatSaver] {len(items)} new map(s) for: {tags_label}"
    else:     subject = f"[BeatSaver] No new maps today"

    parts = []
    parts.append("<!doctype html><html><body style=\"font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:14px;color:#111;margin:0;padding:16px\">")
    if items:
        parts.append(f"<h2 style='margin:0 0 12px'>New maps for tags: <strong>{tags_label}</strong></h2>")
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
            if it["beatsaver_url"]: line += f" · {it['beatsaver_url']}"
            plain_lines.append(line)
    else:
        plain_lines.append("No new maps today.")
    return (subject, "\n".join(plain_lines), html_body)

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
    tags_label = ", ".join(BS_TAGS) if BS_TAGS else "Latest"

    if not last_seen:
        # --- SEED MODE: collect newest tagged maps until we have >= SEED_COUNT ---
        cutoff_seed = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS_BOOTSTRAP)
        raw_docs = fetch_merged_latest_for_tags(BS_TAGS, cutoff_seed, MAX_PAGES_PER_TAG_SEED)
        items = [normalize_doc(d) for d in raw_docs]
        items.sort(key=lambda it: it["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        if BS_MIN_SCORE > 0:
            items = [x for x in items if (x["score"] is not None and x["score"] >= BS_MIN_SCORE)]
        seed_list = items[:SEED_COUNT]

        # send preview of the 3 newest (seed)
        if seed_list and not DRY_RUN:
            send_email(seed_list, tags_label + " (seed preview)")

        # watermark = newest created_at we saw (so next run only looks newer)
        max_ts = max((it["created_at"] for it in items if it["created_at"]), default=None)
        if max_ts and max_ts > datetime.now(timezone.utc): max_ts = datetime.now(timezone.utc)
        if max_ts: state["last_seen"] = max_ts.isoformat()

        # memory: persist the seed UIDs we emailed (avoid re-emailing them as "new")
        for it in seed_list:
            if it["uid"]: seen_ids.add(it["uid"])
        state["seen_ids"] = list(seen_ids)[-20000:]
        save_state(state)
        print(f"[bs] SEED done: pool={len(items)}, seeded={len(seed_list)}, last_seen={state.get('last_seen')}, dry_run={DRY_RUN}")
        return

    # --- DELTA MODE: only look newer than watermark ---
    cutoff_dt = last_seen
    raw_docs = fetch_merged_latest_for_tags(BS_TAGS, cutoff_dt, MAX_PAGES_PER_TAG_DELTA)
    items = [normalize_doc(d) for d in raw_docs]
    if BS_MIN_SCORE > 0:
        items = [x for x in items if (x["score"] is not None and x["score"] >= BS_MIN_SCORE)]
    if cutoff_dt:
        items = [it for it in items if not it["created_at"] or it["created_at"] >= cutoff_dt]
    items.sort(key=lambda it: it["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # dedup within run + across runs
    run_seen, new_items = set(), []
    for it in items:
        uid = it["uid"]
        if not uid or uid in seen_ids or uid in run_seen: continue
        run_seen.add(uid); new_items.append(it)
    if BS_MAX_PER_RUN > 0: new_items = new_items[:BS_MAX_PER_RUN]

    if new_items and not DRY_RUN:
        send_email(new_items, tags_label)
    elif not DRY_RUN:
        preview = items[:PREVIEW_LAST_N]
        if preview: send_email(preview, tags_label + " (preview)")

    # advance watermark to newest timestamp we saw this run (don’t go past now)
    max_ts = max((it["created_at"] for it in items if it["created_at"]), default=last_seen)
    now_utc = datetime.now(timezone.utc)
    if max_ts and max_ts > now_utc: max_ts = now_utc
    if max_ts: state["last_seen"] = max_ts.isoformat()

    for it in new_items:
        if it["uid"]: seen_ids.add(it["uid"])
    state["seen_ids"] = list(seen_ids)[-20000:]
    save_state(state)

    print(f"[bs] DELTA done: fetched_pool={len(items)}, new={len(new_items)}, seen_total={len(state['seen_ids'])}, last_seen={state.get('last_seen')}, dry_run={DRY_RUN}")

if __name__ == "__main__":
    main()

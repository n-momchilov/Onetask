"""One Thing — one inbox, one thing a day, calendar-first.

Run:   ONETHING_TOKEN=secret uvicorn app:app --host 0.0.0.0 --port 8000
Docs:  README.md (iPhone shortcuts, calendar feed, Outlook import, deployment)
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from icalendar import Calendar
from pydantic import BaseModel

# ---------------------------------------------------------------- config
BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ONETHING_DATA", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "onething.db"
TOKEN = os.environ.get("ONETHING_TOKEN", "change-me")
TZ = ZoneInfo(os.environ.get("ONETHING_TZ", "Asia/Seoul"))
ICS_URLS = [u.strip() for u in os.environ.get("ONETHING_ICS_URLS", "").split(",") if u.strip()]
ICS_KEYWORDS = [k.strip().lower() for k in os.environ.get("ONETHING_ICS_KEYWORDS", "").split(",") if k.strip()]
IMPORT_EVERY_HOURS = float(os.environ.get("ONETHING_IMPORT_HOURS", "6"))

KINDS = ("uni", "life", "side")
XP = {"uni": 30, "side": 20, "life": 10}   # per task done
DAY_BONUS = 20                              # every pick done + day closed
MAX_PICKS = 3
DEFAULT_WHY = {
    "uni": "Why uni matters — set it in Settings.",
    "life": "Why life admin matters — set it in Settings.",
    "side": "Why the side project matters — set it in Settings.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('uni','life','side')),
  due        TEXT,                       -- YYYY-MM-DD or NULL
  created_at TEXT NOT NULL,
  done_at    TEXT,
  source     TEXT NOT NULL DEFAULT 'capture',   -- capture | ics
  ext_uid    TEXT UNIQUE                 -- calendar UID for imported events
);
CREATE TABLE IF NOT EXISTS picks (
  day     TEXT NOT NULL,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY (day, task_id)
);
CREATE TABLE IF NOT EXISTS days (
  day           TEXT PRIMARY KEY,
  closed_at     TEXT,
  note          TEXT,
  bonus_awarded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS goals (kind TEXT PRIMARY KEY, why TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('xp', '0');
"""


# ---------------------------------------------------------------- helpers
def connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def get_xp(c: sqlite3.Connection) -> int:
    return int(c.execute("SELECT value FROM meta WHERE key='xp'").fetchone()["value"])


def add_xp(c: sqlite3.Connection, delta: int) -> int:
    xp = max(0, get_xp(c) + delta)
    c.execute("UPDATE meta SET value=? WHERE key='xp'", (str(xp),))
    return xp


def level_info(xp: int) -> tuple[int, int, int]:
    """Level, xp floor of this level, xp needed for next. Early levels come fast."""
    lvl = int((xp / 50) ** 0.5) + 1
    return lvl, 50 * (lvl - 1) ** 2, 50 * lvl**2


def task_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["done"] = d["done_at"] is not None
    return d


def day_stats(c: sqlite3.Connection, since: date) -> dict[str, tuple[int, int]]:
    """{day: (picks, done)} for every day that had picks."""
    rows = c.execute(
        """SELECT p.day, COUNT(*) AS picks, SUM(t.done_at IS NOT NULL) AS done
           FROM picks p JOIN tasks t ON t.id = p.task_id
           WHERE p.day >= ? GROUP BY p.day""",
        (iso(since),),
    ).fetchall()
    return {r["day"]: (r["picks"], r["done"] or 0) for r in rows}


def streak(stats: dict[str, tuple[int, int]], d: date) -> int:
    """Consecutive days (ending today or yesterday) with at least one pick done."""
    cur = d
    if stats.get(iso(cur), (0, 0))[1] < 1:
        cur -= timedelta(days=1)
    n = 0
    while stats.get(iso(cur), (0, 0))[1] >= 1:
        n += 1
        cur -= timedelta(days=1)
    return n


# ---------------------------------------------------------------- capture parsing
KIND_WORDS = {
    "uni": "uni", "u": "uni", "school": "uni", "study": "uni", "college": "uni", "class": "uni",
    "life": "life", "l": "life", "home": "life", "admin": "life",
    "side": "side", "s": "side", "project": "side", "build": "side",
}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_due(word: str, base: date) -> date | None:
    w = word.lower().strip(".,!?")
    if w in ("today", "tdy"):
        return base
    if w in ("tomorrow", "tmrw", "tmr"):
        return base + timedelta(days=1)
    for i, wd in enumerate(WEEKDAYS):
        if w.startswith(wd):
            return base + timedelta(days=(i - base.weekday()) % 7)
    try:
        return date.fromisoformat(w)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})", w)  # 20-10 or 20/10 (day-month)
    if m:
        dd, mm = int(m.group(1)), int(m.group(2))
        try:
            cand = date(base.year, mm, dd)
            return cand if cand >= base else date(base.year + 1, mm, dd)
        except ValueError:
            return None
    return None


def parse_capture(text: str, base: date) -> tuple[str, str, date | None]:
    """'uni: finish report due fri' -> ('finish report', 'uni', <date>). Default lane: life."""
    t = " ".join(text.split())
    kind = "life"
    m = re.match(r"^(\w+)\s*[:\-–]\s*(.+)$", t)
    if m and m.group(1).lower() in KIND_WORDS:
        kind, t = KIND_WORDS[m.group(1).lower()], m.group(2)
    else:
        m = re.match(r"^(uni|life|side|school|study|project)\s+(.+)$", t, re.I)
        if m:
            word = m.group(1).lower()
            kind = KIND_WORDS[word]
            if word in KINDS:  # 'uni finish x' → 'finish x'; 'study ch 4' keeps its verb
                t = m.group(2)
    due = None
    m = re.search(r"\s(?:@|due\s+|by\s+)(\S+)$", t, re.I)
    if m:
        due = parse_due(m.group(1), base)
        if due:
            t = t[: m.start()].rstrip()
    return t.strip(), kind, due


# ---------------------------------------------------------------- calendar import
def import_ics() -> dict:
    """Pull deadlines from the configured ICS feeds into the uni lane. Idempotent (by UID)."""
    stats: dict = {"fetched": 0, "imported": 0, "errors": []}
    cutoff = today() - timedelta(days=1)
    for url in ICS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "onething/1.0"})
            raw = urllib.request.urlopen(req, timeout=30).read()
            cal = Calendar.from_ical(raw)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"{url[:48]}…: {e}")
            continue
        with connect() as c:
            for ev in cal.walk("VEVENT"):
                summary = str(ev.get("SUMMARY", "")).strip() or "(untitled)"
                uid = str(ev.get("UID", "")) or f"{url}|{summary}"
                start = ev.get("DTSTART")
                if start is None:
                    continue
                dt = start.dt
                if isinstance(dt, datetime):
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=TZ)
                    due = dt.astimezone(TZ).date()
                else:
                    due = dt
                if due < cutoff:
                    continue
                if ICS_KEYWORDS and not any(k in summary.lower() for k in ICS_KEYWORDS):
                    continue
                stats["fetched"] += 1
                cur = c.execute(
                    """INSERT INTO tasks (title, kind, due, created_at, source, ext_uid)
                       VALUES (?, 'uni', ?, ?, 'ics', ?)
                       ON CONFLICT(ext_uid) DO UPDATE SET title = excluded.title, due = excluded.due
                       WHERE tasks.done_at IS NULL""",
                    (summary, iso(due), now().isoformat(), uid),
                )
                stats["imported"] += cur.rowcount
    return stats


async def import_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(import_ics)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(IMPORT_EVERY_HOURS * 3600)


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    task = asyncio.create_task(import_loop()) if ICS_URLS else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="One Thing", lifespan=lifespan, docs_url=None, redoc_url=None)


def auth(token: str | None = Query(default=None), x_token: str | None = Header(default=None)) -> None:
    if token != TOKEN and x_token != TOKEN:
        raise HTTPException(401, "bad token")


class CaptureIn(BaseModel):
    text: str


class PickIn(BaseModel):
    task_ids: list[int]


class CloseIn(BaseModel):
    note: str | None = None


class GoalIn(BaseModel):
    kind: str
    why: str


class TaskPatch(BaseModel):
    title: str | None = None
    kind: str | None = None
    due: str | None = None  # "" clears


def today_payload() -> dict:
    d = today()
    ds = iso(d)
    with connect() as c:
        picks = [
            task_dict(r)
            for r in c.execute(
                """SELECT t.* FROM picks p JOIN tasks t ON t.id = p.task_id WHERE p.day = ?
                   ORDER BY CASE t.kind WHEN 'uni' THEN 0 WHEN 'side' THEN 1 ELSE 2 END""",
                (ds,),
            )
        ]
        tasks = [
            task_dict(r)
            for r in c.execute(
                """SELECT * FROM tasks WHERE done_at IS NULL
                   ORDER BY CASE kind WHEN 'uni' THEN 0 WHEN 'life' THEN 1 ELSE 2 END,
                            (due IS NULL), due, created_at"""
            )
        ]
        goals_set = {r["kind"]: r["why"] for r in c.execute("SELECT * FROM goals")}
        goals = {k: goals_set.get(k) or DEFAULT_WHY[k] for k in KINDS}
        xp = get_xp(c)
        lvl, floor, nxt = level_info(xp)
        stats = day_stats(c, d - timedelta(days=90))
        day_row = c.execute("SELECT * FROM days WHERE day = ?", (ds,)).fetchone()
        wins = []
        for i in range(1, 8):
            k = iso(d - timedelta(days=i))
            p, dn = stats.get(k, (0, 0))
            row = c.execute("SELECT note FROM days WHERE day = ?", (k,)).fetchone()
            note = row["note"] if row else None
            if p or note:
                wins.append({"day": k, "picks": p, "done": dn, "note": note})
        due_soon = [
            task_dict(r)
            for r in c.execute(
                """SELECT * FROM tasks WHERE done_at IS NULL AND due IS NOT NULL AND due <= ?
                   ORDER BY due LIMIT 6""",
                (iso(d + timedelta(days=7)),),
            )
        ]
    return {
        "day": ds,
        "picks": picks,
        "tasks": tasks,
        "inbox_count": len(tasks),
        "goals": goals,
        "goals_set": goals_set,
        "xp": xp,
        "level": lvl,
        "level_floor": floor,
        "level_next": nxt,
        "streak": streak(stats, d),
        "closed": bool(day_row and day_row["closed_at"]),
        "note": day_row["note"] if day_row else None,
        "due_soon": due_soon,
        "wins": wins,
    }


@app.get("/", response_class=HTMLResponse)
def ui() -> str:
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/today", dependencies=[Depends(auth)])
def api_today() -> dict:
    return today_payload()


@app.post("/api/capture", dependencies=[Depends(auth)])
def api_capture(body: CaptureIn) -> dict:
    created = []
    with connect() as c:
        for line in body.text.splitlines():
            if not line.strip():
                continue
            title, kind, due = parse_capture(line, today())
            if not title:
                continue
            cur = c.execute(
                "INSERT INTO tasks (title, kind, due, created_at) VALUES (?, ?, ?, ?)",
                (title, kind, iso(due), now().isoformat()),
            )
            created.append({"id": cur.lastrowid, "title": title, "kind": kind, "due": iso(due)})
    return {"created": created}


@app.patch("/api/tasks/{tid}", dependencies=[Depends(auth)])
def api_patch(tid: int, body: TaskPatch) -> dict:
    with connect() as c:
        if not c.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone():
            raise HTTPException(404)
        if body.title is not None:
            c.execute("UPDATE tasks SET title = ? WHERE id = ?", (body.title.strip(), tid))
        if body.kind is not None:
            if body.kind not in KINDS:
                raise HTTPException(400, "kind must be uni|life|side")
            c.execute("UPDATE tasks SET kind = ? WHERE id = ?", (body.kind, tid))
        if body.due is not None:
            due = parse_due(body.due, today()) if body.due else None
            c.execute("UPDATE tasks SET due = ? WHERE id = ?", (iso(due), tid))
    return today_payload()


@app.delete("/api/tasks/{tid}", dependencies=[Depends(auth)])
def api_delete(tid: int) -> dict:
    with connect() as c:
        c.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    return today_payload()


@app.post("/api/tasks/{tid}/done", dependencies=[Depends(auth)])
def api_done(tid: int, undo: bool = False) -> dict:
    with connect() as c:
        t = c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404)
        if undo and t["done_at"]:
            c.execute("UPDATE tasks SET done_at = NULL WHERE id = ?", (tid,))
            add_xp(c, -XP[t["kind"]])
        elif not undo and not t["done_at"]:
            c.execute("UPDATE tasks SET done_at = ? WHERE id = ?", (now().isoformat(), tid))
            add_xp(c, XP[t["kind"]])
    return today_payload()


@app.post("/api/pick", dependencies=[Depends(auth)])
def api_pick(body: PickIn) -> dict:
    ds = iso(today())
    with connect() as c:
        c.execute("DELETE FROM picks WHERE day = ?", (ds,))
        for tid in body.task_ids[:MAX_PICKS]:
            c.execute("INSERT OR IGNORE INTO picks (day, task_id) VALUES (?, ?)", (ds, tid))
    return today_payload()


@app.post("/api/close", dependencies=[Depends(auth)])
def api_close(body: CloseIn) -> dict:
    d = today()
    ds = iso(d)
    bonus = 0
    with connect() as c:
        picks, done = day_stats(c, d).get(ds, (0, 0))
        row = c.execute("SELECT * FROM days WHERE day = ?", (ds,)).fetchone()
        if picks and done == picks and not (row and row["bonus_awarded"]):
            bonus = DAY_BONUS
            add_xp(c, bonus)
        note = (body.note or "").strip() or None
        c.execute(
            """INSERT INTO days (day, closed_at, note, bonus_awarded) VALUES (?, ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                 closed_at = excluded.closed_at,
                 note = COALESCE(excluded.note, days.note),
                 bonus_awarded = MAX(days.bonus_awarded, excluded.bonus_awarded)""",
            (ds, now().isoformat(), note, 1 if bonus else 0),
        )
    return {**today_payload(), "bonus": bonus}


@app.put("/api/goals", dependencies=[Depends(auth)])
def api_goals(body: GoalIn) -> dict:
    if body.kind not in KINDS:
        raise HTTPException(400, "kind must be uni|life|side")
    with connect() as c:
        if body.why.strip():
            c.execute(
                "INSERT INTO goals (kind, why) VALUES (?, ?) ON CONFLICT(kind) DO UPDATE SET why = excluded.why",
                (body.kind, body.why.strip()),
            )
        else:
            c.execute("DELETE FROM goals WHERE kind = ?", (body.kind,))
    return today_payload()


@app.post("/api/import", dependencies=[Depends(auth)])
def api_import() -> dict:
    if not ICS_URLS:
        return {"fetched": 0, "imported": 0, "errors": ["ONETHING_ICS_URLS not set"]}
    return import_ics()


# ---- surfaces for the phone: the pause alert and the calendar feed

@app.get("/today.txt", response_class=PlainTextResponse, dependencies=[Depends(auth)])
def today_txt() -> str:
    """One screen of text for the Shortcuts automation that fires when TikTok & co. open."""
    p = today_payload()
    tail = f"🔥 {p['streak']} · Lv {p['level']}"
    if not p["picks"]:
        return f"No pick for today yet.\n{tail}\nPick your one thing first?"
    lines = [f"{'✅' if t['done'] else '○'} {t['kind'].upper()}: {t['title']}" for t in p["picks"]]
    left = sum(1 for t in p["picks"] if not t["done"])
    verdict = "All done — scroll freely." if left == 0 else f"{left} left today."
    return "\n".join(lines) + f"\n{tail} · {verdict}"


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _vevent(uid: str, day: date, summary: str, description: str = "") -> list[str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}@onething",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
        f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


@app.get("/feed.ics", dependencies=[Depends(auth)])
def feed_ics() -> Response:
    """Subscribe to this in Apple Calendar / Outlook: today's picks, last 14 days, captured deadlines."""
    d = today()
    p = today_payload()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//onething//EN", "X-WR-CALNAME:One Thing", "CALSCALE:GREGORIAN"]
    status = f"🔥 {p['streak']}-day streak · Lv {p['level']} · {p['xp']} xp"
    if p["picks"]:
        for t in p["picks"]:
            mark = "✅" if t["done"] else "★"
            lines += _vevent(f"pick-{p['day']}-{t['id']}", d, f"{mark} {t['kind'].upper()}: {t['title']}", f"{p['goals'][t['kind']]}\n{status}")
    else:
        lines += _vevent(f"nopick-{p['day']}", d, "★ Pick today's one thing", status)
    with connect() as c:
        hist = c.execute(
            """SELECT p.day, t.kind, t.title, t.id, t.done_at FROM picks p JOIN tasks t ON t.id = p.task_id
               WHERE p.day < ? AND p.day >= ? ORDER BY p.day""",
            (iso(d), iso(d - timedelta(days=14))),
        ).fetchall()
        notes = {r["day"]: r["note"] for r in c.execute("SELECT day, note FROM days WHERE note IS NOT NULL")}
        for r in hist:
            mark = "✅" if r["done_at"] else "—"
            lines += _vevent(f"pick-{r['day']}-{r['id']}", date.fromisoformat(r["day"]), f"{mark} {r['kind'].upper()}: {r['title']}", notes.get(r["day"]) or "")
        upcoming = c.execute(
            """SELECT * FROM tasks WHERE done_at IS NULL AND source = 'capture' AND due IS NOT NULL
               AND due > ? AND due <= ? ORDER BY due""",
            (iso(d), iso(d + timedelta(days=30))),
        ).fetchall()
        for t in upcoming:
            lines += _vevent(f"due-{t['id']}", date.fromisoformat(t["due"]), f"⏰ {t['kind'].upper()}: {t['title']}")
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8")

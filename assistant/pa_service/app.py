"""Personal-assistant service: tasks, quick-capture inbox, and the daily agenda.

The database is the source of truth. Hermes only confirms an action after this service saved it.
Natural-language times ("tomorrow 10am", "in 30 minutes", "by Sunday") are parsed here, in code,
in the user's time zone, never by the language model.

Runs on VM 1 (FastAPI + SQLite on /data). All endpoints except /health need the bearer token.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import dateparser
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("PA_ROOT", "/data/pa"))
DB_PATH = ROOT / "pa.db"
TOKEN = (ROOT / "token").read_text().strip()
TZ = ZoneInfo(os.environ.get("PA_TIMEZONE", "Asia/Kolkata"))
PRIORITIES = ("low", "medium", "high", "urgent")
CATEGORIES = ("college", "internship", "research", "personal", "career", "finance", "projects", "other")
_lock = threading.Lock()


# ---------- time parsing (code, not the LLM) ----------
_TIME_HINT = re.compile(
    r"\b(\d{1,2}(:\d{2})?\s*(am|pm)|\d{1,2}:\d{2}|noon|midnight|morning|afternoon|evening|tonight|night|"
    r"in\s+\d+\s*(min|mins|minute|minutes|hour|hours|hr|hrs)|now)\b", re.I)
_WORD_TIMES = [(r"\bmorning\b", "9:00"), (r"\bafternoon\b", "14:00"), (r"\bevening\b", "18:00"),
               (r"\btonight\b", "today 21:00"), (r"\bnight\b", "21:00"), (r"\bnoon\b", "12:00"),
               (r"\bmidnight\b", "23:59")]


def now_local() -> datetime:
    return datetime.now(TZ)


def parse_when(text: str) -> tuple[datetime, bool]:
    """Return (aware datetime in TZ, has_time). Dates without a time mean end of that day.
    Raises ValueError if the text can't be understood or is in the past."""
    raw = text.strip()
    s = re.sub(r"^(by|on|at|before|due|for)\s+", "", raw, flags=re.I)
    has_time = bool(_TIME_HINT.search(s))
    for pat, rep in _WORD_TIMES:
        s = re.sub(pat, rep, s, flags=re.I)
    now = now_local()
    dt = dateparser.parse(s, settings={
        "TIMEZONE": str(TZ), "TO_TIMEZONE": str(TZ), "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future", "RELATIVE_BASE": now.replace(tzinfo=None)})
    if dt is None:
        raise ValueError(f"could not understand the time '{raw}'")
    dt = dt.astimezone(TZ)
    if not has_time:
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)
    if dt < now - timedelta(minutes=1):
        raise ValueError(f"'{raw}' is in the past ({fmt(dt)})")
    return dt, has_time


def fmt(dt: Optional[datetime], has_time: bool = True) -> str:
    if dt is None:
        return ""
    dt = dt.astimezone(TZ)
    day = dt.strftime("%a %d %b %Y")
    return f"{day} {dt.strftime('%H:%M')}" if has_time else day


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


# ---------- storage ----------
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with _lock, db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'other', priority TEXT NOT NULL DEFAULT 'medium',
            due_at TEXT, due_has_time INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
            parent_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, done_at TEXT);
        CREATE INDEX IF NOT EXISTS tasks_status_due ON tasks(status, due_at);
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'note',
            project TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','processed','archived')),
            task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL, created_at TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS inbox_fts USING fts5(text, project, tags, content='inbox', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS inbox_ai AFTER INSERT ON inbox BEGIN
            INSERT INTO inbox_fts(rowid, text, project, tags) VALUES (new.id, new.text, new.project, new.tags); END;
        CREATE TRIGGER IF NOT EXISTS inbox_au AFTER UPDATE ON inbox BEGIN
            INSERT INTO inbox_fts(inbox_fts, rowid, text, project, tags) VALUES ('delete', old.id, old.text, old.project, old.tags);
            INSERT INTO inbox_fts(rowid, text, project, tags) VALUES (new.id, new.text, new.project, new.tags); END;
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, schedule TEXT NOT NULL DEFAULT 'daily'
              CHECK (schedule IN ('daily','weekdays','weekly')),
            per_week INTEGER NOT NULL DEFAULT 7, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS checkins (
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE, day TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, PRIMARY KEY (habit_id, day));
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY, amount REAL NOT NULL CHECK (amount > 0), currency TEXT NOT NULL DEFAULT 'INR',
            category TEXT NOT NULL DEFAULT 'other', note TEXT NOT NULL DEFAULT '', spent_on TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS expenses_day ON expenses(spent_on);
        """)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_out(r: sqlite3.Row) -> dict:
    due = from_iso(r["due_at"])
    out = {k: r[k] for k in ("id", "title", "notes", "category", "priority", "status", "parent_id")}
    out["due"] = fmt(due, bool(r["due_has_time"])) if due else None
    out["due_at_utc"] = r["due_at"]
    out["overdue"] = bool(due and r["status"] == "open" and due < datetime.now(timezone.utc))
    with db() as c:
        out["subtasks"] = [{"id": s["id"], "title": s["title"], "status": s["status"]}
                           for s in c.execute("SELECT id,title,status FROM tasks WHERE parent_id=? ORDER BY id", (r["id"],))]
    return out


def get_task(tid: int) -> sqlite3.Row:
    with db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        raise HTTPException(404, f"task {tid} not found")
    return r


# ---------- API ----------
def auth(authorization: str = Header(default="")) -> None:
    got = authorization.removeprefix("Bearer ").strip()
    if not got or not hmac.compare_digest(got.encode(), TOKEN.encode()):
        raise HTTPException(401, "invalid or missing bearer token")


app = FastAPI(title="Personal assistant", version="1.0")
A = [Depends(auth)]


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0]
    return {"ok": True, "timezone": str(TZ), "local_time": fmt(now_local()), "open_tasks": n}


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)


@app.post("/parse_time", dependencies=A)
def parse_time(body: ParseIn) -> dict:
    try:
        dt, has_time = parse_when(body.text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"input": body.text, "local": fmt(dt, has_time), "utc": to_utc_iso(dt), "has_time": has_time}


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    category: str = "other"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    due: Optional[str] = Field(default=None, max_length=200, description="natural language or ISO time")
    notes: str = Field(default="", max_length=4000)
    parent_id: Optional[int] = None


def norm_category(c: str) -> str:
    c = (c or "other").strip().lower()
    return c if c in CATEGORIES else "other"


@app.post("/tasks", status_code=201, dependencies=A)
def add_task(body: TaskIn) -> dict:
    due_at, has_time = None, False
    if body.due:
        try:
            dt, has_time = parse_when(body.due)
        except ValueError as e:
            raise HTTPException(422, str(e))
        due_at = to_utc_iso(dt)
    if body.parent_id:
        get_task(body.parent_id)
    ts = now_iso()
    with _lock, db() as c:
        cur = c.execute(
            "INSERT INTO tasks (title, notes, category, priority, due_at, due_has_time, parent_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (body.title.strip(), body.notes, norm_category(body.category), body.priority, due_at, int(has_time),
             body.parent_id, ts, ts))
        tid = cur.lastrowid
    return task_out(get_task(tid))


@app.get("/tasks", dependencies=A)
def list_tasks(status: Literal["open", "done", "cancelled", "all"] = "open", category: Optional[str] = None,
               priority: Optional[str] = None, overdue: bool = False, due_within_days: Optional[int] = Query(None, ge=0, le=365),
               q: Optional[str] = None, limit: int = Query(50, ge=1, le=200)) -> dict:
    sql, args = "SELECT * FROM tasks WHERE 1=1", []
    if status != "all":
        sql += " AND status=?"; args.append(status)
    if category:
        sql += " AND category=?"; args.append(norm_category(category))
    if priority:
        sql += " AND priority=?"; args.append(priority)
    if overdue:
        sql += " AND due_at IS NOT NULL AND due_at < ?"; args.append(now_iso())
    if due_within_days is not None:
        sql += " AND due_at IS NOT NULL AND due_at <= ?"
        args.append(to_utc_iso(now_local().replace(hour=23, minute=59) + timedelta(days=due_within_days)))
    if q:
        sql += " AND (title LIKE ? OR notes LIKE ?)"; args += [f"%{q}%", f"%{q}%"]
    sql += (" ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,"
            " due_at IS NULL, due_at, id LIMIT ?")
    args.append(limit)
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return {"count": len(rows), "tasks": [task_out(r) for r in rows]}


class TaskPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    category: Optional[str] = None
    priority: Optional[Literal["low", "medium", "high", "urgent"]] = None
    due: Optional[str] = Field(default=None, max_length=200, description="new due time; 'none' clears it")
    notes: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[Literal["open", "done", "cancelled"]] = None


@app.patch("/tasks/{tid}", dependencies=A)
def update_task(tid: int, body: TaskPatch) -> dict:
    get_task(tid)
    sets, args = [], []
    if body.title is not None:
        sets.append("title=?"); args.append(body.title.strip())
    if body.category is not None:
        sets.append("category=?"); args.append(norm_category(body.category))
    if body.priority is not None:
        sets.append("priority=?"); args.append(body.priority)
    if body.notes is not None:
        sets.append("notes=?"); args.append(body.notes)
    if body.due is not None:
        if body.due.strip().lower() in ("none", "no deadline", "clear", ""):
            sets += ["due_at=NULL", "due_has_time=0"]
        else:
            try:
                dt, has_time = parse_when(body.due)
            except ValueError as e:
                raise HTTPException(422, str(e))
            sets += ["due_at=?", "due_has_time=?"]; args += [to_utc_iso(dt), int(has_time)]
    if body.status is not None:
        sets.append("status=?"); args.append(body.status)
        sets.append("done_at=?"); args.append(now_iso() if body.status == "done" else None)
    if not sets:
        raise HTTPException(400, "nothing to change")
    sets.append("updated_at=?"); args.append(now_iso())
    with _lock, db() as c:
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, tid))
    return task_out(get_task(tid))


@app.post("/tasks/{tid}/done", dependencies=A)
def complete_task(tid: int) -> dict:
    return update_task(tid, TaskPatch(status="done"))


@app.delete("/tasks/{tid}", dependencies=A)
def delete_task(tid: int) -> dict:
    return update_task(tid, TaskPatch(status="cancelled"))


class InboxIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    kind: Literal["idea", "note", "link", "thought", "snippet"] = "note"
    project: str = Field(default="", max_length=100)
    tags: str = Field(default="", max_length=200)


def inbox_out(r: sqlite3.Row) -> dict:
    out = dict(r)
    out["created"] = fmt(from_iso(r["created_at"]))
    return out


@app.post("/inbox", status_code=201, dependencies=A)
def add_inbox(body: InboxIn) -> dict:
    with _lock, db() as c:
        cur = c.execute("INSERT INTO inbox (text, kind, project, tags, created_at) VALUES (?,?,?,?,?)",
                        (body.text.strip(), body.kind, body.project.strip().lower(), body.tags.strip().lower(), now_iso()))
        r = c.execute("SELECT * FROM inbox WHERE id=?", (cur.lastrowid,)).fetchone()
    return inbox_out(r)


@app.get("/inbox", dependencies=A)
def list_inbox(q: Optional[str] = None, kind: Optional[str] = None, project: Optional[str] = None,
               status: Literal["new", "processed", "archived", "all"] = "all", limit: int = Query(20, ge=1, le=200)) -> dict:
    args: list = []
    if q:
        terms = " ".join(f'"{t}"' for t in re.findall(r"\w+", q))
        sql = "SELECT inbox.* FROM inbox JOIN inbox_fts ON inbox_fts.rowid=inbox.id WHERE inbox_fts MATCH ?"
        args.append(terms or '""')
    else:
        sql = "SELECT * FROM inbox WHERE 1=1"
    if kind:
        sql += " AND inbox.kind=?"; args.append(kind)
    if project:
        sql += " AND inbox.project=?"; args.append(project.strip().lower())
    if status != "all":
        sql += " AND inbox.status=?"; args.append(status)
    sql += " ORDER BY inbox.id DESC LIMIT ?"; args.append(limit)
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return {"count": len(rows), "items": [inbox_out(r) for r in rows]}


class ToTaskIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    category: str = "other"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    due: Optional[str] = None


@app.post("/inbox/{iid}/to_task", dependencies=A)
def inbox_to_task(iid: int, body: ToTaskIn) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone()
    if not r:
        raise HTTPException(404, f"inbox item {iid} not found")
    task = add_task(TaskIn(title=body.title or r["text"][:300], category=body.category, priority=body.priority,
                           due=body.due, notes=r["text"] if len(r["text"]) > 300 or body.title else ""))
    with _lock, db() as c:
        c.execute("UPDATE inbox SET status='processed', task_id=? WHERE id=?", (task["id"], iid))
    return {"task": task, "inbox_id": iid}


# ---------- reminders (stored and sent by the always-on Cloudflare worker) ----------
WORKER_CONF = ROOT / "worker.json"  # {"url": "...", "secret": "..."}  (0600)
REPEATS = ("none", "daily", "weekdays", "weekly", "monthly")


def worker(method: str, path: str, body: Optional[dict] = None) -> dict:
    try:
        conf = json.loads(WORKER_CONF.read_text())
    except (OSError, ValueError):
        raise HTTPException(503, "reminder worker is not configured yet")
    req = urllib.request.Request(conf["url"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {conf['secret']}", "Content-Type": "application/json",
                                          "User-Agent": "pa-service/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error")
        except Exception:
            detail = e.reason
        raise HTTPException(e.code if e.code < 500 else 502, f"reminder worker: {detail}")
    except OSError as e:
        raise HTTPException(503, f"reminder worker unreachable: {e}")


class ReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    when: Optional[str] = Field(default=None, max_length=200, description="e.g. 'tomorrow 8pm', 'in 30 minutes'")
    repeat: Literal["none", "daily", "weekdays", "weekly", "monthly"] = "none"
    before_task_id: Optional[int] = None
    before: Optional[str] = Field(default=None, max_length=50, description="e.g. '3 days', '2 hours' before the task due")


_OFFSET = re.compile(r"^\s*(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs|day|days|week|weeks)\s*$", re.I)


def _offset(text: str) -> timedelta:
    m = _OFFSET.match(text or "")
    if not m:
        raise HTTPException(422, f"could not understand the offset '{text}' (use e.g. '3 days', '2 hours')")
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit[0]]


@app.post("/reminders", status_code=201, dependencies=A)
def add_reminder(body: ReminderIn) -> dict:
    if body.before_task_id:
        t = get_task(body.before_task_id)
        if not t["due_at"]:
            raise HTTPException(422, f"task {t['id']} has no deadline to count back from")
        due = from_iso(t["due_at"]).astimezone(TZ)
        if not t["due_has_time"]:
            due = due.replace(hour=9, minute=0)  # date-only deadline: remind at 09:00 that day
        dt = due - _offset(body.before or "1 day")
        if dt < now_local():
            raise HTTPException(422, f"that would be in the past ({fmt(dt)})")
    elif body.when:
        try:
            dt, has_time = parse_when(body.when)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not has_time:
            dt = dt.replace(hour=9, minute=0)  # "remind me tomorrow" -> 09:00
            if dt < now_local():
                raise HTTPException(422, f"that would be in the past ({fmt(dt)}); give a time")
    else:
        raise HTTPException(422, "give either 'when' or 'before_task_id'")
    off = int(dt.utcoffset().total_seconds() // 60)
    return worker("POST", "/reminders", {"text": body.text.strip(), "due_utc": to_utc_iso(dt), "repeat": body.repeat,
                                         "tz_offset_min": off})


@app.get("/reminders", dependencies=A)
def list_reminders(status: Literal["pending", "sent", "cancelled", "failed", "all"] = "pending",
                   limit: int = Query(50, ge=1, le=200)) -> dict:
    return worker("GET", f"/reminders?status={status}&limit={limit}")


@app.post("/reminders/{rid}/cancel", dependencies=A)
def cancel_reminder(rid: str) -> dict:
    if not re.fullmatch(r"r[0-9a-f]{8}", rid):
        raise HTTPException(400, "invalid reminder id")
    return worker("POST", f"/reminders/{rid}/cancel")


class SnoozeIn(BaseModel):
    minutes: int = Field(default=10, ge=1, le=10080)


@app.post("/reminders/{rid}/snooze", dependencies=A)
def snooze_reminder(rid: str, body: SnoozeIn) -> dict:
    if not re.fullmatch(r"r[0-9a-f]{8}", rid):
        raise HTTPException(400, "invalid reminder id")
    return worker("POST", f"/reminders/{rid}/snooze", {"minutes": body.minutes})


# ---------- morning briefing snapshot (pushed to the worker, which sends it on time) ----------
BRIEFING_TIME = os.environ.get("PA_BRIEFING_TIME", "08:00")


def briefing_payload() -> dict:
    """Raw open tasks; the worker buckets them (overdue / today / upcoming) at send time,
    so a snapshot pushed yesterday is still sorted correctly this morning."""
    with db() as c:
        rows = c.execute("SELECT title, priority, due_at, due_has_time FROM tasks WHERE status='open' "
                         "ORDER BY due_at IS NULL, due_at LIMIT 60").fetchall()
        inbox_new = c.execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]
    tasks = [{"title": r["title"][:120], "priority": r["priority"], "has_time": bool(r["due_has_time"]),
              "due_ms": int(from_iso(r["due_at"]).timestamp() * 1000) if r["due_at"] else None} for r in rows]
    with db() as c:
        hrows = c.execute("SELECT * FROM habits WHERE active=1 ORDER BY id").fetchall()
    habits = [{"name": s["name"][:60], "schedule": s["schedule"], "streak": s["streak"], "unit": s["streak_unit"]}
              for s in map(_habit_stats, hrows)]
    return {"tasks": tasks, "habits": habits, "inbox_new": inbox_new, "time_local": BRIEFING_TIME,
            "tz_offset_min": int(now_local().utcoffset().total_seconds() // 60)}


def push_briefing() -> None:
    """Best effort: refresh the data the worker turns into the morning briefing."""
    try:
        worker("PUT", "/briefing", briefing_payload())
    except HTTPException:
        pass


@app.middleware("http")
async def refresh_briefing_after_changes(request, call_next):
    response = await call_next(request)
    if request.method in ("POST", "PATCH", "DELETE") and request.url.path.startswith(("/tasks", "/inbox", "/habits")) \
            and response.status_code < 400:
        threading.Thread(target=push_briefing, daemon=True).start()
    return response


@app.post("/briefing/push", dependencies=A)
def briefing_push_now() -> dict:
    payload = briefing_payload()
    worker("PUT", "/briefing", payload)
    return {"ok": True, "time_local": BRIEFING_TIME, "open_tasks": len(payload["tasks"])}


# ---------- habits (repeat; a task is done once) ----------
from datetime import date as _date  # noqa: E402


def today_local() -> _date:
    return now_local().date()


def _day_arg(text: Optional[str]) -> _date:
    t = (text or "today").strip().lower()
    if t == "today":
        return today_local()
    if t == "yesterday":
        return today_local() - timedelta(days=1)
    try:
        d = _date.fromisoformat(t)
    except ValueError:
        raise HTTPException(422, "day must be 'today', 'yesterday' or YYYY-MM-DD")
    if d > today_local():
        raise HTTPException(422, "can't check in for a future day")
    return d


def _habit_stats(h: sqlite3.Row) -> dict:
    with db() as c:
        days = {_date.fromisoformat(r[0]) for r in c.execute("SELECT day FROM checkins WHERE habit_id=?", (h["id"],))}
    today = today_local()
    week_start = today - timedelta(days=today.weekday())  # Monday
    this_week = sum(1 for d in days if week_start <= d <= today)
    streak = 0
    if h["schedule"] == "weekly":
        # consecutive weeks (before this one, plus this one if already met) reaching per_week
        ws = week_start if this_week >= h["per_week"] else week_start - timedelta(days=7)
        while sum(1 for d in days if ws <= d < ws + timedelta(days=7)) >= h["per_week"]:
            streak += 1; ws -= timedelta(days=7)
        unit = "weeks"
    else:
        due = (lambda d: d.weekday() < 5) if h["schedule"] == "weekdays" else (lambda d: True)
        d = today if today in days else today - timedelta(days=1)
        while True:
            if not due(d):
                d -= timedelta(days=1); continue
            if d in days:
                streak += 1; d -= timedelta(days=1)
            else:
                break
        unit = "days"
    due_today = h["schedule"] == "daily" or (h["schedule"] == "weekdays" and today.weekday() < 5) or \
        (h["schedule"] == "weekly" and this_week < h["per_week"])
    return {"id": h["id"], "name": h["name"], "schedule": h["schedule"], "per_week": h["per_week"],
            "done_today": today in days, "due_today": due_today and today not in days, "streak": streak,
            "streak_unit": unit, "this_week": this_week, "total": len(days)}


class HabitIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    schedule: Literal["daily", "weekdays", "weekly"] = "daily"
    per_week: int = Field(default=7, ge=1, le=7, description="target per week (used when schedule='weekly')")
    how_often: Optional[str] = Field(default=None, max_length=60,
                                     description="the user's words, e.g. '5 times a week'; overrides schedule/per_week")


_WORDS = {"once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def parse_frequency(text: str) -> tuple[str, int]:
    """'every day' -> daily; 'weekdays' -> weekdays; '5 times a week' / '3x per week' / 'twice a week' -> weekly n."""
    t = text.strip().lower()
    m = re.search(r"\b(\d|once|twice|thrice|one|two|three|four|five|six|seven)\s*(x|times?)?\s*(a|per|/|every|each)\s*week", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _WORDS[m.group(1)]
        if not 1 <= n <= 7:
            raise HTTPException(422, f"'{text}': times per week must be 1-7")
        return ("daily", 7) if n == 7 else ("weekly", n)
    if re.search(r"week ?days?|mon(day)?\s*(-|to)\s*fri", t):
        return "weekdays", 5
    if re.search(r"\b(weekly|once a week|every week)\b", t):
        return "weekly", 1
    if re.search(r"\b(daily|every ?day|each day|everyday)\b", t):
        return "daily", 7
    raise HTTPException(422, f"could not understand how often: '{text}' (try 'daily', 'weekdays', '3 times a week')")


@app.post("/habits", status_code=201, dependencies=A)
def add_habit(body: HabitIn) -> dict:
    if body.how_often:
        sched, n = parse_frequency(body.how_often)
        body = body.model_copy(update={"schedule": sched, "per_week": n})
    with _lock, db() as c:
        dup = c.execute("SELECT id FROM habits WHERE active=1 AND lower(name)=lower(?)", (body.name.strip(),)).fetchone()
        if dup:
            raise HTTPException(409, f"habit '{body.name}' already exists (id {dup[0]})")
        per_week = body.per_week if body.schedule == "weekly" else (5 if body.schedule == "weekdays" else 7)
        cur = c.execute("INSERT INTO habits (name, schedule, per_week, created_at) VALUES (?,?,?,?)",
                        (body.name.strip(), body.schedule, per_week, now_iso()))
        h = c.execute("SELECT * FROM habits WHERE id=?", (cur.lastrowid,)).fetchone()
    return _habit_stats(h)


def _get_habit(hid: int) -> sqlite3.Row:
    with db() as c:
        h = c.execute("SELECT * FROM habits WHERE id=? AND active=1", (hid,)).fetchone()
    if not h:
        raise HTTPException(404, f"habit {hid} not found")
    return h


@app.get("/habits", dependencies=A)
def list_habits() -> dict:
    with db() as c:
        rows = c.execute("SELECT * FROM habits WHERE active=1 ORDER BY id").fetchall()
    return {"count": len(rows), "habits": [_habit_stats(h) for h in rows]}


class CheckinIn(BaseModel):
    day: Optional[str] = Field(default="today", description="'today', 'yesterday' or YYYY-MM-DD")
    note: str = Field(default="", max_length=300)


@app.post("/habits/{hid}/checkin", dependencies=A)
def checkin(hid: int, body: CheckinIn = CheckinIn()) -> dict:
    h = _get_habit(hid)
    d = _day_arg(body.day)
    with _lock, db() as c:
        cur = c.execute("INSERT OR IGNORE INTO checkins (habit_id, day, note, created_at) VALUES (?,?,?,?)",
                        (hid, d.isoformat(), body.note, now_iso()))
    out = _habit_stats(h)
    out["already_checked_in"] = cur.rowcount == 0
    out["checked_day"] = d.isoformat()
    return out


@app.delete("/habits/{hid}", dependencies=A)
def remove_habit(hid: int) -> dict:
    _get_habit(hid)
    with _lock, db() as c:
        c.execute("UPDATE habits SET active=0 WHERE id=?", (hid,))
    return {"ok": True, "removed": hid}


# ---------- expenses (a simple log; no bank connections) ----------
EXPENSE_CATEGORIES = ("food", "transport", "shopping", "bills", "education", "health", "entertainment", "rent", "other")


class ExpenseIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000)
    category: str = "other"
    note: str = Field(default="", max_length=300)
    day: Optional[str] = Field(default="today", description="'today', 'yesterday' or YYYY-MM-DD")
    currency: str = Field(default="INR", max_length=3)


@app.post("/expenses", status_code=201, dependencies=A)
def add_expense(body: ExpenseIn) -> dict:
    cat = body.category.strip().lower()
    cat = cat if cat in EXPENSE_CATEGORIES else "other"
    d = _day_arg(body.day)
    with _lock, db() as c:
        cur = c.execute("INSERT INTO expenses (amount, currency, category, note, spent_on, created_at) VALUES (?,?,?,?,?,?)",
                        (round(body.amount, 2), body.currency.upper(), cat, body.note.strip(), d.isoformat(), now_iso()))
        r = c.execute("SELECT * FROM expenses WHERE id=?", (cur.lastrowid,)).fetchone()
    out = dict(r)
    out["month_total"] = expense_summary("month")["total"]
    return out


@app.get("/expenses", dependencies=A)
def list_expenses(days: int = Query(30, ge=1, le=366), category: Optional[str] = None,
                  limit: int = Query(50, ge=1, le=500)) -> dict:
    since = (today_local() - timedelta(days=days - 1)).isoformat()
    sql, args = "SELECT * FROM expenses WHERE spent_on >= ?", [since]
    if category:
        sql += " AND category=?"; args.append(category.lower())
    sql += " ORDER BY spent_on DESC, id DESC LIMIT ?"; args.append(limit)
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    return {"count": len(rows), "expenses": rows}


@app.delete("/expenses/{eid}", dependencies=A)
def delete_expense(eid: int) -> dict:
    with _lock, db() as c:
        n = c.execute("DELETE FROM expenses WHERE id=?", (eid,)).rowcount
    if not n:
        raise HTTPException(404, f"expense {eid} not found")
    return {"ok": True, "deleted": eid}


@app.get("/expenses/summary", dependencies=A)
def expense_summary(period: Literal["week", "month"] = "month") -> dict:
    today = today_local()
    if period == "week":
        start = today - timedelta(days=today.weekday())
        prev_start, prev_end = start - timedelta(days=7), start - timedelta(days=1)
    else:
        start = today.replace(day=1)
        prev_end = start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
    with db() as c:
        cats = c.execute("SELECT category, ROUND(SUM(amount),2), COUNT(*) FROM expenses WHERE spent_on BETWEEN ? AND ? "
                         "GROUP BY category ORDER BY 2 DESC", (start.isoformat(), today.isoformat())).fetchall()
        prev = c.execute("SELECT ROUND(COALESCE(SUM(amount),0),2) FROM expenses WHERE spent_on BETWEEN ? AND ?",
                         (prev_start.isoformat(), prev_end.isoformat())).fetchone()[0]
    total = round(sum(r[1] for r in cats), 2)
    return {"period": period, "from": start.isoformat(), "to": today.isoformat(), "total": total, "currency": "INR",
            "by_category": [{"category": r[0], "total": r[1], "count": r[2]} for r in cats], "previous_period_total": prev}


# ---------- system health (for the laptop dashboard) ----------
def _probe(url: str, timeout: float = 5.0) -> dict:
    import time as _t
    t = _t.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pa-service/1.0"}), timeout=timeout) as r:
            body = r.read(400).decode(errors="replace")
            return {"ok": r.status == 200, "ms": int((_t.time() - t) * 1000), "detail": body[:200]}
    except Exception as e:
        return {"ok": False, "ms": int((_t.time() - t) * 1000), "detail": type(e).__name__}


def _gateway() -> dict:
    import subprocess
    unit = os.environ.get("HERMES_GATEWAY_UNIT", "HERMES_GATEWAY_UNIT")
    active = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout.strip()
    since = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveEnterTimestamp", "--value", unit],
                           capture_output=True, text=True).stdout.strip()
    tg = subprocess.run(["journalctl", "--user", "-u", unit, "-b", "--no-pager", "-q", "-g", "Connected to Telegram"],
                        capture_output=True, text=True).stdout.count("Connected to Telegram")
    return {"ok": active == "active" and tg > 0, "active": active, "since": since, "telegram_connects": tg}


@app.get("/system", dependencies=A)
def system() -> dict:
    from concurrent.futures import ThreadPoolExecutor
    try:
        worker_url = json.loads(WORKER_CONF.read_text())["url"].rstrip("/") + "/health"
    except (OSError, ValueError, KeyError):
        worker_url = None
    checks = {
        "hermes_gateway": _gateway,
        "dsh_bridge": lambda: _probe(os.environ.get("DSH_BRIDGE_URL", "http://VM2_TAILSCALE_IP:8000") + "/health"),
        "freellmapi": lambda: _probe(os.environ.get("FREELLMAPI_URL", "http://VM2_TAILSCALE_IP:3001") + "/api/ping"),
        "laptop_ollama": lambda: _probe(os.environ.get("LAPTOP_OLLAMA_URL", "http://LAPTOP_TAILSCALE_IP:11434") + "/api/version", 3),
        "reminder_worker": (lambda: _probe(worker_url, 8)) if worker_url else (lambda: {"ok": False, "detail": "not configured"}),
    }
    with ThreadPoolExecutor(max_workers=len(checks)) as ex:
        futs = {k: ex.submit(f) for k, f in checks.items()}
        out = {k: f.result() for k, f in futs.items()}
    st = os.statvfs("/data")
    out["disk_data"] = {"ok": st.f_bavail / st.f_blocks > 0.1,
                        "free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1), "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1)}
    with db() as c:
        out["counts"] = {"open_tasks": c.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0],
                         "overdue": c.execute("SELECT COUNT(*) FROM tasks WHERE status='open' AND due_at < ?", (now_iso(),)).fetchone()[0],
                         "inbox_new": c.execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]}
    out["local_time"] = fmt(now_local())
    return out


@app.get("/agenda", dependencies=A)
def agenda(days: int = Query(7, ge=1, le=31)) -> dict:
    now = now_local()
    end_today = now.replace(hour=23, minute=59, second=59)
    horizon = end_today + timedelta(days=days)
    with db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE status='open' ORDER BY due_at IS NULL, due_at").fetchall()
        new_inbox = c.execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]
        done_today = c.execute("SELECT COUNT(*) FROM tasks WHERE status='done' AND done_at >= ?",
                               (to_utc_iso(now.replace(hour=0, minute=0, second=0)),)).fetchone()[0]
    overdue, today, upcoming, undated = [], [], [], []
    for r in rows:
        due = from_iso(r["due_at"])
        item = task_out(r)
        if due is None:
            undated.append(item)
        elif due < now:
            overdue.append(item)
        elif due <= end_today:
            today.append(item)
        elif due <= horizon:
            upcoming.append(item)
    rank = {p: i for i, p in enumerate(reversed(PRIORITIES))}
    undated.sort(key=lambda t: rank[t["priority"]])
    return {"now": fmt(now), "overdue": overdue, "today": today, f"next_{days}_days": upcoming,
            "no_deadline_top": undated[:5], "inbox_new": new_inbox, "done_today": done_today}

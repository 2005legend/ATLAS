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
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','done')),
            deadline_at TEXT, created_at TEXT NOT NULL);
        """)
        # migrations for databases created before projects existed
        cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        if "project_id" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL")
        if "milestone" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN milestone INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id)")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS followups (
            id INTEGER PRIMARY KEY, person TEXT NOT NULL, about TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '',
            since_day TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open'
              CHECK (status IN ('open','done','cancelled')),
            reminder_id TEXT, created_at TEXT NOT NULL, done_at TEXT);
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY, company TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'applied'
              CHECK (status IN ('saved','applied','interview','offer','rejected','withdrawn')),
            applied_on TEXT, contact TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL DEFAULT '', next_at TEXT,
            notes TEXT NOT NULL DEFAULT '', reminder_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_events (
            app_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE, from_status TEXT, to_status TEXT NOT NULL,
            at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS focus_sessions (
            id INTEGER PRIMARY KEY, topic TEXT NOT NULL, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
            planned_min INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','stopped')),
            distractions INTEGER NOT NULL DEFAULT 0, reminder_id TEXT);
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL UNIQUE, target_min_week INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done')), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS study_log (
            id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            minutes INTEGER NOT NULL CHECK (minutes > 0), day TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS modules (
            id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            title TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, done_at TEXT);
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, topic TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
            project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL, interval_days INTEGER NOT NULL DEFAULT 3,
            due_on TEXT NOT NULL, reviews INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0, last_review TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')), created_at TEXT NOT NULL);
        """)
        if "reminder_id" not in {r[1] for r in c.execute("PRAGMA table_info(inbox)")}:
            c.execute("ALTER TABLE inbox ADD COLUMN reminder_id TEXT")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_out(r: sqlite3.Row) -> dict:
    due = from_iso(r["due_at"])
    out = {k: r[k] for k in ("id", "title", "notes", "category", "priority", "status", "parent_id")}
    out["due"] = fmt(due, bool(r["due_has_time"])) if due else None
    out["due_at_utc"] = r["due_at"]
    out["overdue"] = bool(due and r["status"] == "open" and due < datetime.now(timezone.utc))
    out["milestone"] = bool(r["milestone"])
    with db() as c:
        out["subtasks"] = [{"id": s["id"], "title": s["title"], "status": s["status"]}
                           for s in c.execute("SELECT id,title,status FROM tasks WHERE parent_id=? ORDER BY id", (r["id"],))]
        p = c.execute("SELECT name FROM projects WHERE id=?", (r["project_id"],)).fetchone() if r["project_id"] else None
    out["project"] = p[0] if p else None
    return out


def project_key(name: str) -> str:
    """Matching key: words only, lower case ("Physics + ML Research" == "physics ml research")."""
    return " ".join(re.findall(r"[^\W_]+", (name or "").lower()))


def resolve_project(name: Optional[str], create: bool = True) -> Optional[int]:
    """Project id for a name (case/space-insensitive); creates it when create=True."""
    if not name or not name.strip():
        return None
    key = project_key(name)
    if not key:
        return None
    with _lock, db() as c:
        r = c.execute("SELECT id FROM projects WHERE key=?", (key,)).fetchone()
        if r:
            return r[0]
        if not create:
            raise HTTPException(404, f"project '{name}' not found")
        return c.execute("INSERT INTO projects (name, key, created_at) VALUES (?,?,?)",
                         (name.strip(), key, now_iso())).lastrowid


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
    project: Optional[str] = Field(default=None, max_length=100, description="project name; created if new")
    milestone: bool = False


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
    pid = None
    if body.parent_id:
        parent = get_task(body.parent_id)
        pid = parent["project_id"]  # subtasks inherit the parent's project
    pid = resolve_project(body.project) or pid
    ts = now_iso()
    with _lock, db() as c:
        cur = c.execute(
            "INSERT INTO tasks (title, notes, category, priority, due_at, due_has_time, parent_id, project_id, milestone,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (body.title.strip(), body.notes, norm_category(body.category), body.priority, due_at, int(has_time),
             body.parent_id, pid, int(body.milestone), ts, ts))
        tid = cur.lastrowid
    return task_out(get_task(tid))


@app.get("/tasks", dependencies=A)
def list_tasks(status: Literal["open", "done", "cancelled", "all"] = "open", category: Optional[str] = None,
               priority: Optional[str] = None, overdue: bool = False, due_within_days: Optional[int] = Query(None, ge=0, le=365),
               q: Optional[str] = None, project: Optional[str] = None,
               limit: int = Query(50, ge=1, le=200)) -> dict:
    sql, args = "SELECT * FROM tasks WHERE 1=1", []
    if project:
        sql += " AND project_id=?"; args.append(resolve_project(project, create=False))
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
    project: Optional[str] = Field(default=None, max_length=100, description="move to project; 'none' removes it")
    milestone: Optional[bool] = None


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
    if body.project is not None:
        if body.project.strip().lower() in ("none", ""):
            sets.append("project_id=NULL")
        else:
            sets.append("project_id=?"); args.append(resolve_project(body.project))
    if body.milestone is not None:
        sets.append("milestone=?"); args.append(int(body.milestone))
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
            "tz_offset_min": int(now_local().utcoffset().total_seconds() // 60),
            "extras": briefing_extras(), "weekly": {"text": weekly_review()["text"], "time_local": WEEKLY_TIME,
                                                    "computed_ms": int(datetime.now(timezone.utc).timestamp() * 1000)}}


WEEKLY_TIME = os.environ.get("PA_WEEKLY_TIME", "18:00")  # Sundays


def briefing_extras() -> list[dict]:
    """Dated items from other modules for the morning briefing. The worker shows an item when it is due
    within `window_days` (or overdue), bucketed at send time. Later modules append their own sections here."""
    out = []
    with db() as c:
        for p in c.execute("SELECT name, deadline_at FROM projects WHERE status='active' AND deadline_at IS NOT NULL"):
            out.append({"section": "🎯 Project deadlines", "title": p["name"][:80], "window_days": 7,
                        "due_ms": int(from_iso(p["deadline_at"]).timestamp() * 1000)})
        try:
            for f in c.execute("SELECT person, about, due_at FROM followups WHERE status='open'"):
                out.append({"section": "🔁 Follow-ups", "title": f"{f['person']}: {f['about']}"[:100], "window_days": 0,
                            "due_ms": int(from_iso(f["due_at"]).timestamp() * 1000)})
            for a in c.execute("SELECT company, role, next_step, next_at FROM applications WHERE next_at IS NOT NULL "
                               "AND status IN ('applied','interview','offer')"):
                out.append({"section": "💼 Applications", "title": f"{a['company']} {a['role']}: {a['next_step'] or 'next step'}"[:100],
                            "window_days": 2, "due_ms": int(from_iso(a["next_at"]).timestamp() * 1000)})
            for k in c.execute("SELECT topic, due_on FROM cards WHERE status='active' ORDER BY due_on LIMIT 15"):
                d = datetime.combine(_date.fromisoformat(k["due_on"]), datetime.min.time(), TZ).replace(hour=9)
                out.append({"section": "📚 Revise today", "title": k["topic"][:100], "window_days": 0,
                            "due_ms": int(d.timestamp() * 1000)})
        except sqlite3.OperationalError:
            pass  # tables not created yet (first start)
    return out[:40]


def push_briefing() -> None:
    """Best effort: refresh the data the worker turns into the morning briefing."""
    try:
        worker("PUT", "/briefing", briefing_payload())
    except HTTPException:
        pass


@app.middleware("http")
async def refresh_briefing_after_changes(request, call_next):
    response = await call_next(request)
    if request.method in ("POST", "PATCH", "DELETE") and request.url.path.startswith(("/tasks", "/inbox", "/habits", "/projects", "/followups", "/applications", "/focus", "/learning", "/revise")) \
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


# ---------- projects ----------
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    deadline: Optional[str] = Field(default=None, max_length=200)


def _project_row(key_or_name: str) -> sqlite3.Row:
    key = project_key(key_or_name)
    with db() as c:
        r = c.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()
        if not r and key:  # forgiving lookup: a unique partial match ("research" -> "physics + ml research")
            rows = c.execute("SELECT * FROM projects WHERE key LIKE ?", (f"%{key}%",)).fetchall()
            if len(rows) > 1:
                raise HTTPException(409, "several projects match: " + ", ".join(x["name"] for x in rows))
            r = rows[0] if rows else None
    if not r:
        raise HTTPException(404, f"project '{key_or_name}' not found")
    return r


def _project_summary(p: sqlite3.Row) -> dict:
    with db() as c:
        counts = dict(c.execute("SELECT status, COUNT(*) FROM tasks WHERE project_id=? AND status!='cancelled' GROUP BY status",
                                (p["id"],)).fetchall())
        nxt = c.execute("SELECT title, due_at, due_has_time FROM tasks WHERE project_id=? AND status='open' AND milestone=1 "
                        "ORDER BY due_at IS NULL, due_at LIMIT 1", (p["id"],)).fetchone()
    done, open_ = counts.get("done", 0), counts.get("open", 0)
    dl = from_iso(p["deadline_at"])
    nm = None
    if nxt:
        nm = {"title": nxt["title"], "due": fmt(from_iso(nxt["due_at"]), bool(nxt["due_has_time"])) if nxt["due_at"] else None}
    return {"id": p["id"], "name": p["name"], "status": p["status"], "description": p["description"],
            "deadline": fmt(dl, False) if dl else None,
            "days_left": (dl.astimezone(TZ).date() - today_local()).days if dl else None,
            "tasks_open": open_, "tasks_done": done,
            "progress_pct": round(100 * done / (done + open_)) if done + open_ else 0, "next_milestone": nm}


@app.post("/projects", status_code=201, dependencies=A)
def add_project(body: ProjectIn) -> dict:
    deadline_at = None
    if body.deadline:
        try:
            deadline_at = to_utc_iso(parse_when(body.deadline)[0])
        except ValueError as e:
            raise HTTPException(422, str(e))
    key = project_key(body.name)
    if not key:
        raise HTTPException(422, "project name needs letters or numbers")
    with _lock, db() as c:
        if c.execute("SELECT 1 FROM projects WHERE key=?", (key,)).fetchone():
            raise HTTPException(409, f"project '{body.name}' already exists")
        c.execute("INSERT INTO projects (name, key, description, deadline_at, created_at) VALUES (?,?,?,?,?)",
                  (body.name.strip(), key, body.description, deadline_at, now_iso()))
    return _project_summary(_project_row(key))


@app.get("/projects", dependencies=A)
def list_projects(status: Literal["active", "paused", "done", "all"] = "active") -> dict:
    sql = "SELECT * FROM projects" + ("" if status == "all" else " WHERE status=?") + " ORDER BY deadline_at IS NULL, deadline_at, id"
    with db() as c:
        rows = c.execute(sql, () if status == "all" else (status,)).fetchall()
    return {"count": len(rows), "projects": [_project_summary(p) for p in rows]}


class ProjectPatch(BaseModel):
    description: Optional[str] = Field(default=None, max_length=2000)
    deadline: Optional[str] = Field(default=None, max_length=200, description="'none' clears it")
    status: Optional[Literal["active", "paused", "done"]] = None


@app.patch("/projects/{name}", dependencies=A)
def update_project(name: str, body: ProjectPatch) -> dict:
    p = _project_row(name)
    sets, args = [], []
    if body.description is not None:
        sets.append("description=?"); args.append(body.description)
    if body.status is not None:
        sets.append("status=?"); args.append(body.status)
    if body.deadline is not None:
        if body.deadline.strip().lower() in ("none", ""):
            sets.append("deadline_at=NULL")
        else:
            try:
                dt = parse_when(body.deadline)[0]
            except ValueError as e:
                raise HTTPException(422, str(e))
            sets.append("deadline_at=?"); args.append(to_utc_iso(dt))
    if not sets:
        raise HTTPException(400, "nothing to change")
    with _lock, db() as c:
        c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", (*args, p["id"]))
    return _project_summary(_project_row(p["key"]))


@app.get("/projects/{name}/overview", dependencies=A)
def project_overview(name: str) -> dict:
    p = _project_row(name)
    out = _project_summary(p)
    with db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE project_id=? AND status='open' ORDER BY milestone DESC, due_at IS NULL, due_at",
                         (p["id"],)).fetchall()
        notes = [n for n in c.execute("SELECT id, kind, text, project, created_at FROM inbox WHERE project!='' AND status!='archived' "
                                      "ORDER BY id DESC LIMIT 500") if project_key(n["project"]) == p["key"]][:15]
        recent = c.execute("SELECT title FROM tasks WHERE project_id=? AND status='done' ORDER BY done_at DESC LIMIT 5",
                           (p["id"],)).fetchall()
    tasks = [task_out(r) for r in rows]
    out.update({"pending": [t for t in tasks if not t["milestone"]], "milestones": [t for t in tasks if t["milestone"]],
                "overdue": [t["title"] for t in tasks if t["overdue"]],
                "notes": [{"id": n["id"], "kind": n["kind"], "text": n["text"][:300], "created": fmt(from_iso(n["created_at"]))}
                          for n in notes],
                "recently_done": [r["title"] for r in recent]})
    return out


# ---------- weekly review ----------
@app.get("/review", dependencies=A)
def weekly_review() -> dict:
    now = now_local()
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    s_utc, n_utc = to_utc_iso(start), to_utc_iso(now)
    with db() as c:
        done = [r[0] for r in c.execute("SELECT title FROM tasks WHERE status='done' AND done_at BETWEEN ? AND ? ORDER BY done_at",
                                        (s_utc, n_utc))]
        missed = [r[0] for r in c.execute("SELECT title FROM tasks WHERE status='open' AND due_at BETWEEN ? AND ? ORDER BY due_at",
                                          (s_utc, n_utc))]
        added = c.execute("SELECT COUNT(*) FROM tasks WHERE created_at BETWEEN ? AND ?", (s_utc, n_utc)).fetchone()[0]
        hrows = c.execute("SELECT * FROM habits WHERE active=1").fetchall()
        prows = c.execute("SELECT * FROM projects WHERE status='active'").fetchall()
        inbox_new = c.execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]
        nxt = c.execute("SELECT title, due_at, due_has_time FROM tasks WHERE status='open' AND due_at > ? AND due_at <= ? ORDER BY due_at",
                        (n_utc, to_utc_iso(now + timedelta(days=7)))).fetchall()
    elapsed = now.weekday() + 1
    habits = []
    for h in hrows:
        st = _habit_stats(h)
        target = st["per_week"] if h["schedule"] == "weekly" else (min(elapsed, 5) if h["schedule"] == "weekdays" else elapsed)
        habits.append({"name": st["name"], "done": st["this_week"], "target": target})
    spend = expense_summary("week")
    projects = [_project_summary(p) for p in prows]
    with db() as c:
        moved = [f"{r[0]} → {r[1]}" for r in c.execute(
            "SELECT a.company, e.to_status FROM application_events e JOIN applications a ON a.id=e.app_id "
            "WHERE e.from_status IS NOT NULL AND e.at BETWEEN ? AND ? ORDER BY e.at", (s_utc, n_utc))]
        active_apps = c.execute("SELECT COUNT(*) FROM applications WHERE status IN ('saved','applied','interview','offer')").fetchone()[0]
        fu_open = c.execute("SELECT COUNT(*) FROM followups WHERE status='open'").fetchone()[0]
        srows = c.execute("SELECT * FROM subjects WHERE status='active' ORDER BY id").fetchall()
        reviewed = c.execute("SELECT COUNT(*) FROM cards WHERE last_review >= ?", (start.date().isoformat(),)).fetchone()[0]
    focus = focus_stats("week")
    study = [_subject_progress(s) for s in srows]
    out = {"week_of": start.strftime("%d %b"), "done": done, "missed": missed, "added": added, "habits": habits,
           "applications": {"active": active_apps, "moved": moved} if active_apps or moved else None,
           "followups_open": fu_open,
           "focus": {"total": focus["total"], "minutes": focus["total_minutes"], "top": next(iter(focus["by_topic"]), None)},
           "study": [{"name": s["name"], "this_week": s["this_week"], "minutes": s["this_week_min"],
                      "target_min": s["target_min_week"]} for s in study],
           "cards_reviewed": reviewed,
           "spending": {"total": spend["total"], "previous": spend["previous_period_total"]},
           "next_week": [{"title": r["title"], "due": fmt(from_iso(r["due_at"]), bool(r["due_has_time"]))} for r in nxt],
           "inbox_new": inbox_new,
           "projects": [{"name": p["name"], "progress_pct": p["progress_pct"], "days_left": p["days_left"]} for p in projects]}
    out["text"] = review_text(out)
    return out


def review_text(r: dict) -> str:
    L = [f"📊 Week of {r['week_of']}",
         f"✅ Done: {len(r['done'])}" + (" (" + ", ".join(r["done"][:6]) + ")" if r["done"] else "")]
    if r["missed"]:
        L.append(f"⚠️ Missed deadlines: {len(r['missed'])} (" + ", ".join(r["missed"][:6]) + ")")
    if r["habits"]:
        L.append("💪 Habits: " + ", ".join(f"{h['name']} {h['done']}/{h['target']}" for h in r["habits"]))
    sp = r["spending"]
    if sp["total"] or sp["previous"]:
        L.append(f"💸 Spent ₹{sp['total']:g} this week (last week ₹{sp['previous']:g})")
    if r["projects"]:
        L.append("🎯 Projects: " + ", ".join(
            f"{p['name']} {p['progress_pct']}%" + (f" ({p['days_left']} days left)" if p["days_left"] is not None else "")
            for p in r["projects"]))
    if r.get("applications"):
        a = r["applications"]
        L.append(f"💼 Applications: {a['active']} active" + (f", moved this week: {', '.join(a['moved'])}" if a["moved"] else ""))
    if r.get("focus") and r["focus"]["minutes"]:
        L.append(f"⏱ Focus: {r['focus']['total']}" + (f" (most on {r['focus']['top']})" if r["focus"]["top"] else ""))
    if r.get("study"):
        L.append("📚 Study: " + ", ".join(
            f"{s['name']} {s['this_week']}" + (f"/{s['target_min'] // 60}h{s['target_min'] % 60 or ''}" if s["target_min"] else "")
            for s in r["study"]) + (f"; {r['cards_reviewed']} revision(s) done" if r.get("cards_reviewed") else ""))
    if r.get("followups_open"):
        L.append(f"🔁 Open follow-ups: {r['followups_open']}")
    nw = r["next_week"]
    L.append(f"🗓 Next 7 days: {len(nw)} due" + (" (" + ", ".join(f"{t['title']}, {t['due']}" for t in nw[:5]) + ")" if nw else ""))
    if r["inbox_new"]:
        L.append(f"📥 {r['inbox_new']} inbox item(s) to sort")
    return "\n".join(L)


# ---------- shared: schedule a reminder through the worker, never failing the caller ----------
def _when_or_offset(text: str, default_hour: int = 9) -> datetime:
    """'in 4 days', 'Friday', 'tomorrow 5pm', or a bare '4 days' -> aware datetime (date only -> default_hour:00)."""
    t = (text or "").strip()
    if _OFFSET.match(t):
        t = "in " + t
    try:
        dt, has_time = parse_when(t)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not has_time:
        dt = dt.replace(hour=default_hour, minute=0)
    return dt


def _schedule(text: str, dt: datetime) -> tuple[Optional[str], Optional[str]]:
    """(reminder_id, warning). The record is saved either way; a missing reminder is reported, not fatal."""
    if dt <= now_local():
        return None, f"reminder time {fmt(dt)} is in the past; no reminder set"
    try:
        r = worker("POST", "/reminders", {"text": text, "due_utc": to_utc_iso(dt), "repeat": "none",
                                          "tz_offset_min": int(dt.utcoffset().total_seconds() // 60)})
        return r.get("id"), None
    except HTTPException as e:
        return None, f"saved, but the reminder could not be scheduled ({e.detail})"


def _unschedule(rid: Optional[str]) -> None:
    if rid:
        try:
            worker("POST", f"/reminders/{rid}/cancel")
        except HTTPException:
            pass


# ---------- follow-ups ----------
class FollowupIn(BaseModel):
    person: str = Field(min_length=1, max_length=100)
    about: str = Field(min_length=1, max_length=300)
    when: str = Field(default="in 3 days", max_length=100, description="'in 4 days', 'Friday', 'tomorrow 5pm'")
    channel: str = Field(default="", max_length=40, description="email, whatsapp, call, linkedin…")


def _followup_out(r: sqlite3.Row) -> dict:
    out = dict(r)
    out["due"] = fmt(from_iso(r["due_at"]))
    out["overdue"] = r["status"] == "open" and from_iso(r["due_at"]) < datetime.now(timezone.utc)
    out["waiting_days"] = (today_local() - _date.fromisoformat(r["since_day"])).days
    return out


@app.post("/followups", status_code=201, dependencies=A)
def add_followup(body: FollowupIn) -> dict:
    dt = _when_or_offset(body.when)
    rid, warn = _schedule(f"Follow up with {body.person} about {body.about}" + (f" ({body.channel})" if body.channel else ""), dt)
    with _lock, db() as c:
        fid = c.execute("INSERT INTO followups (person, about, channel, since_day, due_at, reminder_id, created_at) VALUES (?,?,?,?,?,?,?)",
                        (body.person.strip(), body.about.strip(), body.channel.strip().lower(), today_local().isoformat(),
                         to_utc_iso(dt), rid, now_iso())).lastrowid
        r = c.execute("SELECT * FROM followups WHERE id=?", (fid,)).fetchone()
    out = _followup_out(r)
    if warn:
        out["warning"] = warn
    return out


@app.get("/followups", dependencies=A)
def list_followups(status: Literal["open", "done", "cancelled", "all"] = "open", person: Optional[str] = None) -> dict:
    sql, args = "SELECT * FROM followups WHERE 1=1", []
    if status != "all":
        sql += " AND status=?"; args.append(status)
    if person:
        sql += " AND person LIKE ?"; args.append(f"%{person}%")
    with db() as c:
        rows = c.execute(sql + " ORDER BY due_at", args).fetchall()
    return {"count": len(rows), "followups": [_followup_out(r) for r in rows]}


@app.post("/followups/{fid}/{action}", dependencies=A)
def close_followup(fid: int, action: Literal["done", "cancel"]) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM followups WHERE id=?", (fid,)).fetchone()
    if not r:
        raise HTTPException(404, f"follow-up {fid} not found")
    _unschedule(r["reminder_id"])
    with _lock, db() as c:
        c.execute("UPDATE followups SET status=?, done_at=? WHERE id=?",
                  ("done" if action == "done" else "cancelled", now_iso(), fid))
        r = c.execute("SELECT * FROM followups WHERE id=?", (fid,)).fetchone()
    return _followup_out(r)


# ---------- job / internship applications ----------
APP_STATUSES = ("saved", "applied", "interview", "offer", "rejected", "withdrawn")


class ApplicationIn(BaseModel):
    company: str = Field(min_length=1, max_length=100)
    role: str = Field(default="", max_length=150)
    status: str = "applied"
    link: str = Field(default="", max_length=500)
    contact: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2000)
    applied_on: Optional[str] = Field(default=None, description="'today', 'yesterday' or YYYY-MM-DD")


def _norm_status(s: str) -> str:
    s = (s or "applied").strip().lower()
    aliases = {"interviewing": "interview", "interviews": "interview", "rejected by them": "rejected", "declined": "rejected",
               "wishlist": "saved", "to apply": "saved", "accepted": "offer", "offered": "offer", "withdrew": "withdrawn"}
    s = aliases.get(s, s)
    if s not in APP_STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(APP_STATUSES)}")
    return s


def _app_out(r: sqlite3.Row) -> dict:
    out = {k: r[k] for k in ("id", "company", "role", "link", "status", "applied_on", "contact", "next_step", "notes")}
    out["next"] = fmt(from_iso(r["next_at"])) if r["next_at"] else None
    return out


def _find_app(ref: str) -> sqlite3.Row:
    with db() as c:
        if str(ref).isdigit():
            r = c.execute("SELECT * FROM applications WHERE id=?", (int(ref),)).fetchone()
        else:
            rows = c.execute("SELECT * FROM applications WHERE company LIKE ? OR (company || ' ' || role) LIKE ? ORDER BY id DESC",
                             (f"%{ref}%", f"%{ref}%")).fetchall()
            active = [x for x in rows if x["status"] not in ("rejected", "withdrawn")]
            pool = active or rows
            if len(pool) > 1:
                raise HTTPException(409, "several applications match: " + "; ".join(f"#{x['id']} {x['company']} {x['role']}" for x in pool))
            r = pool[0] if pool else None
    if not r:
        raise HTTPException(404, f"application '{ref}' not found")
    return r


@app.post("/applications", status_code=201, dependencies=A)
def add_application(body: ApplicationIn) -> dict:
    status = _norm_status(body.status)
    applied_on = _day_arg(body.applied_on).isoformat() if status != "saved" else None
    ts = now_iso()
    with _lock, db() as c:
        aid = c.execute("INSERT INTO applications (company, role, link, status, applied_on, contact, notes, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)", (body.company.strip(), body.role.strip(), body.link.strip(), status, applied_on,
                                                        body.contact.strip(), body.notes, ts, ts)).lastrowid
        c.execute("INSERT INTO application_events (app_id, from_status, to_status, at) VALUES (?,?,?,?)", (aid, None, status, ts))
        r = c.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone()
    return _app_out(r)


class ApplicationPatch(BaseModel):
    status: Optional[str] = None
    next_step: Optional[str] = Field(default=None, max_length=300, description="e.g. 'technical interview'")
    next_when: Optional[str] = Field(default=None, max_length=100, description="e.g. 'Thursday 3pm'; 'none' clears")
    contact: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=2000)
    link: Optional[str] = Field(default=None, max_length=500)


@app.patch("/applications/{ref}", dependencies=A)
def update_application(ref: str, body: ApplicationPatch) -> dict:
    r = _find_app(ref)
    sets, args, warnings = [], [], []
    new_status = _norm_status(body.status) if body.status is not None else None
    for f in ("next_step", "contact", "notes", "link"):
        v = getattr(body, f)
        if v is not None:
            sets.append(f"{f}=?"); args.append(v.strip() if f != "notes" else v)
    if new_status:
        sets.append("status=?"); args.append(new_status)
    reminder_ids = json.loads(r["reminder_ids"] or "[]")
    if body.next_when is not None:
        for rid in reminder_ids:
            _unschedule(rid)
        reminder_ids = []
        if body.next_when.strip().lower() in ("none", ""):
            sets.append("next_at=NULL")
        else:
            dt = _when_or_offset(body.next_when, default_hour=10)
            sets.append("next_at=?"); args.append(to_utc_iso(dt))
            label = f"{(body.next_step or r['next_step'] or 'next step')} at {r['company']}"
            for when, text in ((dt.replace(hour=18, minute=0) - timedelta(days=1), f"Tomorrow: {label} ({fmt(dt)}). Prepare tonight."),
                               (dt - timedelta(hours=1), f"In 1 hour: {label}")):
                rid, warn = _schedule(text, when)
                if rid:
                    reminder_ids.append(rid)
                elif warn and "past" not in warn:
                    warnings.append(warn)
    if new_status in ("rejected", "withdrawn", "offer"):
        for rid in reminder_ids:
            _unschedule(rid)
        reminder_ids = []
    sets.append("reminder_ids=?"); args.append(json.dumps(reminder_ids))
    sets.append("updated_at=?"); args.append(now_iso())
    with _lock, db() as c:
        c.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id=?", (*args, r["id"]))
        if new_status and new_status != r["status"]:
            c.execute("INSERT INTO application_events (app_id, from_status, to_status, at) VALUES (?,?,?,?)",
                      (r["id"], r["status"], new_status, now_iso()))
        r2 = c.execute("SELECT * FROM applications WHERE id=?", (r["id"],)).fetchone()
    out = _app_out(r2)
    out["reminders_set"] = len(reminder_ids)
    if warnings:
        out["warning"] = "; ".join(warnings)
    return out


@app.get("/applications", dependencies=A)
def list_applications(status: Optional[str] = None, active_only: bool = True) -> dict:
    sql, args = "SELECT * FROM applications WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(_norm_status(status))
    elif active_only:
        sql += " AND status NOT IN ('rejected','withdrawn')"
    with db() as c:
        rows = c.execute(sql + " ORDER BY next_at IS NULL, next_at, updated_at DESC", args).fetchall()
        counts = dict(c.execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall())
    return {"count": len(rows), "by_status": counts, "applications": [_app_out(r) for r in rows]}


# ---------- focus sessions ----------
FOCUS_GRACE = timedelta(minutes=60)  # a running session this long past its planned end is closed automatically


def _focus_minutes(r: sqlite3.Row) -> int:
    start = from_iso(r["started_at"])
    end = from_iso(r["ended_at"]) if r["ended_at"] else datetime.now(timezone.utc)
    return max(0, int((end - start).total_seconds() // 60))


def _focus_out(r: sqlite3.Row) -> dict:
    out = dict(r)
    out["minutes"] = _focus_minutes(r)
    out["started"] = fmt(from_iso(r["started_at"]))
    out["planned_end"] = fmt(from_iso(r["started_at"]) + timedelta(minutes=r["planned_min"]))
    return out


def _settle_focus() -> None:
    """The user often never says 'stop'. Sessions that ran past planned end + grace are closed at the planned end."""
    now = datetime.now(timezone.utc)
    with _lock, db() as c:
        for r in c.execute("SELECT * FROM focus_sessions WHERE status='running'").fetchall():
            end = from_iso(r["started_at"]) + timedelta(minutes=r["planned_min"])
            if now > end + FOCUS_GRACE:
                c.execute("UPDATE focus_sessions SET status='done', ended_at=? WHERE id=?",
                          (end.isoformat(timespec="seconds"), r["id"]))


def _running_focus() -> Optional[sqlite3.Row]:
    _settle_focus()
    with db() as c:
        return c.execute("SELECT * FROM focus_sessions WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()


class FocusIn(BaseModel):
    topic: str = Field(min_length=1, max_length=120)
    minutes: int = Field(default=25, ge=5, le=240)
    project: str = Field(default="", max_length=100)


@app.post("/focus/start", status_code=201, dependencies=A)
def focus_start(body: FocusIn) -> dict:
    cur = _running_focus()
    if cur:
        raise HTTPException(409, f"a focus session is already running: '{cur['topic']}' since "
                                 f"{fmt(from_iso(cur['started_at']))} (id {cur['id']}). Stop it first with focus_stop.")
    pid = resolve_project(body.project, create=False) if body.project.strip() else None
    start = datetime.now(timezone.utc)
    brk = 5 if body.minutes <= 30 else 10
    rid, warn = _schedule(f"⏱ {body.minutes} min of {body.topic} done. Take a {brk}-min break.",
                          start.astimezone(TZ) + timedelta(minutes=body.minutes))
    with _lock, db() as c:
        fid = c.execute("INSERT INTO focus_sessions (topic, project_id, planned_min, started_at, reminder_id) VALUES (?,?,?,?,?)",
                        (body.topic.strip(), pid, body.minutes, start.isoformat(timespec="seconds"), rid)).lastrowid
        r = c.execute("SELECT * FROM focus_sessions WHERE id=?", (fid,)).fetchone()
    out = _focus_out(r)
    if warn:
        out["warning"] = warn
    return out


@app.post("/focus/stop", dependencies=A)
def focus_stop() -> dict:
    cur = _running_focus()
    if not cur:
        raise HTTPException(404, "no focus session is running")
    _unschedule(cur["reminder_id"])
    now = datetime.now(timezone.utc)
    elapsed = (now - from_iso(cur["started_at"])).total_seconds() / 60
    status = "done" if elapsed >= cur["planned_min"] * 0.9 else "stopped"
    with _lock, db() as c:
        c.execute("UPDATE focus_sessions SET status=?, ended_at=? WHERE id=?",
                  (status, now.isoformat(timespec="seconds"), cur["id"]))
        r = c.execute("SELECT * FROM focus_sessions WHERE id=?", (cur["id"],)).fetchone()
    return _focus_out(r)


@app.post("/focus/distracted", dependencies=A)
def focus_distracted() -> dict:
    cur = _running_focus()
    if not cur:
        raise HTTPException(404, "no focus session is running")
    with _lock, db() as c:
        c.execute("UPDATE focus_sessions SET distractions=distractions+1 WHERE id=?", (cur["id"],))
        r = c.execute("SELECT * FROM focus_sessions WHERE id=?", (cur["id"],)).fetchone()
    return _focus_out(r)


def _period_start(period: str) -> datetime:
    day = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return day
    if period == "month":
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())  # week starts Monday


@app.get("/focus/stats", dependencies=A)
def focus_stats(period: str = "week") -> dict:
    period = period.strip().lower()
    if period not in ("today", "week", "month"):
        period = "week"
    cur = _running_focus()
    with db() as c:
        rows = c.execute("SELECT f.*, p.name AS project FROM focus_sessions f LEFT JOIN projects p ON p.id=f.project_id "
                         "WHERE f.started_at >= ? ORDER BY f.started_at", (to_utc_iso(_period_start(period)),)).fetchall()
    by_topic: dict = {}
    by_project: dict = {}
    for r in rows:
        m = _focus_minutes(r)
        by_topic[r["topic"].lower()] = by_topic.get(r["topic"].lower(), 0) + m
        if r["project"]:
            by_project[r["project"]] = by_project.get(r["project"], 0) + m
    total = sum(by_topic.values())
    return {"period": period, "sessions": len(rows), "total_minutes": total, "total": f"{total // 60}h {total % 60}m",
            "by_topic": dict(sorted(by_topic.items(), key=lambda x: -x[1])), "by_project": by_project,
            "distractions": sum(r["distractions"] for r in rows), "running": _focus_out(cur) if cur else None}


# ---------- quick-capture triage ----------
@app.get("/inbox/triage", dependencies=A)
def inbox_triage(limit: int = Query(15, ge=1, le=50)) -> dict:
    """Unsorted captures, oldest first, for the user to send each one somewhere."""
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]
        rows = c.execute("SELECT * FROM inbox WHERE status='new' ORDER BY id LIMIT ?", (limit,)).fetchall()
    return {"to_sort": total, "items": [inbox_out(r) for r in rows],
            "options": ["make it a task", "make it a reminder", "keep as a note (optionally in a project)", "archive"]}


def _inbox_row(iid: int) -> sqlite3.Row:
    with db() as c:
        r = c.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone()
    if not r:
        raise HTTPException(404, f"inbox item {iid} not found")
    return r


class ToReminderIn(BaseModel):
    when: str = Field(min_length=1, max_length=100)
    text: Optional[str] = Field(default=None, max_length=300)


@app.post("/inbox/{iid}/to_reminder", dependencies=A)
def inbox_to_reminder(iid: int, body: ToReminderIn) -> dict:
    r = _inbox_row(iid)
    dt = _when_or_offset(body.when)
    rid, warn = _schedule(body.text or r["text"][:300], dt)
    if not rid:
        raise HTTPException(422, warn or "reminder not scheduled")
    with _lock, db() as c:
        c.execute("UPDATE inbox SET status='processed', reminder_id=? WHERE id=?", (rid, iid))
    return {"inbox_id": iid, "reminder_id": rid, "when": fmt(dt)}


@app.post("/inbox/{iid}/archive", dependencies=A)
def inbox_archive(iid: int) -> dict:
    _inbox_row(iid)
    with _lock, db() as c:
        c.execute("UPDATE inbox SET status='archived' WHERE id=?", (iid,))
    return inbox_out(_inbox_row(iid))


class InboxKeepIn(BaseModel):
    project: str = Field(default="", max_length=100)


@app.post("/inbox/{iid}/keep", dependencies=A)
def inbox_keep(iid: int, body: InboxKeepIn = InboxKeepIn()) -> dict:
    """Keep the item as a note (optionally linked to a project) and mark it sorted."""
    _inbox_row(iid)
    with _lock, db() as c:
        if body.project.strip():
            c.execute("UPDATE inbox SET project=? WHERE id=?", (body.project.strip().lower(), iid))
        c.execute("UPDATE inbox SET status='processed' WHERE id=?", (iid,))
    return inbox_out(_inbox_row(iid))


# ---------- learning tracker ----------
_NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
             "forty-five": 45, "sixty": 60, "ninety": 90}


def parse_duration(text: str) -> int:
    """Minutes from '90 min', '1h30', '1.5 hours', 'an hour and a half', 'half an hour', '45'. Raises 422."""
    t = (text or "").strip().lower()
    if re.fullmatch(r"(half an hour|half hour|30 mins?)", t):
        return 30
    half = 30 if re.search(r"\band a half\b", t) else 0
    t = re.sub(r"\band a half\b", "", t)
    t = re.sub(r"\b(" + "|".join(sorted(map(re.escape, _NUMWORDS), key=len, reverse=True)) + r")\b",
               lambda m: str(_NUMWORDS[m.group(1)]), t)
    if re.fullmatch(r"\d+(\.\d+)?", t.strip()):
        return int(round(float(t)))
    hm = re.fullmatch(r"\s*(\d+)\s*(?:h|hr|hrs)\s*(\d{1,2})\s*", t)  # 1h30
    if hm:
        return int(hm.group(1)) * 60 + int(hm.group(2))
    h = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)\b", t)
    m = re.search(r"(\d+)\s*(m|min|mins|minute|minutes)\b", t)
    if not h and not m:
        raise HTTPException(422, f"couldn't read a duration from '{text}' (try '90 min' or '1h30')")
    total = (float(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0) + half
    if total <= 0 or total > 24 * 60 * 7:
        raise HTTPException(422, f"duration out of range: '{text}'")
    return int(round(total))


def parse_weekly_target(text: str) -> int:
    """'3 hours a week' -> 180; '30 min a day' / 'daily' -> x7; plain duration = per week."""
    t = (text or "").strip().lower()
    if not t or t in ("none", "0"):
        return 0
    mins = parse_duration(re.sub(r"\b(a|per|every|each)\s+(week|day)\b|\b(weekly|daily|/week|/day|a week|a day)\b", " ", t).strip()
                          or t)
    return mins * 7 if re.search(r"\b(day|daily|/day)\b", t) else mins


def _subject_row(name: str, create: bool = False) -> sqlite3.Row:
    key = project_key(name)
    if not key:
        raise HTTPException(422, "subject name is empty")
    with _lock, db() as c:
        r = c.execute("SELECT * FROM subjects WHERE key=?", (key,)).fetchone()
        if not r:
            rows = c.execute("SELECT * FROM subjects WHERE key LIKE ?", (f"%{key}%",)).fetchall()
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                raise HTTPException(409, "several subjects match: " + ", ".join(x["name"] for x in rows))
            if not create:
                raise HTTPException(404, f"no subject called '{name}'. Add it with learning_add.")
            sid = c.execute("INSERT INTO subjects (name, key, created_at) VALUES (?,?,?)",
                            (name.strip(), key, now_iso())).lastrowid
            r = c.execute("SELECT * FROM subjects WHERE id=?", (sid,)).fetchone()
    return r


def _study_days(s: sqlite3.Row) -> dict:
    """{day: minutes} from the study log plus focus sessions whose topic mentions the subject."""
    days: dict = {}
    with db() as c:
        for d, m in c.execute("SELECT day, SUM(minutes) FROM study_log WHERE subject_id=? GROUP BY day", (s["id"],)):
            days[_date.fromisoformat(d)] = days.get(_date.fromisoformat(d), 0) + m
        for r in c.execute("SELECT * FROM focus_sessions WHERE status != 'running'").fetchall():
            if s["key"] in project_key(r["topic"]):
                d = from_iso(r["started_at"]).astimezone(TZ).date()
                days[d] = days.get(d, 0) + _focus_minutes(r)
    return days


def _subject_progress(s: sqlite3.Row) -> dict:
    days = _study_days(s)
    today = today_local()
    ws = today - timedelta(days=today.weekday())
    week = sum(m for d, m in days.items() if ws <= d <= today)
    streak, d = 0, (today if days.get(today) else today - timedelta(days=1))
    while days.get(d):
        streak += 1; d -= timedelta(days=1)
    with db() as c:
        mods = c.execute("SELECT title, done FROM modules WHERE subject_id=? ORDER BY position, id", (s["id"],)).fetchall()
    nxt = next((m["title"] for m in mods if not m["done"]), None)
    tgt = s["target_min_week"]
    return {"id": s["id"], "name": s["name"], "status": s["status"],
            "this_week_min": week, "this_week": f"{week // 60}h {week % 60}m",
            "target_min_week": tgt, "target_pct": round(100 * week / tgt) if tgt else None,
            "total_min": sum(days.values()), "streak_days": streak,
            "modules_done": sum(1 for m in mods if m["done"]), "modules_total": len(mods), "next_module": nxt}


class SubjectIn(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    target: str = Field(default="", max_length=60, description="'3 hours a week', '30 min a day'")


@app.post("/learning", status_code=201, dependencies=A)
def learning_add(body: SubjectIn) -> dict:
    tgt = parse_weekly_target(body.target)
    s = _subject_row(body.subject, create=True)
    with _lock, db() as c:
        if body.target.strip():
            c.execute("UPDATE subjects SET target_min_week=?, status='active' WHERE id=?", (tgt, s["id"]))
        s = c.execute("SELECT * FROM subjects WHERE id=?", (s["id"],)).fetchone()
    return _subject_progress(s)


@app.get("/learning", dependencies=A)
def learning_progress(subject: Optional[str] = None) -> dict:
    if subject and subject.strip():
        return _subject_progress(_subject_row(subject))
    with db() as c:
        rows = c.execute("SELECT * FROM subjects WHERE status='active' ORDER BY id").fetchall()
    return {"subjects": [_subject_progress(s) for s in rows]}


class StudyIn(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    duration: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=300)
    day: Optional[str] = None


@app.post("/learning/log", status_code=201, dependencies=A)
def study_log(body: StudyIn) -> dict:
    mins = parse_duration(body.duration)
    day = _day_arg(body.day)
    s = _subject_row(body.subject, create=True)
    with _lock, db() as c:
        c.execute("INSERT INTO study_log (subject_id, minutes, day, note, created_at) VALUES (?,?,?,?,?)",
                  (s["id"], mins, day.isoformat(), body.note.strip(), now_iso()))
    return {"logged_min": mins, "day": day.isoformat(), **_subject_progress(s)}


class ModulesIn(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    titles: str = Field(min_length=1, max_length=4000, description="one module, or several separated by commas/new lines")


@app.post("/learning/modules", status_code=201, dependencies=A)
def module_add(body: ModulesIn) -> dict:
    s = _subject_row(body.subject, create=True)
    titles = [t.strip(" -•\t") for t in re.split(r"[\n,;]+", body.titles) if t.strip(" -•\t")]
    with _lock, db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position), 0) FROM modules WHERE subject_id=?", (s["id"],)).fetchone()[0]
        for i, t in enumerate(titles, 1):
            c.execute("INSERT INTO modules (subject_id, title, position) VALUES (?,?,?)", (s["id"], t[:200], pos + i))
    return {"added": titles, **_subject_progress(s)}


class ModuleDoneIn(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)


@app.post("/learning/modules/done", dependencies=A)
def module_done(body: ModuleDoneIn) -> dict:
    s = _subject_row(body.subject)
    with _lock, db() as c:
        rows = c.execute("SELECT * FROM modules WHERE subject_id=? AND done=0 AND title LIKE ? ORDER BY position",
                         (s["id"], f"%{body.title.strip()}%")).fetchall()
        exact = [r for r in rows if r["title"].lower() == body.title.strip().lower()]
        rows = exact or rows
        if not rows:
            raise HTTPException(404, f"no open module matching '{body.title}' in {s['name']}")
        if len(rows) > 1:
            raise HTTPException(409, "several modules match: " + ", ".join(r["title"] for r in rows))
        c.execute("UPDATE modules SET done=1, done_at=? WHERE id=?", (now_iso(), rows[0]["id"]))
    return {"done": rows[0]["title"], **_subject_progress(s)}


# ---------- spaced repetition ----------
LADDER = (1, 3, 7, 14, 30, 60)  # days; "remembered" climbs one step, "forgot" goes back to 1


def _card_out(r: sqlite3.Row) -> dict:
    out = dict(r)
    out["due"] = _date.fromisoformat(r["due_on"]).strftime("%a %d %b")
    return out


class CardIn(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    prompt: str = Field(default="", max_length=500, description="question or what to recall")
    first_in: str = Field(default="3 days", max_length=60)
    project: str = Field(default="", max_length=100)


@app.post("/revise", status_code=201, dependencies=A)
def revise_add(body: CardIn) -> dict:
    t = re.sub(r"^\s*after\s+", "in ", body.first_in.strip(), flags=re.I)
    due = _when_or_offset(t).date()
    if due <= today_local():
        due = today_local() + timedelta(days=1)
    interval = max(1, (due - today_local()).days)
    pid = resolve_project(body.project, create=False) if body.project.strip() else None
    with _lock, db() as c:
        cid = c.execute("INSERT INTO cards (topic, prompt, project_id, interval_days, due_on, created_at) VALUES (?,?,?,?,?,?)",
                        (body.topic.strip(), body.prompt.strip(), pid, interval, due.isoformat(), now_iso())).lastrowid
        r = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    return _card_out(r)


@app.get("/revise/due", dependencies=A)
def revise_due(days: int = Query(0, ge=0, le=60)) -> dict:
    until = (today_local() + timedelta(days=days)).isoformat()
    with db() as c:
        rows = c.execute("SELECT * FROM cards WHERE status='active' AND due_on <= ? ORDER BY due_on, id", (until,)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM cards WHERE status='active'").fetchone()[0]
    return {"due_count": len(rows), "active_cards": total, "cards": [_card_out(r) for r in rows]}


class ReviewIn(BaseModel):
    remembered: bool


@app.post("/revise/{cid}", dependencies=A)
def revise_done(cid: int, body: ReviewIn) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, f"revision card {cid} not found")
    if body.remembered:
        interval = next((d for d in LADDER if d > r["interval_days"]), LADDER[-1])
        lapses = r["lapses"]
    else:
        interval, lapses = LADDER[0], r["lapses"] + 1
    due = today_local() + timedelta(days=interval)
    with _lock, db() as c:
        c.execute("UPDATE cards SET interval_days=?, due_on=?, reviews=reviews+1, lapses=?, last_review=? WHERE id=?",
                  (interval, due.isoformat(), lapses, today_local().isoformat(), cid))
        r = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    return _card_out(r)


@app.post("/revise/{cid}/retire", dependencies=A)
def revise_retire(cid: int) -> dict:
    with _lock, db() as c:
        if not c.execute("UPDATE cards SET status='retired' WHERE id=?", (cid,)).rowcount:
            raise HTTPException(404, f"revision card {cid} not found")
        r = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    return _card_out(r)

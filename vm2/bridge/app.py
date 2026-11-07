"""DSH delegation bridge: an authenticated task API in front of `dsh headless`.

Runs as the unprivileged `dsh-bridge` user on VM 2. Its only privilege is `sudo dsh-ctl`, which runs
each task as `dsh-runner` in its own systemd sandbox (sees only its own workspace, no secrets on any
command line, CPU/memory/time limits), one at a time. Task records live in SQLite so they
survive restarts; tasks that were running when the bridge stopped become `interrupted`
and are never re-run automatically.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

ROOT = Path(os.environ.get("DSH_BRIDGE_ROOT", "/data/dsh"))
DB_PATH = ROOT / "bridge" / "tasks.db"
TOKEN_PATH = ROOT / "bridge" / "token"
WORKSPACES = ROOT / "workspaces"
RESULTS = ROOT / "results"
LOGS = ROOT / "logs"
INBOX = ROOT / "inbox"
CTL = "/usr/local/sbin/dsh-ctl"
DSH_BIN = "/opt/npm/bin/dsh"
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
FREELLMAPI = os.environ.get("FREELLMAPI_URL", "http://127.0.0.1:3001")
MAX_TIMEOUT_S = 7200
RATE_LIMIT_PER_MIN = 60  # flood guard only
V1_TOOLS = {"read", "write", "exec"}
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}


def ctl(*args: str, text: bool = True, **kw) -> subprocess.CompletedProcess:
    """Call the root-owned control program; the bridge has no other privilege."""
    return subprocess.run(["sudo", "-n", CTL, *args], capture_output=True, text=text, **kw)


_TOKEN = TOKEN_PATH.read_text().strip()
_db_lock = threading.Lock()
_rate: deque[float] = deque()
_rate_lock = threading.Lock()
_running: dict[str, subprocess.Popen] = {}
_cancel_requested: set[str] = set()


# ---------- storage ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                objective TEXT NOT NULL,
                input_context TEXT NOT NULL DEFAULT '',
                input_files TEXT NOT NULL DEFAULT '{}',
                allowed_tools TEXT NOT NULL,
                timeout_s INTEGER NOT NULL,
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER,
                result TEXT,
                error TEXT,
                artifact_references TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL)"""
        )
        # Recovery: anything left running by a previous process is interrupted, never re-run.
        for (tid,) in c.execute("SELECT task_id FROM tasks WHERE status='running'").fetchall():
            ctl("stop", tid)
        c.execute(
            "UPDATE tasks SET status='interrupted', finished_at=?, "
            "error='bridge restarted while task was running; not re-run automatically' "
            "WHERE status='running'",
            (time.time(),),
        )


def update(task_id: str, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with _db_lock, db() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE task_id=?", (*fields.values(), task_id))


def get_row(task_id: str) -> sqlite3.Row:
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "task not found")
    return row


# ---------- API models ----------
SAFE_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/\-]{1,200}$")


class TaskIn(BaseModel):
    objective: str = Field(min_length=3, max_length=8000)
    input_context: str = Field(default="", max_length=32000)
    input_files: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[Literal["read", "write", "exec"]] = Field(default_factory=lambda: sorted(V1_TOOLS))
    timeout_s: int = Field(default=1800, ge=30, le=MAX_TIMEOUT_S)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("input_files")
    @classmethod
    def _files(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 50 or sum(len(s) for s in v.values()) > 200_000:
            raise ValueError("input_files: at most 50 files and 200 KB in total")
        for p in v:
            if not SAFE_PATH.match(p):
                raise ValueError(f"input_files: unsafe path {p!r}")
        return v

    @field_validator("allowed_tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        # v1 can only run DSH's workspace-write preset, which always includes all three.
        if set(v) != V1_TOOLS:
            raise ValueError("v1 supports only allowed_tools = ['exec', 'read', 'write'] (workspace-confined)")
        return sorted(v)


def public(row: sqlite3.Row, with_result: bool = False) -> dict:
    out = {
        "task_id": row["task_id"],
        "status": row["status"],
        "objective": row["objective"],
        "workspace_id": row["workspace_id"],
        "allowed_tools": json.loads(row["allowed_tools"]),
        "timeout_s": row["timeout_s"],
        "idempotency_key": row["idempotency_key"],
        "exit_code": row["exit_code"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
    if row["started_at"]:
        out["elapsed_s"] = round((row["finished_at"] or time.time()) - row["started_at"], 1)
    if with_result:
        out["result"] = row["result"]
        out["artifact_references"] = json.loads(row["artifact_references"])
    return out


# ---------- auth / rate limit ----------
def auth(authorization: str = Header(default="")) -> None:
    supplied = authorization.removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied.encode(), _TOKEN.encode()):
        raise HTTPException(401, "invalid or missing bearer token")


def rate_limit() -> None:
    now = time.time()
    with _rate_lock:
        while _rate and now - _rate[0] > 60:
            _rate.popleft()
        if len(_rate) >= RATE_LIMIT_PER_MIN:
            raise HTTPException(429, f"rate limit: {RATE_LIMIT_PER_MIN} new tasks per minute")
        _rate.append(now)


# ---------- execution ----------
def build_prompt(row: sqlite3.Row) -> str:
    parts = [row["objective"].strip()]
    if row["input_context"].strip():
        parts += ["", "Context:", row["input_context"].strip()]
    parts += ["", "Work only inside the current directory. When done, summarise what you changed "
                  "and the exact output of any tests you ran."]
    return "\n".join(parts)


def prepare_workspace(row: sqlite3.Row) -> None:
    inbox = INBOX / row["task_id"]
    shutil.rmtree(inbox, ignore_errors=True)
    (inbox / "files").mkdir(parents=True)
    (inbox / "prompt.txt").write_text(build_prompt(row))
    for rel, content in json.loads(row["input_files"]).items():
        target = inbox / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    done = ctl("prepare", row["task_id"])
    shutil.rmtree(inbox, ignore_errors=True)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or "prepare failed")


def list_workspace(task_id: str) -> list[dict]:
    files = []
    for line in ctl("list", task_id).stdout.splitlines():
        rel, _, size = line.partition("\t")
        if rel:
            files.append({"path": rel, "bytes": int(size or 0)})
    return files


def collect(task_id: str, stdout: str, stderr: str) -> list[dict]:
    out_dir = RESULTS / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "final_response.txt").write_text(stdout)
    (out_dir / "stderr.log").write_text(stderr)
    (out_dir / "changes.diff").write_text(ctl("diff", task_id).stdout)
    files = list_workspace(task_id)
    (out_dir / "files.json").write_text(json.dumps(files, indent=1))
    refs = [{"name": n, "path": str(out_dir / n)} for n in
            ("final_response.txt", "changes.diff", "stderr.log", "files.json")]
    refs.append({"name": "workspace", "path": str(WORKSPACES / task_id), "files": files[:200]})
    return refs


def run_task(row: sqlite3.Row) -> None:
    task_id, timeout = row["task_id"], row["timeout_s"]
    started = time.time()
    update(task_id, status="running", started_at=started)
    try:
        prepare_workspace(row)
    except Exception as e:
        update(task_id, status="failed", finished_at=time.time(), error=f"workspace setup failed: {e}")
        return
    proc = subprocess.Popen(["sudo", "-n", CTL, "run", task_id, str(timeout)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _running[task_id] = proc
    try:
        # systemd enforces RuntimeMaxSec=timeout inside the sandbox; this is only a backstop.
        stdout, stderr = proc.communicate(timeout=timeout + 60)
    except subprocess.TimeoutExpired:
        ctl("stop", task_id)
        stdout, stderr = proc.communicate()
    finally:
        _running.pop(task_id, None)
    if task_id in _cancel_requested:
        status, err = "cancelled", "cancelled by request"
    elif time.time() - started >= timeout - 1 and proc.returncode != 0:
        status, err = "timed_out", f"exceeded timeout of {timeout} s; sandbox stopped"
    elif proc.returncode == 0:
        status, err = "completed", None
    else:
        status, err = "failed", f"dsh exited with code {proc.returncode}"
    _cancel_requested.discard(task_id)
    LOGS.mkdir(exist_ok=True)
    (LOGS / f"{task_id}.stderr.log").write_text(stderr or "")
    refs = collect(task_id, stdout or "", stderr or "")
    update(task_id, status=status, exit_code=proc.returncode, result=(stdout or "").strip()[-20000:],
           error=err, finished_at=time.time(), artifact_references=json.dumps(refs))


def worker() -> None:
    while True:
        with db() as c:
            row = c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            time.sleep(2)
            continue
        try:
            run_task(row)
        except Exception as e:  # never let one task kill the worker
            update(row["task_id"], status="failed", finished_at=time.time(), error=f"bridge error: {e!r}")


# ---------- routes ----------
app = FastAPI(title="DSH delegation bridge", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    for d in (RESULTS, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    init_db()
    threading.Thread(target=worker, daemon=True, name="dsh-worker").start()


@app.get("/health")
def health() -> dict:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/version", timeout=3) as r:
            ollama = json.load(r).get("version", "ok")
    except Exception as e:
        ollama = f"unreachable: {e.__class__.__name__}"
    try:
        with urllib.request.urlopen(f"{FREELLMAPI}/api/ping", timeout=3) as r:
            freellmapi = "ok" if r.status == 200 else f"http {r.status}"
    except Exception as e:
        freellmapi = f"unreachable: {e.__class__.__name__}"
    with db() as c:
        counts = dict(c.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())
    return {"bridge": "ok", "ollama": ollama, "freellmapi": freellmapi, "dsh": Path(DSH_BIN).exists(),
            "tasks": counts}


@app.post("/tasks", status_code=201, dependencies=[Depends(auth)])
def create_task(body: TaskIn) -> dict:
    if body.idempotency_key:
        with db() as c:
            existing = c.execute("SELECT * FROM tasks WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
        if existing:
            return {**public(existing), "duplicate": True}
    rate_limit()
    task_id = "task-" + uuid.uuid4().hex[:12]
    try:
        with _db_lock, db() as c:
            c.execute(
                "INSERT INTO tasks (task_id, idempotency_key, objective, input_context, input_files, "
                "allowed_tools, timeout_s, workspace_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, body.idempotency_key, body.objective, body.input_context, json.dumps(body.input_files),
                 json.dumps(body.allowed_tools), body.timeout_s, task_id, "queued", time.time()),
            )
    except sqlite3.IntegrityError:  # lost a race on the same idempotency key
        with db() as c:
            existing = c.execute("SELECT * FROM tasks WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
        return {**public(existing), "duplicate": True}
    return {**public(get_row(task_id)), "duplicate": False}


@app.get("/tasks/{task_id}", dependencies=[Depends(auth)])
def task_status(task_id: str) -> dict:
    row = get_row(task_id)
    out = public(row)
    if row["status"] == "queued":
        with db() as c:
            out["queue_position"] = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='queued' AND created_at<=?", (row["created_at"],)
            ).fetchone()[0]
    return out


@app.get("/tasks/{task_id}/result", dependencies=[Depends(auth)])
def task_result(task_id: str) -> dict:
    row = get_row(task_id)
    if row["status"] not in TERMINAL:
        raise HTTPException(409, f"task is {row['status']}; result not ready")
    out = public(row, with_result=True)
    diff = RESULTS / task_id / "changes.diff"
    out["diff"] = diff.read_text()[-20000:] if diff.exists() else ""
    return out


SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
MAX_FETCH_BYTES = 200_000


def _finished(task_id: str) -> None:
    row = get_row(task_id)  # 404 for unknown tasks
    if row["status"] not in TERMINAL:
        raise HTTPException(409, f"task is {row['status']}; files are available once it finishes")


@app.get("/tasks/{task_id}/files", dependencies=[Depends(auth)])
def list_files(task_id: str) -> dict:
    _finished(task_id)
    return {"task_id": task_id, "files": list_workspace(task_id)[:500]}


@app.get("/tasks/{task_id}/files/{rel:path}", dependencies=[Depends(auth)])
def fetch_file(task_id: str, rel: str) -> dict:
    _finished(task_id)
    if not SAFE_PATH.match(rel) or any(part in SKIP_PARTS for part in Path(rel).parts):
        raise HTTPException(400, "invalid path")
    done = ctl("read", task_id, rel, text=False)
    if done.returncode == 3:
        raise HTTPException(404, "file not found")
    if done.returncode != 0:
        raise HTTPException(400, "path escapes the task workspace" if done.returncode == 4 else "invalid path")
    data = done.stdout
    truncated = len(data) > MAX_FETCH_BYTES
    try:
        text = data[:MAX_FETCH_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "binary file; only UTF-8 text files can be fetched in v1")
    return {"task_id": task_id, "path": rel, "bytes": len(data[:MAX_FETCH_BYTES]), "truncated": truncated,
            "content": text}


@app.post("/tasks/{task_id}/cancel", dependencies=[Depends(auth)])
def cancel(task_id: str) -> dict:
    row = get_row(task_id)
    if row["status"] == "queued":
        update(task_id, status="cancelled", finished_at=time.time(), error="cancelled before start")
    elif row["status"] == "running" and task_id in _running:
        _cancel_requested.add(task_id)
        ctl("stop", task_id)
    return public(get_row(task_id))

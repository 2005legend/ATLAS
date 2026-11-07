"""Hermes MCP tools for delegating coding tasks to DeepSeek Harness on VM 2.

Talks to the DSH delegation bridge over Tailscale and keeps a local task history
(task id, objective, status, result summary) in SQLite on VM 1. Tasks on the CPU
worker take 10-30 minutes: submit, carry on, and check back with dsh_status/dsh_wait.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).resolve().parent
BRIDGE_URL = os.environ.get("DSH_BRIDGE_URL", "").rstrip("/")
TOKEN_FILE = Path(os.environ.get("DSH_BRIDGE_TOKEN_FILE", HERE / "dsh_token"))
HISTORY_DB = Path(os.environ.get("DSH_TASK_HISTORY_DB", HERE / "task_history.db"))
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}

mcp = FastMCP("dsh")


# ---------- task history (VM 1) ----------
def _history() -> sqlite3.Connection:
    c = sqlite3.connect(HISTORY_DB)
    c.execute(
        """CREATE TABLE IF NOT EXISTS delegations (
            task_id TEXT PRIMARY KEY, objective TEXT, status TEXT,
            result_summary TEXT, submitted_at REAL, updated_at REAL)"""
    )
    return c


def _record(task: dict, summary: str | None = None) -> None:
    if "task_id" not in task:
        return
    with _history() as c:
        c.execute(
            """INSERT INTO delegations (task_id, objective, status, result_summary, submitted_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,
               result_summary=COALESCE(excluded.result_summary, delegations.result_summary)""",
            (task["task_id"], task.get("objective"), task.get("status"), summary, time.time(), time.time()),
        )


# ---------- bridge client ----------
def _call(method: str, path: str, body: dict | None = None, timeout: float = 20) -> dict:
    if not BRIDGE_URL:
        return {"ok": False, "error": "delegation_unavailable", "detail": "DSH_BRIDGE_URL is not configured"}
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return {"ok": False, "error": "delegation_unavailable", "detail": f"bridge token file missing: {TOKEN_FILE}"}
    req = urllib.request.Request(
        BRIDGE_URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except Exception:
            detail = e.reason
        return {"ok": False, "error": f"http_{e.code}", "detail": detail}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return {"ok": False, "error": "delegation_unavailable",
                "detail": f"DSH worker (VM 2) unreachable: {e}. Do the task yourself or tell the user delegation is down."}


# ---------- tools ----------
@mcp.tool()
def dsh_submit(objective: str, input_context: str = "", input_files: dict[str, str] | None = None,
               timeout_min: int = 30, idempotency_key: str | None = None) -> dict:
    """Delegate a bounded coding task to the DSH coding agent on VM 2.

    It works in a fresh, isolated workspace (no network access to other machines, no secrets)
    and can read, write and run commands only inside it. It is slow (CPU model): expect
    10-30 minutes. Returns a task_id; then use dsh_wait or dsh_status, and dsh_result.

    objective: precise instructions, including how to verify (e.g. "run python3 -m pytest").
    input_context: optional background text. input_files: optional {relative_path: content}
    seeded into the workspace (max 50 files / 200 KB). timeout_min: 1-120.
    idempotency_key: reuse the same key when retrying a submit, so the task never runs twice.
    """
    body = {"objective": objective, "input_context": input_context, "input_files": input_files or {},
            "timeout_s": max(1, min(int(timeout_min), 120)) * 60,
            "idempotency_key": idempotency_key or f"hermes-{uuid.uuid4().hex}"}
    out = _call("POST", "/tasks", body)
    _record(out)
    return out


@mcp.tool()
def dsh_status(task_id: str) -> dict:
    """Get the status of a delegated DSH task (queued, running, completed, failed, cancelled, timed_out, interrupted)."""
    out = _call("GET", f"/tasks/{task_id}")
    _record(out)
    return out


@mcp.tool()
def dsh_wait(task_id: str, max_minutes: int = 10) -> dict:
    """Wait up to max_minutes (1-10) for a DSH task to finish; returns its status (and result when done).

    If it is still running when the wait ends, carry on with other work and call dsh_wait again later.
    """
    deadline = time.time() + max(1, min(int(max_minutes), 10)) * 60
    while True:
        out = _call("GET", f"/tasks/{task_id}")
        if out.get("ok") is False or out.get("status") in TERMINAL:
            break
        if time.time() >= deadline:
            out["note"] = "still running; call dsh_wait again later"
            break
        time.sleep(15)
    _record(out)
    if out.get("status") in TERMINAL:
        return dsh_result(task_id)
    return out


@mcp.tool()
def dsh_result(task_id: str) -> dict:
    """Get the final answer, diff and artifact list of a finished DSH task. Review it before reporting to the user."""
    out = _call("GET", f"/tasks/{task_id}/result")
    if out.get("ok") is not False:
        _record(out, summary=(out.get("result") or "")[:500])
    return out


@mcp.tool()
def dsh_cancel(task_id: str) -> dict:
    """Cancel a queued or running DSH task."""
    out = _call("POST", f"/tasks/{task_id}/cancel")
    _record(out)
    return out


RESULTS_ROOT = Path(os.environ.get("DSH_RESULTS_DIR", "/data/workspace/dsh-results"))
TASK_ID_RE = re.compile(r"^task-[0-9a-f]{6,32}$")


@mcp.tool()
def dsh_list_files(task_id: str) -> dict:
    """List the files a finished DSH task left in its workspace on VM 2 (path and size)."""
    return _call("GET", f"/tasks/{task_id}/files")


@mcp.tool()
def dsh_fetch_files(task_id: str, paths: list[str] | None = None, save: bool = True) -> dict:
    """Copy files from a finished DSH task (on VM 2) to this machine (VM 1) and return their contents.

    paths: workspace-relative paths from dsh_list_files; omit to fetch every file (max 50).
    save: when true, writes them under /data/workspace/dsh-results/<task_id>/ on VM 1.
    Only UTF-8 text files up to 200 KB each; returned content is shortened to 4000 characters per file.
    """
    if not TASK_ID_RE.match(task_id):
        return {"ok": False, "error": "invalid task_id"}
    if not paths:
        listing = _call("GET", f"/tasks/{task_id}/files")
        if listing.get("ok") is False:
            return listing
        paths = [f["path"] for f in listing.get("files", [])]
    out_dir = RESULTS_ROOT / task_id
    fetched, errors = [], []
    for rel in paths[:50]:
        got = _call("GET", f"/tasks/{task_id}/files/{urllib.parse.quote(rel)}")
        if got.get("ok") is False:
            errors.append({"path": rel, "error": got.get("error"), "detail": got.get("detail")})
            continue
        item = {"path": rel, "bytes": got["bytes"], "truncated": got["truncated"]}
        if save:
            dest = (out_dir / rel).resolve()
            if out_dir.resolve() not in dest.parents:
                errors.append({"path": rel, "error": "refused: path escapes the results folder"})
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(got["content"], encoding="utf-8")
            item["saved_to"] = str(dest)
        content = got["content"]
        item["content"] = content if len(content) <= 4000 else content[:4000] + "\n... [shortened; full file saved]"
        fetched.append(item)
    return {"task_id": task_id, "saved_dir": str(out_dir) if save and fetched else None,
            "files": fetched, "errors": errors}


@mcp.tool()
def dsh_history(limit: int = 10) -> list[dict]:
    """List recent delegations recorded on VM 1 (task id, objective, status, result summary)."""
    with _history() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM delegations ORDER BY submitted_at DESC LIMIT ?",
                         (max(1, min(limit, 50)),)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    mcp.run()

"""Hermes MCP tools for the personal-assistant service (tasks, quick-capture inbox, agenda, reminders).

Every tool calls the service, which is the source of truth: only report success to the user when the
tool returned the saved item. Times are given in plain language ("tomorrow 10am", "in 30 minutes",
"by Sunday") and are parsed by the service in the user's time zone.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).resolve().parent
URL = os.environ.get("PA_URL", "http://127.0.0.1:8100").rstrip("/")
TOKEN_FILE = Path(os.environ.get("PA_TOKEN_FILE", "/data/pa/token"))
Priority = Literal["low", "medium", "high", "urgent"]
Category = Literal["college", "internship", "research", "personal", "career", "finance", "projects", "other"]

mcp = FastMCP("pa")


def _call(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    if query:
        path += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "", False)})
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN_FILE.read_text().strip()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("detail")
        except Exception:
            detail = e.reason
        return {"ok": False, "error": f"http_{e.code}", "detail": detail,
                "note": "Nothing was saved. Tell the user what went wrong; ask them to rephrase if it was the time."}
    except OSError as e:
        return {"ok": False, "error": "assistant_service_unavailable", "detail": str(e), "note": "Nothing was saved."}


# ---------- tasks ----------
@mcp.tool()
def task_add(title: str, priority: Priority = "medium", category: Category = "other", due: str | None = None,
             notes: str = "", parent_id: int | None = None) -> dict:
    """Add ONE task to the user's to-do list. Call it once per task (three tasks -> three calls).

    due: plain language like "tomorrow 10am", "by Sunday", "next Friday", "in 2 hours"; leave empty if none.
    category: pick the closest; priority: low/medium/high/urgent (default medium).
    parent_id: id of a bigger task this is a subtask of.
    Returns the saved task with its exact due date: repeat that date back to the user."""
    return _call("POST", "/tasks", {"title": title, "priority": priority, "category": category, "due": due,
                                    "notes": notes, "parent_id": parent_id})


@mcp.tool()
def task_list(status: Literal["open", "done", "cancelled", "all"] = "open", category: Category | None = None,
              overdue_only: bool = False, due_within_days: int | None = None, search: str | None = None) -> dict:
    """List the user's tasks (open by default), most urgent first. Use for "what do I have to do",
    "what's overdue", "deadlines this week" (due_within_days=7), or to find a task's id before changing it."""
    return _call("GET", "/tasks", query={"status": status, "category": category, "overdue": overdue_only,
                                         "due_within_days": due_within_days, "q": search})


@mcp.tool()
def task_done(task_id: int) -> dict:
    """Mark a task as completed. Use task_list(search=...) first if you don't know its id."""
    return _call("POST", f"/tasks/{task_id}/done")


@mcp.tool()
def task_update(task_id: int, title: str | None = None, priority: Priority | None = None,
                category: Category | None = None, due: str | None = None, notes: str | None = None) -> dict:
    """Change a task. Only pass what changes. due="none" removes the deadline."""
    return _call("PATCH", f"/tasks/{task_id}", {k: v for k, v in {"title": title, "priority": priority, "category": category,
                                                                  "due": due, "notes": notes}.items() if v is not None})


@mcp.tool()
def task_delete(task_id: int) -> dict:
    """Delete (cancel) a task the user no longer needs. Ask first if it's unclear which task they mean."""
    return _call("DELETE", f"/tasks/{task_id}")


# ---------- quick-capture inbox / notes ----------
@mcp.tool()
def inbox_add(text: str, kind: Literal["note", "link", "thought", "snippet"] = "note",
              project: str = "", tags: str = "") -> dict:
    """Save a note, link, command/snippet or thought to the user's inbox (their second brain). For ideas use idea_add.
    Keep the user's wording. project: e.g. "SLM research"; tags: comma-separated words."""
    return _call("POST", "/inbox", {"text": text, "kind": kind, "project": project, "tags": tags})


@mcp.tool()
def idea_add(text: str, project: str = "") -> dict:
    """Save an idea ("save this idea", "I have an idea for…", "note this idea down"). Keep the user's wording."""
    return _call("POST", "/inbox", {"text": text, "kind": "idea", "project": project})


@mcp.tool()
def inbox_search(query: str | None = None, project: str | None = None,
                 kind: Literal["idea", "note", "link", "thought", "snippet"] | None = None, limit: int = 10) -> dict:
    """Find saved notes/ideas by words, project or kind. No arguments = the latest items."""
    return _call("GET", "/inbox", query={"q": query, "project": project, "kind": kind, "limit": limit})


@mcp.tool()
def inbox_to_task(inbox_id: int, title: str | None = None, priority: Priority = "medium",
                  category: Category = "other", due: str | None = None) -> dict:
    """Turn a saved inbox item into a task (and mark the note processed)."""
    return _call("POST", f"/inbox/{inbox_id}/to_task", {"title": title, "priority": priority,
                                                        "category": category, "due": due})


# ---------- reminders (sent on time by an always-on worker, even while the VMs sleep) ----------
@mcp.tool()
def reminder_add(text: str, when: str | None = None,
                 repeat: Literal["none", "daily", "weekdays", "weekly", "monthly"] = "none",
                 before_task_id: int | None = None, before: str | None = None) -> dict:
    """Set a reminder that will be sent to the user on Telegram.

    when: plain language, e.g. "tomorrow 8pm", "in 30 minutes", "Sunday 9am" (no time given = 9:00).
    repeat: "every day at 8" -> repeat="daily"; "every weekday" -> "weekdays"; "every Sunday" -> "weekly" with when="Sunday ...".
    Deadline reminders: before_task_id=<task id> with before="3 days" (counts back from that task's deadline).
    Returns the saved reminder with its exact time: repeat it to the user."""
    return _call("POST", "/reminders", {"text": text, "when": when, "repeat": repeat,
                                        "before_task_id": before_task_id, "before": before})


@mcp.tool()
def reminder_list(status: Literal["pending", "sent", "cancelled", "failed", "all"] = "pending") -> dict:
    """List reminders (upcoming by default). Reminder messages show their id like [#r1a2b3c4d]."""
    return _call("GET", "/reminders", query={"status": status})


@mcp.tool()
def reminder_cancel(reminder_id: str) -> dict:
    """Cancel a reminder (id like r1a2b3c4d). Use reminder_list to find it if needed."""
    return _call("POST", f"/reminders/{reminder_id}/cancel")


@mcp.tool()
def reminder_snooze(reminder_id: str, minutes: int = 10) -> dict:
    """Snooze a reminder: send it again after `minutes`. For "snooze that", use the id from the
    latest reminder message, or reminder_list(status="sent")."""
    return _call("POST", f"/reminders/{reminder_id}/snooze", {"minutes": minutes})


# ---------- habits (repeat; a task is done once) ----------
@mcp.tool()
def habit_add(name: str, how_often: str) -> dict:
    """Start tracking a habit (one call per habit). name: just the activity, e.g. "German" (not "Track German").
    how_often (required): copy the user's words for frequency, e.g. "every day", "weekdays", "5 times a week",
    "twice a week". If they gave none, use "every day". The service works out the schedule."""
    return _call("POST", "/habits", {"name": name, "how_often": how_often})


@mcp.tool()
def habit_checkin(habit_id: int, day: str = "today", note: str = "") -> dict:
    """Record that the user did a habit ("did DSA today", "worked out yesterday"). day: today/yesterday/YYYY-MM-DD.
    Use habit_list first if you don't know the id. Reply with the new streak."""
    return _call("POST", f"/habits/{habit_id}/checkin", {"day": day, "note": note})


@mcp.tool()
def habit_list() -> dict:
    """All habits with streak, this week's count, and whether each is done/due today."""
    return _call("GET", "/habits")


@mcp.tool()
def habit_remove(habit_id: int) -> dict:
    """Stop tracking a habit (history is kept)."""
    return _call("DELETE", f"/habits/{habit_id}")


# ---------- expenses (simple log, no bank connections) ----------
ExpenseCategory = Literal["food", "transport", "shopping", "bills", "education", "health", "entertainment", "rent", "other"]


@mcp.tool()
def expense_add(amount: float, category: ExpenseCategory = "other", note: str = "", day: str = "today") -> dict:
    """Log money spent, in rupees ("spent ₹120 on lunch" -> amount=120, category="food", note="lunch").
    day: today/yesterday/YYYY-MM-DD. Returns the saved entry and this month's total."""
    return _call("POST", "/expenses", {"amount": amount, "category": category, "note": note, "day": day})


@mcp.tool()
def expense_summary(period: Literal["week", "month"] = "month") -> dict:
    """Spending this week or month: total, by category, and the previous period's total for comparison."""
    return _call("GET", "/expenses/summary", query={"period": period})


@mcp.tool()
def expense_list(days: int = 30, category: ExpenseCategory | None = None) -> dict:
    """Individual expenses from the last `days` days (optionally one category)."""
    return _call("GET", "/expenses", query={"days": days, "category": category})


@mcp.tool()
def expense_delete(expense_id: int) -> dict:
    """Delete a wrongly logged expense (find its id with expense_list)."""
    return _call("DELETE", f"/expenses/{expense_id}")


# ---------- agenda ----------
@mcp.tool()
def agenda(days: int = 7) -> dict:
    """The user's agenda: overdue tasks, today's tasks, the next `days` days, top undated tasks,
    new inbox items and tasks done today. Use for "what's on today", "plan my day", "what's coming up"."""
    return _call("GET", "/agenda", query={"days": days})


@mcp.tool()
def parse_time(text: str) -> dict:
    """Check how a time expression will be understood (e.g. "next Monday 9am") before using it."""
    return _call("POST", "/parse_time", {"text": text})


if __name__ == "__main__":
    mcp.run()

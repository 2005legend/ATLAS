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
Category = str  # one of: college, internship, research, personal, career, finance, projects, other (unknown -> other)

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
             notes: str = "", parent_id: int | None = None, project: str | None = None, milestone: bool = False) -> dict:
    """Add ONE task (three tasks -> three calls). due: "tomorrow 10am", "by Sunday", or empty.
    category: college, internship, research, personal, career, finance, projects or other.
    parent_id: parent task (subtask). project: e.g. "SLM research" (created if new); milestone: key checkpoint.
    Repeat the returned due date to the user."""
    return _call("POST", "/tasks", {"title": title, "priority": priority, "category": category, "due": due,
                                    "notes": notes, "parent_id": parent_id, "project": project, "milestone": milestone})


@mcp.tool()
def task_list(status: Literal["open", "done", "cancelled", "all"] = "open", category: Category | None = None,
              overdue_only: bool = False, due_within_days: int | None = None, search: str | None = None,
              project: str | None = None) -> dict:
    """List the user's tasks (open by default), most urgent first. Use for "what do I have to do",
    "what's overdue", "deadlines this week" (due_within_days=7), or to find a task's id before changing it.
    project: only that project's tasks."""
    return _call("GET", "/tasks", query={"status": status, "category": category, "overdue": overdue_only,
                                         "due_within_days": due_within_days, "q": search, "project": project})


# ---------- follow-ups ----------
@mcp.tool()
def followup_add(person: str, about: str, when: str = "in 3 days", channel: str = "") -> dict:
    """Track something the user is waiting on or must chase ("I emailed my professor, follow up in four days").
    Saves it and schedules a reminder. when: "4 days", "Friday", "tomorrow 5pm". channel: email/whatsapp/call/linkedin.
    If the result has a `warning`, tell the user the reminder wasn't set."""
    return _call("POST", "/followups", {"person": person, "about": about, "when": when, "channel": channel})


@mcp.tool()
def followup_list(status: str = "open", person: str | None = None) -> dict:
    """Open loops: who the user is waiting on or must chase, with due dates and days waiting.
    status: open (default), done, cancelled or all."""
    return _call("GET", "/followups", query={"status": status, "person": person})


@mcp.tool()
def followup_done(followup_id: int, cancelled: bool = False) -> dict:
    """Close a follow-up (they replied / done). cancelled=true if it's no longer needed. Cancels its reminder."""
    return _call("POST", f"/followups/{followup_id}/{'cancel' if cancelled else 'done'}")


# ---------- job / internship applications ----------
@mcp.tool()
def application_add(company: str, role: str = "", status: str = "applied", link: str = "", contact: str = "",
                    notes: str = "", applied_on: str | None = None) -> dict:
    """Add a job/internship application ("Add this Amazon role to my application tracker").
    status: saved (want to apply), applied, interview, offer, rejected, withdrawn. applied_on: today/yesterday/YYYY-MM-DD."""
    return _call("POST", "/applications", {"company": company, "role": role, "status": status, "link": link,
                                           "contact": contact, "notes": notes, "applied_on": applied_on})


@mcp.tool()
def application_update(company_or_id: str, status: str | None = None, next_step: str | None = None,
                       next_when: str | None = None, contact: str | None = None, notes: str | None = None) -> dict:
    """Update an application (by company, "company role" or id). next_step + next_when ("Thursday 3pm")
    schedules reminders the evening before and 1 h before; rejected/withdrawn/offer cancels them."""
    return _call("PATCH", f"/applications/{urllib.parse.quote(str(company_or_id), safe='')}",
                 {k: v for k, v in {"status": status, "next_step": next_step, "next_when": next_when,
                                    "contact": contact, "notes": notes}.items() if v is not None})


@mcp.tool()
def application_list(status: str | None = None, include_closed: bool = False) -> dict:
    """The application tracker: active applications (or one status), with next steps and counts by status."""
    return _call("GET", "/applications", query={"status": status, "active_only": not include_closed})


# ---------- projects + weekly review ----------
@mcp.tool()
def project_add(name: str, description: str = "", deadline: str | None = None) -> dict:
    """Create a project that groups tasks, milestones and notes (e.g. "SLM research", deadline "end of November")."""
    return _call("POST", "/projects", {"name": name, "description": description, "deadline": deadline})


@mcp.tool()
def project_list(status: Literal["active", "paused", "done", "all"] = "active") -> dict:
    """List projects with progress %, open/done task counts, days to deadline and next milestone."""
    return _call("GET", "/projects", query={"status": status})


@mcp.tool()
def project_overview(name: str) -> dict:
    """Everything about one project: pending tasks, milestones, overdue items, linked notes, recently done,
    progress and deadline. Use for "show me everything pending in my research project". A partial name works."""
    return _call("GET", f"/projects/{urllib.parse.quote(name, safe='')}/overview")


@mcp.tool()
def project_update(name: str, status: Literal["active", "paused", "done"] | None = None, deadline: str | None = None,
                   description: str | None = None) -> dict:
    """Change a project's status, deadline ("none" clears it) or description."""
    return _call("PATCH", f"/projects/{urllib.parse.quote(name, safe='')}",
                 {k: v for k, v in {"status": status, "deadline": deadline, "description": description}.items() if v is not None})


@mcp.tool()
def weekly_review() -> dict:
    """This week's review: tasks done, missed deadlines, habit completion, spending vs last week, project progress,
    what's due in the next 7 days. Reply with its `text` (you may add 1-2 sentences of advice)."""
    return _call("GET", "/review")


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


@mcp.tool()
def inbox_triage() -> dict:
    """Unsorted inbox captures, oldest first ("sort my inbox", "what's in my inbox to sort").
    Show them numbered with a suggested destination for each (task / reminder / keep as note / archive) and
    act only on what the user confirms, one tool call per item."""
    return _call("GET", "/inbox/triage")


@mcp.tool()
def inbox_to_reminder(inbox_id: int, when: str, text: str | None = None) -> dict:
    """Turn an inbox item into a one-time reminder (when: "tomorrow 10am", "in 2 days"); marks it sorted."""
    return _call("POST", f"/inbox/{inbox_id}/to_reminder", {"when": when, "text": text})


@mcp.tool()
def inbox_keep(inbox_id: int, project: str = "") -> dict:
    """Keep an inbox item as a note (optionally file it under a project) and mark it sorted."""
    return _call("POST", f"/inbox/{inbox_id}/keep", {"project": project})


@mcp.tool()
def inbox_archive(inbox_id: int) -> dict:
    """Archive an inbox item the user no longer needs (it stays searchable, never deleted)."""
    return _call("POST", f"/inbox/{inbox_id}/archive")


# ---------- focus sessions ----------
@mcp.tool()
def focus_start(topic: str, minutes: int = 25, project: str = "") -> dict:
    """Start a timed focus session ("start a 50-minute research session"). A message arrives when the time is up.
    Only one runs at a time; if one is running the tool says so: ask the user whether to stop it."""
    return _call("POST", "/focus/start", {"topic": topic, "minutes": minutes, "project": project})


@mcp.tool()
def focus_stop() -> dict:
    """End the running focus session ("done", "stop the timer"); returns the minutes focused."""
    return _call("POST", "/focus/stop")


@mcp.tool()
def focus_distracted() -> dict:
    """Count one distraction in the running focus session ("I got distracted")."""
    return _call("POST", "/focus/distracted")


@mcp.tool()
def focus_stats(period: str = "week") -> dict:
    """Focus time per topic and project. period: today, week (default) or month. Also shows a running session."""
    return _call("GET", "/focus/stats", query={"period": period})


# ---------- learning tracker ----------
@mcp.tool()
def learning_add(subject: str, target: str = "") -> dict:
    """Track a subject the user is learning, with an optional weekly goal ("3 hours a week", "30 min a day")."""
    return _call("POST", "/learning", {"subject": subject, "target": target})


@mcp.tool()
def study_log(subject: str, duration: str, note: str = "", day: str | None = None) -> dict:
    """Log study time ("I studied ML for 1h30"). duration: "90 min", "1h30", "2 hours". day: today/yesterday/YYYY-MM-DD.
    Focus sessions whose topic names the subject count automatically: don't log those twice."""
    return _call("POST", "/learning/log", {"subject": subject, "duration": duration, "note": note, "day": day})


@mcp.tool()
def module_add(subject: str, titles: str) -> dict:
    """Add course modules/chapters to a subject, in order. titles: one, or several separated by commas or new lines."""
    return _call("POST", "/learning/modules", {"subject": subject, "titles": titles})


@mcp.tool()
def module_done(subject: str, title: str) -> dict:
    """Mark a module/chapter of a subject finished (title can be part of the name)."""
    return _call("POST", "/learning/modules/done", {"subject": subject, "title": title})


@mcp.tool()
def learning_progress(subject: str | None = None) -> dict:
    """Study time this week vs goal, streak, modules done/total and next module. No subject = all subjects."""
    return _call("GET", "/learning", query={"subject": subject})


# ---------- spaced repetition ----------
@mcp.tool()
def revise_add(topic: str, first_in: str = "3 days", prompt: str = "") -> dict:
    """Schedule a topic for spaced revision ("remind me to revise transformers after three days").
    After each review the gap grows 3 -> 7 -> 14 -> 30 -> 60 days. Due topics appear in the morning briefing.
    prompt: optional question to answer from memory."""
    return _call("POST", "/revise", {"topic": topic, "first_in": first_in, "prompt": prompt})


@mcp.tool()
def revise_due(days_ahead: int = 0) -> dict:
    """Topics due for revision today (or within days_ahead days)."""
    return _call("GET", "/revise/due", query={"days": days_ahead})


@mcp.tool()
def revise_done(card_id: int, remembered: bool) -> dict:
    """Record a revision. remembered=true moves it to the next, longer gap; false brings it back tomorrow.
    Ask the user whether they remembered it if they didn't say."""
    return _call("POST", f"/revise/{card_id}", {"remembered": remembered})


# ---------- reminders (sent on time by an always-on worker, even while the VMs sleep) ----------
@mcp.tool()
def reminder_add(text: str, when: str | None = None,
                 repeat: Literal["none", "daily", "weekdays", "weekly", "monthly"] = "none",
                 before_task_id: int | None = None, before: str | None = None) -> dict:
    """Set a Telegram reminder. when: "tomorrow 8pm", "in 30 minutes" (no time = 9:00).
    "every day at 8" -> repeat="daily"; "every Sunday" -> "weekly" with when="Sunday ...".
    Before a deadline: before_task_id + before="3 days". Repeat the returned time to the user."""
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
    """Track a habit (one call each). name: the activity only, e.g. "German".
    how_often: the user's words ("every day", "weekdays", "5 times a week"); none given -> "every day"."""
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
ExpenseCategory = str  # one of: food, transport, shopping, bills, education, health, entertainment, rent, other (unknown -> other)


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


def _slim(node):
    """Drop schema noise every model call pays for: per-field "title", and `anyOf: [X, null]` -> X.
    Only the advertised schema changes; arguments are still validated against the full pydantic model."""
    if isinstance(node, dict):
        if isinstance(node.get("title"), str):  # metadata label; a *property* named "title" is a dict and stays
            node.pop("title")
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            rest = [s for s in any_of if s.get("type") != "null"]
            if len(rest) == 1 and len(rest) < len(any_of):
                node.pop("anyOf")
                node.update(rest[0])
        for v in node.values():
            _slim(v)
    elif isinstance(node, list):
        for v in node:
            _slim(v)
    return node


for _t in mcp._tool_manager.list_tools():
    _slim(_t.parameters)
    _t.description = " ".join((_t.description or "").split())  # collapse docstring indentation


if __name__ == "__main__":
    mcp.run()

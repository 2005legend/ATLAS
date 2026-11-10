"""Tests for the personal-assistant service. Run: PA_ROOT=<tmp dir with a token file> python -m pytest -q"""
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
(_tmp / "token").write_text("test-token\n")
os.environ["PA_ROOT"] = str(_tmp)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as pa  # noqa: E402

H = {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="module")
def client():
    with TestClient(pa.app) as c:
        yield c


# ---------- time parsing ----------
def test_tomorrow_10am():
    dt, has_time = pa.parse_when("tomorrow at 10 AM")
    now = pa.now_local()
    assert has_time and dt.date() == (now + timedelta(days=1)).date() and (dt.hour, dt.minute) == (10, 0)


def test_in_30_minutes():
    dt, has_time = pa.parse_when("in 30 minutes")
    delta = dt - pa.now_local()
    assert has_time and timedelta(minutes=29) <= delta <= timedelta(minutes=31)


def test_date_without_time_is_end_of_day():
    dt, has_time = pa.parse_when("by Sunday")
    assert not has_time and dt.weekday() == 6 and (dt.hour, dt.minute) == (23, 59)
    assert dt.date() >= pa.now_local().date()


def test_next_monday_is_future_monday():
    dt, _ = pa.parse_when("next Monday 9am")
    assert dt.weekday() == 0 and dt > pa.now_local() and dt.hour == 9


def test_word_times():
    assert pa.parse_when("tomorrow evening")[0].hour == 18
    assert pa.parse_when("tomorrow morning")[0].hour == 9


def test_timezone_is_india():
    dt, _ = pa.parse_when("tomorrow 10am")
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)
    assert pa.to_utc_iso(dt).endswith("04:30:00+00:00")  # 10:00 IST = 04:30 UTC


def test_past_time_rejected():
    with pytest.raises(ValueError):
        pa.parse_when("yesterday 10am")


def test_gibberish_rejected():
    with pytest.raises(ValueError):
        pa.parse_when("whenever the moon is blue-ish xyzzy")


# ---------- API ----------
def test_auth_required(client):
    assert client.get("/tasks").status_code == 401
    assert client.get("/health").status_code == 200


def test_task_lifecycle(client):
    r = client.post("/tasks", headers=H, json={"title": "Submit assignment", "priority": "high",
                                               "category": "College", "due": "tomorrow 10am"})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["category"] == "college" and t["priority"] == "high" and "10:00" in t["due"] and not t["overdue"]
    tid = t["id"]
    assert any(x["id"] == tid for x in client.get("/tasks", headers=H).json()["tasks"])
    r = client.patch(f"/tasks/{tid}", headers=H, json={"priority": "urgent", "due": "none"})
    assert r.json()["priority"] == "urgent" and r.json()["due"] is None
    assert client.post(f"/tasks/{tid}/done", headers=H).json()["status"] == "done"
    assert not any(x["id"] == tid for x in client.get("/tasks", headers=H).json()["tasks"])
    assert any(x["id"] == tid for x in client.get("/tasks?status=done", headers=H).json()["tasks"])


def test_bad_due_is_422_and_nothing_saved(client):
    before = client.get("/tasks?status=all", headers=H).json()["count"]
    r = client.post("/tasks", headers=H, json={"title": "x", "due": "blorp o'clock xyz"})
    assert r.status_code == 422
    assert client.get("/tasks?status=all", headers=H).json()["count"] == before


def test_unknown_category_becomes_other(client):
    assert client.post("/tasks", headers=H, json={"title": "y", "category": "astrology"}).json()["category"] == "other"


def test_subtasks(client):
    parent = client.post("/tasks", headers=H, json={"title": "Research experiment"}).json()
    client.post("/tasks", headers=H, json={"title": "Run baseline", "parent_id": parent["id"]})
    again = client.get("/tasks?q=Research experiment", headers=H).json()["tasks"][0]
    assert [s["title"] for s in again["subtasks"]] == ["Run baseline"]


def test_inbox_search_and_convert(client):
    r = client.post("/inbox", headers=H, json={"text": "AI agent that detects reasoning failures in small language models",
                                               "kind": "idea", "project": "SLM research"})
    assert r.status_code == 201
    iid = r.json()["id"]
    hits = client.get("/inbox?q=reasoning failures", headers=H).json()["items"]
    assert any(h["id"] == iid for h in hits)
    assert client.get("/inbox?project=slm research", headers=H).json()["count"] >= 1
    conv = client.post(f"/inbox/{iid}/to_task", headers=H, json={"due": "next Friday", "category": "research"}).json()
    assert conv["task"]["category"] == "research"
    item = [h for h in client.get("/inbox", headers=H).json()["items"] if h["id"] == iid][0]
    assert item["status"] == "processed" and item["task_id"] == conv["task"]["id"]


def test_reminder_refused_when_worker_missing(client):
    # No worker.json yet: must fail loudly (so Hermes never confirms a reminder that wasn't stored)
    r = client.post("/reminders", headers=H, json={"text": "study German", "when": "tomorrow 8pm"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


def test_reminder_bad_time_rejected_before_worker(client):
    r = client.post("/reminders", headers=H, json={"text": "x", "when": "flibber o'clock"})
    assert r.status_code == 422


def test_deadline_offset_parsing():
    assert pa._offset("3 days") == pa.timedelta(days=3)
    assert pa._offset("2 hours") == pa.timedelta(hours=2)
    with pytest.raises(pa.HTTPException):
        pa._offset("a while")


def test_briefing_payload_shape(client):
    client.post("/tasks", headers=H, json={"title": "Briefing check", "due": "tomorrow 10am", "priority": "high"})
    p = pa.briefing_payload()
    t = [x for x in p["tasks"] if x["title"] == "Briefing check"][0]
    assert t["has_time"] and isinstance(t["due_ms"], int) and p["tz_offset_min"] == 330


# ---------- habits ----------
def _backfill(hid, days_ago):
    with pa.db() as c:
        for n in days_ago:
            c.execute("INSERT OR IGNORE INTO checkins (habit_id, day, created_at) VALUES (?,?,?)",
                      (hid, (pa.today_local() - timedelta(days=n)).isoformat(), pa.now_iso()))


def test_daily_streak_counts_back_from_yesterday_if_today_open(client):
    h = client.post("/habits", headers=H, json={"name": "DSA practice"}).json()
    _backfill(h["id"], [1, 2, 3, 5])  # yesterday, 2, 3 days ago; gap at 4
    s = [x for x in client.get("/habits", headers=H).json()["habits"] if x["id"] == h["id"]][0]
    assert s["streak"] == 3 and not s["done_today"] and s["due_today"]
    r = client.post(f"/habits/{h['id']}/checkin", headers=H, json={}).json()
    assert r["streak"] == 4 and r["done_today"] and not r["already_checked_in"]
    again = client.post(f"/habits/{h['id']}/checkin", headers=H, json={}).json()
    assert again["already_checked_in"] and again["streak"] == 4  # no double counting


def test_weekdays_streak_skips_weekends(client):
    h = client.post("/habits", headers=H, json={"name": "German", "schedule": "weekdays"}).json()
    today = pa.today_local()
    # check in on every weekday of the last 14 days (weekends left empty)
    _backfill(h["id"], [n for n in range(0, 14) if (today - timedelta(days=n)).weekday() < 5])
    s = [x for x in client.get("/habits", headers=H).json()["habits"] if x["id"] == h["id"]][0]
    assert s["streak"] >= 9  # 10 weekdays in 14 days, minus today if today is a weekend-free count edge


def test_weekly_target_streak(client):
    h = client.post("/habits", headers=H, json={"name": "Gym", "schedule": "weekly", "per_week": 2}).json()
    today = pa.today_local()
    last_monday = today - timedelta(days=today.weekday()) - timedelta(days=7)
    with pa.db() as c:
        for off in (0, 2):  # two check-ins last week
            c.execute("INSERT INTO checkins (habit_id, day, created_at) VALUES (?,?,?)",
                      (h["id"], (last_monday + timedelta(days=off)).isoformat(), pa.now_iso()))
    s = [x for x in client.get("/habits", headers=H).json()["habits"] if x["id"] == h["id"]][0]
    assert s["streak"] == 1 and s["streak_unit"] == "weeks"


@pytest.mark.parametrize("text,expected", [
    ("every day", ("daily", 7)), ("daily", ("daily", 7)), ("5 times a week", ("weekly", 5)), ("3x per week", ("weekly", 3)),
    ("twice a week", ("weekly", 2)), ("on weekdays", ("weekdays", 5)), ("Mon to Fri", ("weekdays", 5)),
    ("once a week", ("weekly", 1)), ("7 times a week", ("daily", 7))])
def test_parse_frequency(text, expected):
    assert pa.parse_frequency(text) == expected


def test_how_often_overrides_schedule(client):
    h = client.post("/habits", headers=H, json={"name": "Reading", "how_often": "4 times a week"}).json()
    assert h["schedule"] == "weekly" and h["per_week"] == 4
    assert client.post("/habits", headers=H, json={"name": "Odd", "how_often": "whenever I feel like it"}).status_code == 422


def test_habit_rules(client):
    assert client.post("/habits", headers=H, json={"name": "dsa PRACTICE"}).status_code == 409  # duplicate name
    h = client.post("/habits", headers=H, json={"name": "Water"}).json()
    assert client.post(f"/habits/{h['id']}/checkin", headers=H, json={"day": "2999-01-01"}).status_code == 422
    assert client.post(f"/habits/{h['id']}/checkin", headers=H, json={"day": "yesterday"}).json()["checked_day"] == \
        (pa.today_local() - timedelta(days=1)).isoformat()
    assert client.delete(f"/habits/{h['id']}", headers=H).json()["ok"]
    assert all(x["id"] != h["id"] for x in client.get("/habits", headers=H).json()["habits"])


# ---------- expenses ----------
def test_expenses_and_summary(client):
    r = client.post("/expenses", headers=H, json={"amount": 120, "category": "Food", "note": "lunch"})
    assert r.status_code == 201 and r.json()["category"] == "food" and r.json()["month_total"] >= 120
    client.post("/expenses", headers=H, json={"amount": 80.5, "category": "metro rides", "note": "metro"})  # unknown -> other
    s = client.get("/expenses/summary?period=month", headers=H).json()
    cats = {c["category"]: c["total"] for c in s["by_category"]}
    assert cats.get("food", 0) >= 120 and cats.get("other", 0) >= 80.5 and s["total"] >= 200.5
    assert client.post("/expenses", headers=H, json={"amount": -5}).status_code == 422
    assert client.post("/expenses", headers=H, json={"amount": 5, "day": "2999-01-01"}).status_code == 422
    e = client.get("/expenses?days=1", headers=H).json()["expenses"][0]
    assert client.delete(f"/expenses/{e['id']}", headers=H).json()["ok"]


def test_briefing_payload_has_habits(client):
    assert any(h["name"] == "DSA practice" for h in pa.briefing_payload()["habits"])


# ---------- projects ----------
def test_project_create_and_lookup(client):
    r = client.post("/projects", headers=H, json={"name": "Physics + ML Research", "deadline": "in 20 days"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["name"] == "Physics + ML Research" and p["days_left"] in (19, 20) and p["progress_pct"] == 0
    assert client.post("/projects", headers=H, json={"name": "physics  + ml research"}).status_code == 409  # same key
    assert client.get("/projects/physics + ml research/overview", headers=H).status_code == 200
    assert client.get("/projects/ml research/overview", headers=H).json()["name"] == "Physics + ML Research"  # unique partial
    assert client.get("/projects/nonexistent thing/overview", headers=H).status_code == 404


def test_tasks_join_project_and_progress(client):
    t1 = client.post("/tasks", headers=H, json={"title": "Run baseline", "project": "Physics + ML research"}).json()
    t2 = client.post("/tasks", headers=H, json={"title": "Draft paper", "project": "physics + ml research",
                                               "milestone": True, "due": "in 10 days"}).json()
    sub = client.post("/tasks", headers=H, json={"title": "Pick seeds", "parent_id": t1["id"]}).json()
    assert t1["project"] == t2["project"] == sub["project"] == "Physics + ML Research"  # subtask inherits
    client.post(f"/tasks/{t1['id']}/done", headers=H)
    ov = client.get("/projects/physics + ml research/overview", headers=H).json()
    assert ov["tasks_done"] == 1 and ov["tasks_open"] == 2 and ov["progress_pct"] == 33
    assert [m["title"] for m in ov["milestones"]] == ["Draft paper"] and ov["next_milestone"]["title"] == "Draft paper"
    assert "Run baseline" in ov["recently_done"]
    listed = client.get("/tasks?project=physics + ml research", headers=H).json()
    assert {t["title"] for t in listed["tasks"]} == {"Draft paper", "Pick seeds"}


def test_new_project_created_by_task_and_notes_linked(client):
    t = client.post("/tasks", headers=H, json={"title": "Book venue", "project": "Hackathon 2026"}).json()
    assert t["project"] == "Hackathon 2026"
    client.post("/inbox", headers=H, json={"text": "idea: theme around climate", "kind": "idea", "project": "hackathon-2026!"})
    ov = client.get("/projects/hackathon 2026/overview", headers=H).json()
    assert any("climate" in n["text"] for n in ov["notes"])
    r = client.patch("/projects/hackathon 2026", headers=H, json={"status": "paused", "deadline": "in 30 days"})
    assert r.json()["status"] == "paused" and r.json()["days_left"] in (29, 30)
    assert all(p["name"] != "Hackathon 2026" for p in client.get("/projects", headers=H).json()["projects"])  # active only


def test_ambiguous_project_lookup(client):
    client.post("/projects", headers=H, json={"name": "SLM study A"})
    client.post("/projects", headers=H, json={"name": "SLM study B"})
    assert client.get("/projects/slm study/overview", headers=H).status_code == 409


# ---------- weekly review ----------
def test_weekly_review(client):
    t = client.post("/tasks", headers=H, json={"title": "Weekly done thing"}).json()
    client.post(f"/tasks/{t['id']}/done", headers=H)
    with pa.db() as c:  # a task whose deadline passed earlier this week (or today) and is still open
        start = pa.now_local().replace(hour=0, minute=0, second=1)
        c.execute("INSERT INTO tasks (title, due_at, due_has_time, created_at, updated_at) VALUES (?,?,1,?,?)",
                  ("Missed thing", pa.to_utc_iso(start), pa.now_iso(), pa.now_iso()))
    r = client.get("/review", headers=H).json()
    assert "Weekly done thing" in r["done"] and "Missed thing" in r["missed"]
    assert "📊 Week of" in r["text"] and "Missed deadlines" in r["text"] and "Physics + ML Research" in r["text"]


def test_briefing_payload_extras_and_weekly(client):
    p = pa.briefing_payload()
    assert any(e["section"].endswith("Project deadlines") and e["title"] == "Physics + ML Research" for e in p["extras"])
    assert p["weekly"]["text"].startswith("📊") and p["weekly"]["time_local"] == "18:00"


# ---------- follow-ups + applications (fake worker records scheduled/cancelled reminders) ----------
@pytest.fixture
def fake_worker(monkeypatch):
    calls = {"scheduled": [], "cancelled": []}
    n = {"i": 0}

    def fake(method, path, body=None):
        if method == "POST" and path == "/reminders":
            n["i"] += 1
            calls["scheduled"].append(body)
            return {"id": f"r{n['i']:08x}"}
        if path.endswith("/cancel"):
            calls["cancelled"].append(path.split("/")[2])
            return {"ok": True}
        return {"ok": True}
    monkeypatch.setattr(pa, "worker", fake)
    return calls


def test_followup_creates_reminder_and_done_cancels_it(client, fake_worker):
    r = client.post("/followups", headers=H, json={"person": "Prof. Rao", "about": "thesis draft", "when": "4 days", "channel": "Email"})
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["reminder_id"] == "r00000001" and "warning" not in f and f["channel"] == "email" and f["waiting_days"] == 0
    sent = fake_worker["scheduled"][0]
    due = datetime.fromisoformat(sent["due_utc"]).astimezone(pa.TZ)
    assert "Prof. Rao" in sent["text"] and due.date() == (pa.now_local() + timedelta(days=4)).date() and due.hour == 9
    assert any(x["id"] == f["id"] for x in client.get("/followups", headers=H).json()["followups"])
    done = client.post(f"/followups/{f['id']}/done", headers=H).json()
    assert done["status"] == "done" and fake_worker["cancelled"] == ["r00000001"]
    assert all(x["id"] != f["id"] for x in client.get("/followups", headers=H).json()["followups"])


def test_followup_saved_even_if_worker_down(client):
    r = client.post("/followups", headers=H, json={"person": "Recruiter", "about": "Amazon role", "when": "tomorrow"})
    assert r.status_code == 201 and r.json()["reminder_id"] is None and "could not be scheduled" in r.json()["warning"]
    client.post(f"/followups/{r.json()['id']}/cancel", headers=H)


def test_followup_bad_time(client, fake_worker):
    assert client.post("/followups", headers=H, json={"person": "x", "about": "y", "when": "blorp"}).status_code == 422


def test_application_lifecycle_with_interview_reminders(client, fake_worker):
    a = client.post("/applications", headers=H, json={"company": "Amazon", "role": "SDE intern", "link": "https://x"}).json()
    assert a["status"] == "applied" and a["applied_on"] == pa.today_local().isoformat()
    u = client.patch("/applications/amazon", headers=H, json={"status": "Interviewing", "next_step": "technical interview",
                                                               "next_when": "in 3 days 3pm"}).json()
    assert u["status"] == "interview" and u["reminders_set"] == 2 and "15:00" in u["next"]
    texts = [s["text"] for s in fake_worker["scheduled"]]
    assert any(t.startswith("Tomorrow: technical interview at Amazon") for t in texts) and any(t.startswith("In 1 hour") for t in texts)
    # rescheduling replaces the reminders
    client.patch(f"/applications/{a['id']}", headers=H, json={"next_when": "in 4 days 11am"})
    assert len(fake_worker["cancelled"]) == 2
    r = client.patch("/applications/amazon", headers=H, json={"status": "rejected"}).json()
    assert r["status"] == "rejected" and len(fake_worker["cancelled"]) == 4
    lst = client.get("/applications", headers=H).json()
    assert all(x["id"] != a["id"] for x in lst["applications"]) and lst["by_status"].get("rejected") == 1
    rev = client.get("/review", headers=H).json()
    assert any("Amazon → interview" in m for m in rev["applications"]["moved"]) or rev["applications"] is None


def test_application_lookup_rules(client):
    client.post("/applications", headers=H, json={"company": "Zoho", "role": "ML intern"})
    client.post("/applications", headers=H, json={"company": "Zoho", "role": "Backend intern"})
    assert client.patch("/applications/zoho", headers=H, json={"notes": "x"}).status_code == 409
    assert client.patch("/applications/zoho ml", headers=H, json={"notes": "x"}).status_code == 200
    assert client.post("/applications", headers=H, json={"company": "X", "status": "maybe"}).status_code == 422
    assert client.get("/applications?status=wishlist", headers=H).status_code == 200  # alias -> saved


def test_briefing_extras_followups_and_interviews(client, fake_worker):
    client.post("/followups", headers=H, json={"person": "Mentor", "about": "intro call", "when": "tomorrow"})
    client.post("/applications", headers=H, json={"company": "Infosys", "role": "Analyst"})
    client.patch("/applications/infosys", headers=H, json={"status": "interview", "next_step": "HR round", "next_when": "tomorrow 11am"})
    ex = pa.briefing_extras()
    assert any(e["section"].endswith("Follow-ups") and e["title"].startswith("Mentor") for e in ex)
    assert any(e["section"].endswith("Applications") and "HR round" in e["title"] for e in ex)


# ---------- Pair 3: focus sessions + triage ----------
def _backdate_focus(fid, minutes):
    start = (datetime.now(pa.timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with pa.db() as c:
        c.execute("UPDATE focus_sessions SET started_at=? WHERE id=?", (start, fid))


def test_focus_start_schedules_end_reminder_and_blocks_second(client, fake_worker):
    r = client.post("/focus/start", headers=H, json={"topic": "Research", "minutes": 50})
    assert r.status_code == 201, r.text
    f = r.json()
    sent = fake_worker["scheduled"][-1]
    due = datetime.fromisoformat(sent["due_utc"])
    assert "50 min of Research" in sent["text"] and "10-min break" in sent["text"]
    assert timedelta(minutes=49) <= due - datetime.now(pa.timezone.utc) <= timedelta(minutes=51)
    assert client.post("/focus/start", headers=H, json={"topic": "Other"}).status_code == 409
    assert client.post("/focus/distracted", headers=H).json()["distractions"] == 1
    _backdate_focus(f["id"], 48)
    s = client.post("/focus/stop", headers=H).json()
    assert s["status"] == "done" and 47 <= s["minutes"] <= 49 and f["reminder_id"] in fake_worker["cancelled"]
    assert client.post("/focus/stop", headers=H).status_code == 404


def test_focus_stopped_early_and_stats(client, fake_worker):
    f = client.post("/focus/start", headers=H, json={"topic": "Physics", "minutes": 25}).json()
    _backdate_focus(f["id"], 10)
    assert client.post("/focus/stop", headers=H).json()["status"] == "stopped"
    st = client.get("/focus/stats?period=today", headers=H).json()
    assert st["by_topic"]["physics"] >= 10 and st["running"] is None and st["total_minutes"] >= 10
    assert client.get("/focus/stats?period=nonsense", headers=H).json()["period"] == "week"


def test_forgotten_focus_session_is_closed_at_planned_end(client, fake_worker):
    f = client.post("/focus/start", headers=H, json={"topic": "Forgot to stop", "minutes": 25}).json()
    _backdate_focus(f["id"], 25 + 90)
    assert client.get("/focus/stats", headers=H).json()["running"] is None
    with pa.db() as c:
        r = c.execute("SELECT * FROM focus_sessions WHERE id=?", (f["id"],)).fetchone()
    assert r["status"] == "done" and pa._focus_minutes(r) == 25
    assert client.post("/focus/start", headers=H, json={"topic": "Next one"}).status_code == 201
    client.post("/focus/stop", headers=H)


def test_inbox_triage_flow(client, fake_worker):
    ids = [client.post("/inbox", headers=H, json={"text": t, "kind": "thought"}).json()["id"]
           for t in ("call the bank about card", "maybe try JAX for the sim", "old meme link")]
    tr = client.get("/inbox/triage", headers=H).json()
    assert tr["to_sort"] >= 3 and all(i in [x["id"] for x in tr["items"]] for i in ids)
    r = client.post(f"/inbox/{ids[0]}/to_reminder", headers=H, json={"when": "tomorrow 10am"}).json()
    assert r["reminder_id"] and fake_worker["scheduled"][-1]["text"] == "call the bank about card"
    k = client.post(f"/inbox/{ids[1]}/keep", headers=H, json={"project": "Physics ML"}).json()
    assert k["status"] == "processed" and k["project"] == "physics ml"
    assert client.post(f"/inbox/{ids[2]}/archive", headers=H).json()["status"] == "archived"
    left = [x["id"] for x in client.get("/inbox/triage", headers=H).json()["items"]]
    assert not set(ids) & set(left)
    assert client.post("/inbox/999999/archive", headers=H).status_code == 404


def test_weekly_review_mentions_focus(client, fake_worker):
    r = client.get("/review", headers=H).json()
    assert r["focus"]["minutes"] > 0 and "⏱ Focus" in r["text"]


# ---------- Pair 4: learning + spaced repetition ----------
@pytest.mark.parametrize("text,mins", [("90 min", 90), ("1h30", 90), ("1.5 hours", 90), ("an hour and a half", 90),
                                       ("half an hour", 30), ("45", 45), ("2 hours 15 minutes", 135), ("three hours", 180)])
def test_parse_duration(text, mins):
    assert pa.parse_duration(text) == mins


@pytest.mark.parametrize("text,mins", [("3 hours a week", 180), ("30 min a day", 210), ("5 hours per week", 300),
                                       ("2h", 120), ("", 0)])
def test_parse_weekly_target(text, mins):
    assert pa.parse_weekly_target(text) == mins


def test_parse_duration_rejects_nonsense():
    with pytest.raises(pa.HTTPException):
        pa.parse_duration("a while")


def test_learning_target_log_modules_and_focus_count(client, fake_worker):
    s = client.post("/learning", headers=H, json={"subject": "Machine Learning", "target": "3 hours a week"}).json()
    assert s["target_min_week"] == 180 and s["this_week_min"] == 0
    r = client.post("/learning/log", headers=H, json={"subject": "machine learning", "duration": "1h30", "note": "ch 3"}).json()
    assert r["logged_min"] == 90 and r["this_week_min"] == 90 and r["target_pct"] == 50 and r["streak_days"] == 1
    m = client.post("/learning/modules", headers=H, json={"subject": "Machine Learning",
                                                         "titles": "Linear models, Trees\nNeural nets"}).json()
    assert m["modules_total"] == 3 and m["next_module"] == "Linear models"
    d = client.post("/learning/modules/done", headers=H, json={"subject": "machine", "title": "linear"}).json()
    assert d["modules_done"] == 1 and d["next_module"] == "Trees"
    assert client.post("/learning/modules/done", headers=H, json={"subject": "machine", "title": "quantum"}).status_code == 404
    f = client.post("/focus/start", headers=H, json={"topic": "machine learning revision", "minutes": 25}).json()
    _backdate_focus(f["id"], 30)
    client.post("/focus/stop", headers=H)
    p = client.get("/learning?subject=Machine Learning", headers=H).json()
    assert p["this_week_min"] >= 119
    assert client.get("/learning?subject=chemistry", headers=H).status_code == 404
    assert any(x["name"] == "Machine Learning" for x in client.get("/learning", headers=H).json()["subjects"])


def test_revise_ladder_and_reset(client):
    c = client.post("/revise", headers=H, json={"topic": "Transformers attention", "first_in": "after 3 days"}).json()
    today = pa.today_local()
    assert c["due_on"] == (today + timedelta(days=3)).isoformat() and c["interval_days"] == 3
    assert c["id"] not in [x["id"] for x in client.get("/revise/due", headers=H).json()["cards"]]
    assert c["id"] in [x["id"] for x in client.get("/revise/due?days=3", headers=H).json()["cards"]]
    steps = []
    for _ in range(4):
        steps.append(client.post(f"/revise/{c['id']}", headers=H, json={"remembered": True}).json()["interval_days"])
    assert steps == [7, 14, 30, 60]
    assert client.post(f"/revise/{c['id']}", headers=H, json={"remembered": True}).json()["interval_days"] == 60
    r = client.post(f"/revise/{c['id']}", headers=H, json={"remembered": False}).json()
    assert r["interval_days"] == 1 and r["lapses"] == 1 and r["due_on"] == (today + timedelta(days=1)).isoformat()
    assert client.post(f"/revise/{c['id']}/retire", headers=H).json()["status"] == "retired"
    assert client.post("/revise/999999", headers=H, json={"remembered": True}).status_code == 404


def test_revise_due_today_shows_in_briefing(client):
    c = client.post("/revise", headers=H, json={"topic": "Backprop", "first_in": "tomorrow"}).json()
    with pa.db() as db:
        db.execute("UPDATE cards SET due_on=? WHERE id=?", (pa.today_local().isoformat(), c["id"]))
    assert c["id"] in [x["id"] for x in client.get("/revise/due", headers=H).json()["cards"]]
    assert any(e["section"] == "📚 Revise today" and e["title"] == "Backprop" for e in pa.briefing_extras())
    client.post(f"/revise/{c['id']}", headers=H, json={"remembered": True})
    rv = client.get("/review", headers=H).json()
    assert rv["cards_reviewed"] >= 1 and "📚 Study: Machine Learning" in rv["text"]


def test_agenda_buckets(client):
    client.post("/tasks", headers=H, json={"title": "Due today task", "due": "today 23:00"})
    client.post("/tasks", headers=H, json={"title": "Due in 3 days", "due": "in 3 days"})
    a = client.get("/agenda", headers=H).json()
    titles_today = [t["title"] for t in a["today"]]
    titles_soon = [t["title"] for t in a["next_7_days"]]
    if pa.now_local().hour < 23:  # 'today 23:00' is still in the future
        assert "Due today task" in titles_today
    assert "Due in 3 days" in titles_soon

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


def test_agenda_buckets(client):
    client.post("/tasks", headers=H, json={"title": "Due today task", "due": "today 23:00"})
    client.post("/tasks", headers=H, json={"title": "Due in 3 days", "due": "in 3 days"})
    a = client.get("/agenda", headers=H).json()
    titles_today = [t["title"] for t in a["today"]]
    titles_soon = [t["title"] for t in a["next_7_days"]]
    if pa.now_local().hour < 23:  # 'today 23:00' is still in the future
        assert "Due today task" in titles_today
    assert "Due in 3 days" in titles_soon

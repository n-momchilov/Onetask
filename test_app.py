"""End-to-end tests. Run: python3 -m pytest -q  (or python3 test_app.py)"""
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
SAMPLE_ICS = TMP / "uni.ics"
_soon = date.today() + timedelta(days=3)
_old = date.today() - timedelta(days=30)
SAMPLE_ICS.write_text(
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n"
    f"BEGIN:VEVENT\r\nUID:a1\r\nDTSTART;VALUE=DATE:{_soon:%Y%m%d}\r\nSUMMARY:Deadline ML report\r\nEND:VEVENT\r\n"
    f"BEGIN:VEVENT\r\nUID:a2\r\nDTSTART:{_soon:%Y%m%d}T090000Z\r\nSUMMARY:Lecture Data Eng\r\nEND:VEVENT\r\n"
    f"BEGIN:VEVENT\r\nUID:a3\r\nDTSTART;VALUE=DATE:{_old:%Y%m%d}\r\nSUMMARY:Deadline old\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)
os.environ.update(
    ONETHING_DATA=str(TMP),
    ONETHING_TOKEN="t0k",
    ONETHING_ICS_URLS=SAMPLE_ICS.as_uri(),
    ONETHING_ICS_KEYWORDS="deadline",
)
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402
from icalendar import Calendar  # noqa: E402

import app as A  # noqa: E402

H = {"X-Token": "t0k"}


def test_parse_capture():
    base = date(2026, 10, 17)  # Saturday
    assert A.parse_capture("uni: finish report due fri", base) == ("finish report", "uni", date(2026, 10, 23))
    assert A.parse_capture("side - deploy bot @tomorrow", base) == ("deploy bot", "side", date(2026, 10, 18))
    assert A.parse_capture("call the bank", base) == ("call the bank", "life", None)
    assert A.parse_capture("study chapter 4 by 20-10", base) == ("study chapter 4", "uni", date(2026, 10, 20))
    assert A.parse_capture("uni: exam @2026-11-02", base)[2] == date(2026, 11, 2)
    assert A.parse_capture("life: renew permit by 01/02", base)[2] == date(2027, 2, 1)
    # unknown due word stays in title
    assert A.parse_capture("buy milk due whenever", base) == ("buy milk due whenever", "life", None)


def test_level_and_streak():
    assert A.level_info(0) == (1, 0, 50)
    assert A.level_info(50)[0] == 2
    assert A.level_info(199)[0] == 2 and A.level_info(200)[0] == 3
    d = date(2026, 10, 17)
    s = {"2026-10-16": (2, 1), "2026-10-15": (1, 1), "2026-10-13": (1, 1)}
    assert A.streak(s, d) == 2           # today not done yet → counts from yesterday
    s["2026-10-17"] = (1, 1)
    assert A.streak(s, d) == 3
    s["2026-10-16"] = (2, 0)
    assert A.streak(s, d) == 1


def test_flow():
    with TestClient(A.app) as c:
        assert c.get("/api/today").status_code == 401
        assert c.get("/api/today?token=wrong").status_code == 401
        assert "One Thing" in c.get("/").text

        # import from calendar (keyword filter + old events skipped), idempotent
        r = c.post("/api/import", headers=H).json()
        assert r["imported"] == 1 and not r["errors"], r
        assert c.post("/api/import", headers=H).json()["fetched"] == 1
        p = c.get("/api/today", headers=H).json()
        assert [t["title"] for t in p["tasks"] if t["source"] == "ics"] == ["Deadline ML report"]

        # brain dump
        r = c.post("/api/capture", headers=H, json={"text": "uni: write intro due tomorrow\nside: deploy app\n\ncall bank"}).json()
        assert [x["kind"] for x in r["created"]] == ["uni", "side", "life"]
        p = c.get("/api/today", headers=H).json()
        assert p["inbox_count"] == 4 and p["picks"] == [] and p["xp"] == 0

        # pause text with no picks
        assert "No pick" in c.get("/today.txt?token=t0k").text

        ids = {t["title"]: t["id"] for t in p["tasks"]}
        p = c.post("/api/pick", headers=H, json={"task_ids": [ids["write intro"], ids["deploy app"], ids["call bank"], ids["Deadline ML report"]]}).json()
        assert len(p["picks"]) == 3  # capped
        assert [t["kind"] for t in p["picks"]] == ["uni", "side", "life"]

        txt = c.get("/today.txt?token=t0k").text
        assert "○ UNI: write intro" in txt and "3 left" in txt

        # done / undo / done → xp consistent
        p = c.post(f"/api/tasks/{ids['write intro']}/done", headers=H).json()
        assert p["xp"] == 30 and p["streak"] == 1
        p = c.post(f"/api/tasks/{ids['write intro']}/done?undo=true", headers=H).json()
        assert p["xp"] == 0 and p["streak"] == 0
        for k in ("write intro", "deploy app"):
            p = c.post(f"/api/tasks/{ids[k]}/done", headers=H).json()
        p = c.post(f"/api/tasks/{ids['deploy app']}/done", headers=H).json()  # double done = no double xp
        assert p["xp"] == 50 and p["level"] == 2

        # close with one pick left → no bonus
        p = c.post("/api/close", headers=H, json={"note": "intro written"}).json()
        assert p["bonus"] == 0 and p["closed"] and p["note"] == "intro written"
        # finish last, close again → bonus once
        c.post(f"/api/tasks/{ids['call bank']}/done", headers=H)
        p = c.post("/api/close", headers=H, json={"note": ""}).json()
        assert p["bonus"] == 20 and p["xp"] == 80 and p["note"] == "intro written"
        assert c.post("/api/close", headers=H, json={}).json()["bonus"] == 0
        assert "All done" in c.get("/today.txt?token=t0k").text

        # goals
        p = c.put("/api/goals", headers=H, json={"kind": "uni", "why": "Graduate on time"}).json()
        assert p["goals"]["uni"] == "Graduate on time"
        assert c.put("/api/goals", headers=H, json={"kind": "bad", "why": "x"}).status_code == 400

        # patch + delete
        p = c.patch(f"/api/tasks/{ids['Deadline ML report']}", headers=H, json={"due": "2026-12-01", "title": "ML report"}).json()
        t = next(t for t in p["tasks"] if t["id"] == ids["Deadline ML report"])
        assert t["due"] == "2026-12-01" and t["title"] == "ML report"
        assert c.patch("/api/tasks/9999", headers=H, json={}).status_code == 404
        c.post("/api/capture", headers=H, json={"text": "junk"})
        junk = next(t for t in c.get("/api/today", headers=H).json()["tasks"] if t["title"] == "junk")
        c.delete(f"/api/tasks/{junk['id']}", headers=H)
        assert all(t["title"] != "junk" for t in c.get("/api/today", headers=H).json()["tasks"])

        # calendar feed parses and contains today's picks + upcoming captured deadlines
        c.post("/api/capture", headers=H, json={"text": "uni: thesis draft due 2026-12-24" if date.today() < date(2026, 11, 24) else "uni: thesis draft due tomorrow"})
        r = c.get("/feed.ics?token=t0k")
        assert r.headers["content-type"].startswith("text/calendar")
        cal = Calendar.from_ical(r.content)
        summaries = [str(e["SUMMARY"]) for e in cal.walk("VEVENT")]
        assert any(s.startswith("✅ UNI: write intro") for s in summaries), summaries
        assert len({str(e["UID"]) for e in cal.walk("VEVENT")}) == len(summaries)


if __name__ == "__main__":
    test_parse_capture()
    test_level_and_streak()
    test_flow()
    print("all tests passed")

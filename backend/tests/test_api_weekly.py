"""Read APIs, calendar, report and the weekly carry-forward job."""
import csv
import io
from datetime import timedelta

from conftest import EMAILS, EMP, HR, MGR, YEAR
from common import db
from common.http import parse_groups


def approve_via_manager(world):
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    return run


def test_parse_groups_formats():
    assert parse_groups("Manager,HRAdmin") == {"Manager", "HRAdmin"}
    assert parse_groups("[Manager HRAdmin]") == {"Manager", "HRAdmin"}
    assert parse_groups(None) == set()


def test_me_and_balances(world, monday):
    world.submit("CASUAL", monday, monday + timedelta(days=1))
    code, me = world.call_api("GET", "/me")
    assert code == 200 and me["employee_id"] == EMP and me["is_manager"] is False
    code, data = world.call_api("GET", "/me/balances")
    assert code == 200 and data["year"] == YEAR
    casual = next(b for b in data["balances"] if b["leave_type"] == "CASUAL")
    assert casual == {"leave_type": "CASUAL", "name": "Casual leave", "requires_balance": True, "allocated": 10,
                      "carried_forward": 0, "used": 0, "available": 10, "pending": 2, "usable": 8}
    unpaid = next(b for b in data["balances"] if b["leave_type"] == "UNPAID")
    assert unpaid["usable"] is None


def test_history_hides_internal_fields(world, monday):
    world.submit("CASUAL", monday, monday)
    world.start_workflow()
    code, data = world.call_api("GET", "/me/leaves")
    assert code == 200 and len(data["requests"]) == 1
    item = data["requests"][0]
    assert "task_token" not in item and "execution_arn" not in item
    assert item["status"] == "PENDING_MANAGER"


def test_pending_list_for_manager_and_hr(world, monday):
    world.submit("CASUAL", monday, monday)
    world.start_workflow()
    code, data = world.call_api("GET", "/approvals/pending", MGR, ("Manager",))
    assert code == 200 and len(data["pending"]) == 1 and data["pending"][0]["my_level"] == "MANAGER"
    code, data = world.call_api("GET", "/approvals/pending", HR, ("HRAdmin",))
    assert data["pending"] == []  # not HR's step
    assert world.call_api("GET", "/approvals/pending", EMP, ("Employee",))[0] == 403

    # Long leave reaches HR after manager approval
    _, body = world.submit("EARNED", monday + timedelta(days=14), monday + timedelta(days=23))
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    code, data = world.call_api("GET", "/approvals/pending", HR, ("HRAdmin",))
    assert [p["request_id"] for p in data["pending"]] == [body["request_id"]]
    assert data["pending"][0]["my_level"] == "HR"


def test_calendar_and_report(world, monday):
    _, body = world.submit("CASUAL", monday, monday + timedelta(days=1))
    approve_via_manager(world)
    world.set_balance(EMP, "SICK", available=0, used=12)
    world.submit("SICK", monday + timedelta(days=7), monday + timedelta(days=7))  # auto-rejected

    q = {"from": (monday - timedelta(days=3)).isoformat(), "to": (monday + timedelta(days=30)).isoformat()}
    code, cal = world.call_api("GET", "/calendar", MGR, ("Manager",), query=q)
    assert code == 200
    assert [(e["employee_id"], e["status"]) for e in cal["entries"]] == [(EMP, "APPROVED")]
    code, cal = world.call_api("GET", "/calendar", HR, ("HRAdmin",), query=q)
    assert len(cal["entries"]) == 1
    assert world.call_api("GET", "/calendar", EMP, ("Employee",), query=q)[0] == 403
    # a range that ends before the leave starts shows nothing
    code, cal = world.call_api("GET", "/calendar", HR, ("HRAdmin",),
                               query={"from": q["from"], "to": (monday - timedelta(days=1)).isoformat()})
    assert cal["entries"] == []

    code, rep = world.call_api("GET", "/reports/leaves", HR, ("HRAdmin",), query=q)
    assert code == 200 and rep["rows"] == 2 and rep["url"].startswith("https://reports-bucket")
    csv_text = next(iter(world.s3.objects.values())).decode()
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert {r["status"] for r in rows} == {"APPROVED", "AUTO_REJECTED"}
    assert rows[0]["request_id"] == body["request_id"]

    assert world.call_api("GET", "/calendar", HR, ("HRAdmin",),
                          query={"from": "2026-12-01", "to": "2026-11-01"})[0] == 400


def test_unknown_route(world):
    assert world.call_api("GET", "/nope")[0] == 404


# ---------------------------------------------------------------- weekly job

def test_weekly_job_carries_forward_once(world):
    from handlers import weekly_balance
    prev = YEAR - 1
    world.set_balance(EMP, "EARNED", available=14, used=4, year=prev)   # 14 unused, cap is 10
    world.set_balance(MGR, "EARNED", available=3, used=15, year=prev)   # 3 unused
    world.set_balance(EMP, "CASUAL", available=6, used=4, year=prev)    # casual: no carry-forward

    summary = weekly_balance.handler({}, None)
    assert summary["errors"] == [] and summary["employees"] == 3 and summary["emails_sent"] == 3
    emp_earned = world.balance(EMP, "EARNED")
    assert db.num(emp_earned["carried_forward"]) == 10 and db.num(emp_earned["available"]) == 28
    assert db.num(world.balance(MGR, "EARNED")["available"]) == 21
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 10
    assert db.num(world.balance(HR, "EARNED")["carried_forward"]) == 0
    assert world.balance(EMP, "EARNED", year=prev)["carry_forward_done"] is True

    # Running again (next week) must not credit anything twice
    again = weekly_balance.handler({}, None)
    assert again["balances_created"] == 0 and again["carried_forward_days"] == 0
    assert db.num(world.balance(EMP, "EARNED")["available"]) == 28
    summary_mail = world.ses.to(EMAILS[EMP])[-1]
    assert "balance summary" in summary_mail["subject"] and "28 available" in summary_mail["text"]


def test_submit_in_new_year_before_weekly_job_still_carries_forward(world, monday):
    from handlers import weekly_balance
    world.set_balance(EMP, "EARNED", available=5, used=13, year=YEAR - 1)
    code, _ = world.submit("EARNED", monday, monday)  # creates this year's record first
    assert code == 201
    assert db.num(world.balance(EMP, "EARNED")["available"]) == 23
    weekly_balance.handler({}, None)
    assert db.num(world.balance(EMP, "EARNED")["available"]) == 23  # not 28


def test_weekly_dry_run_sends_no_email(world):
    from handlers import weekly_balance
    summary = weekly_balance.handler({"dry_run": True}, None)
    assert summary["emails_sent"] == 0 and world.ses.sent == []

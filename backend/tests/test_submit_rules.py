"""Submission rules: quota enforcement, overlaps, notice, config-driven behaviour."""
from datetime import date, timedelta

from conftest import EMAILS, EMP, FAKE_TODAY, MGR
from common import db
from common.dates import working_days


# ---------------------------------------------------------------- balance

def test_insufficient_balance_is_auto_rejected(world, monday):
    world.set_balance(EMP, "CASUAL", available=2, used=8)
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=2))
    assert code == 422 and body["status"] == "AUTO_REJECTED"
    assert "Insufficient Casual leave balance" in body["reason"]
    assert "requested 3" in body["reason"] and "only 2" in body["reason"]
    assert world.sfn.started == []  # never reaches the approval workflow
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "AUTO_REJECTED" and req["rejection_reason"] == body["reason"]
    mail = [m for m in world.ses.to(EMAILS[EMP]) if "auto-rejected" in m["subject"]]
    assert mail and "Insufficient" in mail[0]["text"]
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 2


def test_exact_balance_is_allowed(world, monday):
    world.set_balance(EMP, "CASUAL", available=3, used=7)
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=2))
    assert code == 201, body


def test_pending_requests_count_against_balance(world, monday):
    # 18 earned days; 8 pending; asking for 11 more must fail (18 - 8 = 10)
    code, first = world.submit("EARNED", monday, monday + timedelta(days=9))
    assert code == 201 and first["request"]["days"] == 8
    later = monday + timedelta(days=14)  # Monday two weeks later
    code, second = world.submit("EARNED", later, later + timedelta(days=14))  # 11 working days
    assert code == 422 and "8 day(s) are already pending" in second["reason"]
    assert "only 10" in second["reason"]


def test_balance_record_created_from_config_on_first_use(world, monday):
    assert world.balance(EMP, "SICK") is None
    code, _ = world.submit("SICK", monday, monday)
    assert code == 201
    bal = world.balance(EMP, "SICK")
    assert db.num(bal["allocated"]) == 12 and db.num(bal["available"]) == 12


# ---------------------------------------------------------------- overlaps

def test_overlap_with_pending_request(world, monday):
    world.submit("CASUAL", monday, monday + timedelta(days=2))
    code, body = world.submit("SICK", monday + timedelta(days=2), monday + timedelta(days=3))
    assert code == 422 and "overlap" in body["reason"]
    assert body["request"]["status"] == "AUTO_REJECTED"


def test_duplicate_submission_is_blocked(world, monday):
    world.submit("CASUAL", monday, monday)
    code, body = world.submit("CASUAL", monday, monday)
    assert code == 422 and "overlap your pending manager request" in body["reason"]


def test_adjacent_dates_do_not_overlap(world, monday):
    world.submit("CASUAL", monday, monday + timedelta(days=1))
    code, _ = world.submit("CASUAL", monday + timedelta(days=2), monday + timedelta(days=3))
    assert code == 201


def test_overlap_with_approved_request(world, monday):
    _, body = world.submit("CASUAL", monday, monday)
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    code, again = world.submit("SICK", monday - timedelta(days=3), monday)  # Fri..Mon
    assert code == 422 and "approved request" in again["reason"]


def test_rejected_dates_can_be_requested_again(world, monday):
    _, body = world.submit("CASUAL", monday, monday)
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[1])
    run.resume()
    code, _ = world.submit("CASUAL", monday, monday)
    assert code == 201


def test_auto_rejected_request_does_not_block_dates(world, monday):
    world.set_balance(EMP, "CASUAL", available=0, used=10)
    code, _ = world.submit("CASUAL", monday, monday)
    assert code == 422
    code, _ = world.submit("SICK", monday, monday)
    assert code == 201


# ---------------------------------------------------------------- dates & rules

def test_working_days_skip_weekends_and_holidays():
    fri = date(2026, 11, 6)
    assert len(working_days(fri, fri + timedelta(days=3))) == 2  # Fri + Mon
    assert len(working_days(fri, fri + timedelta(days=3), ["2026-11-09"])) == 1


def test_holidays_from_config_are_not_counted(world, monday):
    world.db.Table("t-config").put_item(Item={"leave_type": f"SYSTEM#HOLIDAYS#{monday.year}",
                                              "dates": [monday.isoformat()]})
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=2))
    assert code == 201 and body["request"]["days"] == 2


def test_weekend_only_request_rejected_as_bad_input(world, monday):
    sat = monday - timedelta(days=2)
    code, body = world.submit("CASUAL", sat, sat + timedelta(days=1))
    assert code == 400 and "no working days" in body["error"]


def test_bad_input(world, monday):
    assert world.submit("CASUAL", monday, monday - timedelta(days=1))[0] == 400
    assert world.submit("BOGUS", monday, monday)[0] == 400
    assert world.submit("SYSTEM#SETTINGS", monday, monday)[0] == 400
    assert world.submit("CASUAL", "2026-13-01", "2026-13-02")[0] == 400
    assert world.submit("CASUAL", date(2026, 12, 31), date(2027, 1, 1))[0] == 400
    code, body = world.submit("CASUAL", monday, monday, reason="x" * 501)
    assert code == 400


def test_notice_period(world):
    # Casual leave needs 1 day notice; FAKE_TODAY is a Wednesday
    code, body = world.submit("CASUAL", FAKE_TODAY, FAKE_TODAY)
    assert code == 422 and "notice" in body["reason"]
    code, _ = world.submit("CASUAL", FAKE_TODAY + timedelta(days=1), FAKE_TODAY + timedelta(days=1))
    assert code == 201


def test_backdated_sick_leave_allowed_within_limit(world):
    code, _ = world.submit("SICK", FAKE_TODAY - timedelta(days=2), FAKE_TODAY - timedelta(days=1))
    assert code == 201
    code, body = world.submit("SICK", FAKE_TODAY - timedelta(days=10), FAKE_TODAY - timedelta(days=9))
    assert code == 422 and "at most 7 day(s)" in body["reason"]
    code, body = world.submit("CASUAL", FAKE_TODAY - timedelta(days=1), FAKE_TODAY - timedelta(days=1))
    assert code == 422 and "past dates" in body["reason"]


def test_max_consecutive_days(world, monday):
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=7))  # 6 working days > 5
    assert code == 422 and "at most 5 working day(s)" in body["reason"]


# ---------------------------------------------------------------- config driven (no code change)

def test_hr_can_change_rules_in_config(world, monday):
    cfg = world.db.Table("t-config")
    cfg.update_item(Key={"leave_type": "CASUAL"}, UpdateExpression="SET max_consecutive_days = :m",
                    ExpressionAttributeValues={":m": 2})
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=2))
    assert code == 422 and "at most 2" in body["reason"]

    cfg.update_item(Key={"leave_type": "SYSTEM#SETTINGS"}, UpdateExpression="SET hr_approval_threshold_days = :t",
                    ExpressionAttributeValues={":t": 1})
    code, body = world.submit("SICK", monday, monday + timedelta(days=1))
    assert code == 201 and body["request"]["requires_hr"] is True


def test_new_leave_type_added_by_hr_works_immediately(world, monday):
    world.db.Table("t-config").put_item(Item={
        "leave_type": "PATERNITY", "name": "Paternity leave", "annual_quota": 10, "requires_balance": True,
        "carry_forward": False, "max_consecutive_days": 10, "active": True})
    code, types = world.call_api("GET", "/config/leave-types")
    assert "PATERNITY" in [t["leave_type"] for t in types["leave_types"]]
    code, body = world.submit("PATERNITY", monday, monday + timedelta(days=4))
    assert code == 201 and db.num(world.balance(EMP, "PATERNITY")["available"]) == 10


def test_inactive_leave_type_rejected(world, monday):
    world.db.Table("t-config").update_item(Key={"leave_type": "CASUAL"}, UpdateExpression="SET active = :f",
                                           ExpressionAttributeValues={":f": False})
    assert world.submit("CASUAL", monday, monday)[0] == 400


# ---------------------------------------------------------------- failures

def test_workflow_start_failure_is_reported(world, monday):
    world.sfn.fail_start = True
    code, body = world.submit("CASUAL", monday, monday)
    assert code == 500
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "AUTO_REJECTED" and "pending_level" not in req
    # the dates are free again
    world.sfn.fail_start = False
    assert world.submit("CASUAL", monday, monday)[0] == 201


def test_email_failure_does_not_break_submission(world, monday):
    world.ses.fail_for.add(EMAILS[EMP])
    code, _ = world.submit("CASUAL", monday, monday)
    assert code == 201


def test_unlinked_user_and_missing_manager(world, monday):
    from handlers import submit
    event = world.api_event("POST", "/leaves", body={})
    event["requestContext"]["authorizer"]["claims"].pop("custom:employee_id")
    assert submit.handler(event, None)["statusCode"] == 403
    world.db.Table("t-employees").put_item(Item={"employee_id": "emp-x", "name": "X", "email": "x@example.com"})
    code, body = world.submit("CASUAL", monday, monday, employee_id="emp-x")
    assert code == 400 and "no manager" in body["error"]


def test_invalid_json_body(world):
    from handlers import submit
    event = world.api_event("POST", "/leaves")
    event["body"] = "{not json"
    assert submit.handler(event, None)["statusCode"] == 400

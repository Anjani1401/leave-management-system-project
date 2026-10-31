"""End-to-end: submit -> Step Functions (real ASL) -> email links -> final state."""
import json
from datetime import timedelta

from conftest import EMAILS, EMP, HR, MGR, YEAR
from common import db, tokens


def test_short_leave_manager_approves(world, monday):
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=2))
    assert code == 201, body
    rid = body["request_id"]
    assert body["request"]["days"] == 3 and body["request"]["requires_hr"] is False

    # Submitting does NOT touch the balance
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 10
    assert any("submitted" in m["subject"].lower() for m in world.ses.to(EMAILS[EMP]))

    run = world.start_workflow()
    assert run.state == "NotifyManager" and run.waiting_token
    req = world.request(EMP, rid)
    assert req["status"] == "PENDING_MANAGER" and req["pending_level"] == "MANAGER"
    assert req["task_token"] == run.waiting_token

    approve, _ = world.links_for(EMAILS[MGR])
    get, post = world.click_link(approve, comment="Enjoy")
    assert get["statusCode"] == 200 and "Confirm: Approve" in get["body"]
    assert post["statusCode"] == 200 and "Request approved" in post["body"]

    run.resume()
    assert run.status == "SUCCEEDED"
    assert run.path == ["NotifyManager", "ManagerDecision", "FinalApprove", "Approved"]

    req = world.request(EMP, rid)
    assert req["status"] == "APPROVED" and req["approved_by"] == MGR
    assert "task_token" not in req and "pending_level" not in req
    bal = world.balance(EMP, "CASUAL")
    assert db.num(bal["available"]) == 7 and db.num(bal["used"]) == 3
    assert any("approved" in m["subject"].lower() for m in world.ses.to(EMAILS[EMP]))


def test_long_leave_needs_hr_and_hr_approves(world, monday):
    end = monday + timedelta(days=9)  # Mon..Wed next week = 8 working days
    code, body = world.submit("EARNED", monday, end)
    assert code == 201 and body["request"]["days"] == 8 and body["request"]["requires_hr"] is True
    run = world.start_workflow()

    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    assert run.state == "NotifyHR" and run.waiting_token
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "PENDING_HR" and req["pending_level"] == "HR"
    assert db.num(world.balance(EMP, "EARNED")["available"]) == 18  # still untouched
    assert any("awaiting hr" in m["subject"].lower() for m in world.ses.to(EMAILS[EMP]))

    world.click_link(world.links_for(EMAILS[HR])[0])
    run.resume()
    assert run.path == ["NotifyManager", "ManagerDecision", "NotifyHR", "HRDecision", "FinalApprove", "Approved"]
    assert world.request(EMP, body["request_id"])["status"] == "APPROVED"
    assert db.num(world.balance(EMP, "EARNED")["available"]) == 10


def test_long_leave_hr_rejects_balance_unchanged(world, monday):
    code, body = world.submit("EARNED", monday, monday + timedelta(days=9))
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    world.click_link(world.links_for(EMAILS[HR])[1], comment="Quarter-end freeze")
    run.resume()
    assert run.path[-2:] == ["RejectByHR", "Rejected"]
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "REJECTED" and "Quarter-end freeze" in req["rejection_reason"]
    assert db.num(world.balance(EMP, "EARNED")["available"]) == 18


def test_manager_rejects(world, monday):
    code, body = world.submit("CASUAL", monday, monday)
    run = world.start_workflow()
    _, post = world.click_link(world.links_for(EMAILS[MGR])[1], comment="Release week")
    assert "Request rejected" in post["body"]
    run.resume()
    assert run.path == ["NotifyManager", "ManagerDecision", "RejectByManager", "Rejected"]
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "REJECTED"
    assert req["rejection_reason"] == "Rejected by your manager (Meena Rao): Release week"
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 10
    rejected_mail = [m for m in world.ses.to(EMAILS[EMP]) if "rejected" in m["subject"].lower()]
    assert rejected_mail and "Release week" in rejected_mail[-1]["text"]


def test_manager_inaction_escalates_to_hr_then_hr_approves(world, monday):
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=1))
    run = world.start_workflow()
    old_approve, _ = world.links_for(EMAILS[MGR])

    run.timeout()  # 48 hours pass with no decision
    assert run.state == "EscalateToHR" and run.waiting_token
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "PENDING_HR" and req["pending_level"] == "ESCALATION" and req["escalated"] is True
    assert any("escalated" in m["subject"].lower() for m in world.ses.to(EMAILS[EMP]))

    # The manager's old link no longer works
    get, post = world.click_link(old_approve)
    assert "Already processed" in get["body"]
    assert post["statusCode"] == 409

    world.click_link(world.links_for(EMAILS[HR])[0])
    run.resume()
    assert run.path == ["NotifyManager", "EscalateToHR", "HRDecision", "FinalApprove", "Approved"]
    assert world.request(EMP, body["request_id"])["status"] == "APPROVED"
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 8


def test_no_response_from_anyone_auto_rejects(world, monday):
    code, body = world.submit("CASUAL", monday, monday)
    run = world.start_workflow()
    run.timeout()
    run.timeout()
    assert run.path == ["NotifyManager", "EscalateToHR", "RejectTimeout", "Rejected"]
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "REJECTED" and req["rejection_stage"] == "TIMEOUT"
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 10


def test_hr_step_timeout_rejects(world, monday):
    code, body = world.submit("EARNED", monday, monday + timedelta(days=9))
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    run.timeout()
    assert run.path[-2:] == ["RejectTimeout", "Rejected"]
    assert world.request(EMP, body["request_id"])["status"] == "REJECTED"


def test_double_click_is_idempotent(world, monday):
    code, body = world.submit("CASUAL", monday, monday)
    run = world.start_workflow()
    approve, reject = world.links_for(EMAILS[MGR])
    _, first = world.click_link(approve)
    assert first["statusCode"] == 200
    _, second = world.click_link(approve)
    assert second["statusCode"] == 409
    _, other = world.click_link(reject)  # reject after approve is also refused
    assert other["statusCode"] == 409
    run.resume()
    assert world.request(EMP, body["request_id"])["status"] == "APPROVED"
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 9  # deducted exactly once


def test_get_on_link_never_changes_anything(world, monday):
    world.submit("CASUAL", monday, monday)
    world.start_workflow()
    approve, _ = world.links_for(EMAILS[MGR])
    get, _ = world.click_link(approve, confirm=False)
    assert get["statusCode"] == 200
    assert world.sfn.task_results == {}  # nothing was sent to Step Functions


def test_tampered_and_expired_links(world, monday):
    from handlers import decision_link
    world.submit("CASUAL", monday, monday)
    world.start_workflow()
    approve, _ = world.links_for(EMAILS[MGR])
    token = approve.split("t=")[1]
    payload, sig = token.split(".")
    tampered = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB") + "." + sig
    resp = decision_link.handler({"httpMethod": "GET", "queryStringParameters": {"t": tampered}}, None)
    assert resp["statusCode"] == 400 and "invalid" in resp["body"].lower()

    expired = tokens.sign({"e": EMP, "r": "x", "l": "MANAGER", "a": "APPROVED", "u": MGR}, ttl_seconds=-10)
    resp = decision_link.handler({"httpMethod": "GET", "queryStringParameters": {"t": expired}}, None)
    assert resp["statusCode"] == 400 and "expired" in resp["body"].lower()

    resp = decision_link.handler({"httpMethod": "GET", "queryStringParameters": None}, None)
    assert resp["statusCode"] == 400


def test_balance_changed_before_final_approval(world, monday):
    code, body = world.submit("CASUAL", monday, monday + timedelta(days=2))
    run = world.start_workflow()
    world.set_balance(EMP, "CASUAL", available=1, used=9)  # e.g. HR corrected the balance meanwhile
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    assert run.path[-3:] == ["FinalApprove", "RejectBalanceChanged", "Rejected"]
    req = world.request(EMP, body["request_id"])
    assert req["status"] == "REJECTED" and req["rejection_stage"] == "BALANCE"
    assert db.num(world.balance(EMP, "CASUAL")["available"]) == 1  # never negative, unchanged


def test_unpaid_leave_no_balance_needed(world, monday):
    code, body = world.submit("UNPAID", monday, monday + timedelta(days=3))
    assert code == 201
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    assert run.status == "SUCCEEDED" and run.path[-1] == "Approved"
    bal = world.balance(EMP, "UNPAID")
    assert db.num(bal["used"]) == 4 and db.num(bal["available"]) == 0


def test_hr_admin_cannot_approve_own_long_leave(world, monday):
    # HR's own long leave: manager approves, but there is no *other* HR Admin
    code, body = world.submit("EARNED", monday, monday + timedelta(days=9), employee_id=HR)
    assert code == 201
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    assert run.path[-3:] == ["NotifyHR", "RejectNoApprover", "Rejected"]
    assert world.request(HR, body["request_id"])["rejection_stage"] == "NO_APPROVER"


def test_in_app_approval_resumes_workflow(world, monday):
    code, body = world.submit("CASUAL", monday, monday)
    run = world.start_workflow()
    path = {"employee_id": EMP, "request_id": body["request_id"]}

    # Employee can't approve; a different manager can't either
    code, resp = world.call_api("POST", "/approvals/{employee_id}/{request_id}", EMP, ("Employee",),
                                body={"decision": "APPROVED"}, path_params=path)
    assert code == 403
    code, resp = world.call_api("POST", "/approvals/{employee_id}/{request_id}", HR, ("HRAdmin",),
                                body={"decision": "APPROVED"}, path_params=path)
    assert code == 403  # HR is not the approver at the manager step

    code, resp = world.call_api("POST", "/approvals/{employee_id}/{request_id}", MGR, ("Manager",),
                                body={"decision": "APPROVED", "comment": "ok"}, path_params=path)
    assert code == 200, resp
    run.resume()
    assert run.status == "SUCCEEDED" and world.request(EMP, body["request_id"])["status"] == "APPROVED"

    # The email link for the same step is now dead
    _, post = world.click_link(world.links_for(EMAILS[MGR])[0])
    assert post["statusCode"] == 409


def test_workflow_input_and_output_are_json(world, monday):
    world.submit("CASUAL", monday, monday)
    started = world.sfn.started[-1]
    assert started["name"].startswith("LR-") and len(started["name"]) <= 80
    assert started["input"]["config"]["approval_timeout_seconds"] == 172800
    run = world.start_workflow()
    world.click_link(world.links_for(EMAILS[MGR])[0])
    run.resume()
    json.dumps(run.output)


def test_year_is_recorded(world, monday):
    _, body = world.submit("CASUAL", monday, monday)
    assert world.request(EMP, body["request_id"])["year"] == YEAR

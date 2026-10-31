"""Step Functions task (waitForTaskToken): tell an approver a decision is needed.

Input from the state machine:
  {"taskToken": "...", "request": {...}, "level": "MANAGER" | "HR" | "ESCALATION",
   "timeout_seconds": 172800}

The workflow pauses until someone calls SendTaskSuccess with this task token
(done by the decision endpoint) or until the timeout fires.
"""
import logging
import os
from urllib.parse import quote

from common import db, leave_rules, notify, tokens
from common.dates import now_iso

log = logging.getLogger()
log.setLevel(logging.INFO)


class NoApproverError(Exception):
    pass


class RequestNotPendingError(Exception):
    pass


def handler(event, context):
    task_token = event["taskToken"]
    ref = event["request"]
    level = event["level"]
    timeout = int(event.get("timeout_seconds") or os.environ.get("APPROVAL_TIMEOUT_SECONDS", "172800"))

    req = leave_rules.get_request(ref["employee_id"], ref["request_id"])
    if not req or req.get("status") not in db.PENDING_STATUSES:
        raise RequestNotPendingError(f"Request {ref['request_id']} is not pending")

    if level == db.LEVEL_MANAGER:
        manager = leave_rules.get_employee(req.get("manager_id"))
        approvers = [manager] if manager and manager.get("email") else []
        new_status = db.PENDING_MANAGER
    else:
        approvers = [a for a in leave_rules.list_hr_admins()
                     if a.get("email") and a["employee_id"] != req["employee_id"]]
        new_status = db.PENDING_HR
    if not approvers:
        raise NoApproverError(f"No approver found for level {level} on {req['request_id']}")

    now = now_iso()
    note = {
        db.LEVEL_MANAGER: "Waiting for manager approval",
        db.LEVEL_HR: "Manager approved; waiting for HR Admin approval",
        db.LEVEL_ESCALATION: "Manager did not respond in time; escalated to HR Admin",
    }[level]
    db.requests_table().update_item(
        Key={"employee_id": req["employee_id"], "request_id": req["request_id"]},
        UpdateExpression=("SET task_token = :tt, pending_level = :lvl, #s = :s, status_created = :sc, "
                          "pending_since = :now, updated_at = :now, escalated = :esc, "
                          "history = list_append(history, :h)"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":tt": task_token,
            ":lvl": level,
            ":s": new_status,
            ":sc": f"{new_status}#{req['created_at']}",
            ":now": now,
            ":esc": level == db.LEVEL_ESCALATION or bool(req.get("escalated")),
            ":h": leave_rules.history_entry(new_status, "system", note),
        },
    )

    base_url = os.environ["DECISION_BASE_URL"].rstrip("/")
    hours = round(timeout / 3600, 1)
    notified = []
    for approver in approvers:
        links = {}
        for action in ("APPROVED", "REJECTED"):
            token = tokens.sign(
                {"e": req["employee_id"], "r": req["request_id"], "l": level,
                 "a": action, "u": approver["employee_id"]},
                ttl_seconds=timeout + 86400,
            )
            links[action] = f"{base_url}/decision?t={quote(token)}"

        who = {db.LEVEL_MANAGER: "as their manager",
               db.LEVEL_HR: "as HR Admin (leave longer than the HR threshold, manager already approved)",
               db.LEVEL_ESCALATION: "as HR Admin, because the manager did not respond in time"}[level]
        subject = f"Approval needed: {req.get('employee_name')} - {req.get('leave_type_name')} ({req.get('days')} days)"
        intro = (f"{req.get('employee_name')} has requested leave and needs your decision {who}. "
                 f"Please respond within {hours:g} hours.")

        # SNS notification to the approver (required by the brief)
        text = "\n".join(
            [intro, ""] + [f"{k}: {v}" for k, v in notify.request_rows(req)]
            + ["", f"APPROVE: {links['APPROVED']}", "", f"REJECT: {links['REJECTED']}"]
        )
        sns_ok = notify.publish_to_approver(approver["email"], subject, text)

        # Same message as a formatted SES email (buttons instead of raw links)
        ses_ok = False
        if os.environ.get("APPROVER_EMAIL_VIA_SES", "true").lower() == "true":
            ses_ok = notify.send_email(
                approver["email"], subject, "Leave approval needed", notify.request_rows(req),
                intro=intro,
                buttons=[("Approve", links["APPROVED"], "#1f7a4d"), ("Reject", links["REJECTED"], "#b42318")],
                footer="Each link opens a confirmation page. Links expire automatically.",
            )
        if sns_ok or ses_ok:
            notified.append(approver["employee_id"])

    if not notified:
        raise NoApproverError(f"Could not notify any approver for {req['request_id']}")

    # Tell the employee about the stage change.
    if level == db.LEVEL_HR:
        notify.email_employee(req, f"Manager approved - awaiting HR: {req.get('leave_type_name')} {req.get('start_date')}",
                              "Your manager approved your leave",
                              "Because this leave is longer than the HR threshold, it now needs HR Admin approval.",
                              extra_rows=[("Status", "Pending HR approval")])
    elif level == db.LEVEL_ESCALATION:
        notify.email_employee(req, f"Leave request escalated to HR: {req.get('leave_type_name')} {req.get('start_date')}",
                              "Your leave request was escalated",
                              "Your manager did not respond within the allowed time, so HR Admin will decide.",
                              extra_rows=[("Status", "Pending HR approval (escalated)")])

    log.info("NOTIFIED level=%s request=%s approvers=%s", level, req["request_id"], notified)
    return {"notified": notified, "level": level}

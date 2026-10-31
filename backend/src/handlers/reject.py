"""Step Functions task: reject a request (by an approver, timeout, or error).

Input: {"request": {...},
        "stage": "MANAGER" | "HR" | "TIMEOUT" | "BALANCE" | "NO_APPROVER" | "SYSTEM",
        "decision": {...optional...}, "details": {...optional state...}}
The balance is never touched here, because it is only deducted on approval.
"""
import logging

from common import aws, db, leave_rules, notify
from common.dates import now_iso

log = logging.getLogger()
log.setLevel(logging.INFO)


def _reason(event, req):
    stage = event.get("stage")
    decision = event.get("decision") or {}
    if stage in ("MANAGER", "HR"):
        who = decision.get("decided_by_name") or decision.get("decided_by") or stage.title()
        role = "your manager" if stage == "MANAGER" and decision.get("level") == db.LEVEL_MANAGER else "HR Admin"
        comment = decision.get("comment")
        text = f"Rejected by {role} ({who})"
        return text + (f": {comment}" if comment else ".")
    if stage == "TIMEOUT":
        return ("No approval decision was made within the allowed time, so the request was "
                "automatically rejected. Please resubmit or contact HR.")
    if stage == "NO_APPROVER":
        return ("No approver could be found for this request (for example, no manager email on file, "
                "or no other HR Admin to approve). Please contact HR.")
    if stage == "BALANCE":
        return ("Your leave balance changed before final approval and is no longer sufficient "
                "for this request.")
    return "The request could not be processed because of a system error. Please submit it again."


def handler(event, context):
    ref = event["request"]
    req = leave_rules.get_request(ref["employee_id"], ref["request_id"])
    if not req or req.get("status") not in db.PENDING_STATUSES:
        log.info("Reject skipped, request %s not pending", ref.get("request_id"))
        return {"status": req.get("status") if req else None, "skipped": True}

    stage = event.get("stage", "SYSTEM")
    reason = _reason(event, req)
    decision = event.get("decision") or {}
    by = decision.get("decided_by") or "system"
    now = now_iso()
    try:
        db.requests_table().update_item(
            Key={"employee_id": req["employee_id"], "request_id": req["request_id"]},
            UpdateExpression=("SET #s = :r, status_created = :sc, rejection_reason = :reason, "
                              "rejection_stage = :stage, rejected_by = :by, rejected_at = :now, "
                              "updated_at = :now, history = list_append(history, :h) "
                              "REMOVE task_token, pending_level"),
            ConditionExpression="#s IN (:pm, :ph)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":r": db.REJECTED,
                ":sc": f"{db.REJECTED}#{req['created_at']}",
                ":reason": reason,
                ":stage": stage,
                ":by": by,
                ":now": now,
                ":h": leave_rules.history_entry(db.REJECTED, by, reason),
                ":pm": db.PENDING_MANAGER,
                ":ph": db.PENDING_HR,
            },
        )
    except aws.ClientError as err:
        if aws.error_code(err) == "ConditionalCheckFailedException":
            return {"status": "UNCHANGED", "skipped": True}
        raise

    notify.email_employee(req, f"Leave rejected: {req.get('leave_type_name')} {req.get('start_date')}",
                          "Your leave request was rejected", "Your leave balance has not been changed.",
                          extra_rows=[("Status", "Rejected"), ("Reason", reason)])
    log.info("REJECTED %s stage=%s", req["request_id"], stage)
    return {"status": db.REJECTED, "request_id": req["request_id"], "stage": stage}

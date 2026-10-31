"""Recording an approver's decision and resuming the Step Functions workflow.

Used by both the email-link endpoint and the in-app approve/reject buttons, so
the same checks apply to both.
"""
import json

from common import aws, db, leave_rules
from common.dates import now_iso

VALID_DECISIONS = ("APPROVED", "REJECTED")


class DecisionError(Exception):
    def __init__(self, code, message, status=None):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def apply_decision(employee_id, request_id, level, decision, approver_id, approver_name="",
                   comment="", via="email"):
    if decision not in VALID_DECISIONS:
        raise DecisionError("bad_decision", "Decision must be APPROVED or REJECTED.")
    comment = (comment or "").strip()[:500]

    req = leave_rules.get_request(employee_id, request_id)
    if not req:
        raise DecisionError("not_found", "This leave request no longer exists.")
    if approver_id == employee_id:
        raise DecisionError("self_approval", "You cannot approve or reject your own leave request.")
    if req.get("status") not in db.PENDING_STATUSES or req.get("pending_level") != level:
        raise DecisionError(
            "already_processed",
            f"This request is no longer waiting for this decision (current status: {req.get('status')}).",
            status=req.get("status"),
        )
    task_token = req.get("task_token")
    if not task_token:
        raise DecisionError("already_processed", "This request has already been decided.",
                            status=req.get("status"))

    # Claim the decision atomically so two clicks (or the email link and the
    # in-app button at the same time) can't both resume the workflow.
    try:
        db.requests_table().update_item(
            Key={"employee_id": employee_id, "request_id": request_id},
            UpdateExpression="SET pending_level = :claimed, last_decision = :d, updated_at = :now",
            ConditionExpression="pending_level = :lvl AND task_token = :tt",
            ExpressionAttributeValues={
                ":claimed": f"DECIDED_{level}",
                ":lvl": level,
                ":tt": task_token,
                ":now": now_iso(),
                ":d": {"decision": decision, "level": level, "by": approver_id,
                       "by_name": approver_name, "comment": comment, "via": via, "at": now_iso()},
            },
        )
    except aws.ClientError as err:
        if aws.error_code(err) == "ConditionalCheckFailedException":
            raise DecisionError("already_processed", "This request has already been decided.") from err
        raise

    output = {
        "decision": decision,
        "level": level,
        "decided_by": approver_id,
        "decided_by_name": approver_name,
        "comment": comment,
        "via": via,
    }
    try:
        aws.sfn().send_task_success(taskToken=task_token, output=json.dumps(output))
    except aws.ClientError as err:
        if aws.error_code(err) in ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken"):
            raise DecisionError(
                "expired",
                "The approval window for this step has closed, so this decision could not be applied.",
            ) from err
        raise
    return output

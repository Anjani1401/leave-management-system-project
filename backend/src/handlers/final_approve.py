"""Step Functions task: final approval.

This is the ONLY place a leave balance is decremented. The request status
change and the balance deduction happen in one DynamoDB transaction:
  * the request must still be pending   (no double approval)
  * available balance must be >= days    (balance can never go negative)
If either condition fails, nothing is written.
"""
import logging

from common import db, leave_rules, notify
from common.dates import now_iso

log = logging.getLogger()
log.setLevel(logging.INFO)


class BalanceChangedError(Exception):
    """Balance is no longer sufficient -> the workflow rejects the request."""


class RequestNotPendingError(Exception):
    """Request was already finalised -> the workflow ends without changes."""


def handler(event, context):
    ref = event["request"]
    req = leave_rules.get_request(ref["employee_id"], ref["request_id"])
    if not req or req.get("status") not in db.PENDING_STATUSES:
        raise RequestNotPendingError(f"Request {ref['request_id']} is not pending")

    cfg = leave_rules.get_leave_type(req["leave_type"])
    requires_balance = cfg["requires_balance"] if cfg else True
    days = db.num(req["days"])
    year = db.num(req["year"])
    if cfg:
        leave_rules.ensure_balance(req["employee_id"], cfg, year)

    decision = req.get("last_decision") or {}
    approved_by = decision.get("by", "")
    now = now_iso()

    request_update = {
        "table_env": "REQUESTS_TABLE",
        "Key": {"employee_id": req["employee_id"], "request_id": req["request_id"]},
        "UpdateExpression": ("SET #s = :approved, status_created = :sc, approved_at = :now, "
                             "approved_by = :by, updated_at = :now, history = list_append(history, :h) "
                             "REMOVE task_token, pending_level"),
        "ConditionExpression": "#s IN (:pm, :ph)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":approved": db.APPROVED,
            ":sc": f"{db.APPROVED}#{req['created_at']}",
            ":now": now,
            ":by": approved_by,
            ":h": leave_rules.history_entry(db.APPROVED, approved_by or "system", "Final approval"),
            ":pm": db.PENDING_MANAGER,
            ":ph": db.PENDING_HR,
        },
    }
    if requires_balance:
        balance_update = {
            "table_env": "BALANCES_TABLE",
            "Key": db.balance_key(req["employee_id"], req["leave_type"], year),
            "UpdateExpression": "SET available = available - :d, used = used + :d, version = version + :one, updated_at = :now",
            "ConditionExpression": "attribute_exists(employee_id) AND available >= :d",
            "ExpressionAttributeValues": {":d": days, ":one": 1, ":now": now},
        }
    else:  # e.g. unpaid leave: track usage, never touch "available"
        balance_update = {
            "table_env": "BALANCES_TABLE",
            "Key": db.balance_key(req["employee_id"], req["leave_type"], year),
            "UpdateExpression": "SET used = if_not_exists(used, :zero) + :d, version = if_not_exists(version, :zero) + :one, updated_at = :now",
            "ExpressionAttributeValues": {":d": days, ":zero": 0, ":one": 1, ":now": now},
        }

    try:
        db.transact_update([request_update, balance_update])
    except db.TransactionCancelled as exc:
        reasons = exc.reasons + ["None", "None"]
        if reasons[0] == "ConditionalCheckFailed":
            raise RequestNotPendingError(f"Request {req['request_id']} was already finalised") from exc
        if reasons[1] == "ConditionalCheckFailed":
            raise BalanceChangedError(
                f"Insufficient balance at final approval for {req['request_id']}"
            ) from exc
        raise

    balance = leave_rules.get_balance(req["employee_id"], req["leave_type"], year) or {}
    extra = [("Status", "Approved")]
    if approved_by:
        extra.append(("Approved by", decision.get("by_name") or approved_by))
    if requires_balance:
        extra.append(("Remaining balance", f"{db.num(balance.get('available'))} day(s)"))
    notify.email_employee(req, f"Leave approved: {req.get('leave_type_name')} {req.get('start_date')}",
                          "Your leave has been approved", "Enjoy your time off.", extra_rows=extra)
    log.info("APPROVED %s days=%s", req["request_id"], days)
    return {"status": db.APPROVED, "request_id": req["request_id"], "days": days}

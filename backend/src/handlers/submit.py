"""POST /leaves - an employee applies for leave.

1. Validate the input (dates, leave type).
2. Apply the business rules: notice period, max consecutive days,
   overlapping dates, and the balance (quota) check.
   A rule failure saves the request as AUTO_REJECTED and emails the reason.
3. Otherwise save it as PENDING_MANAGER, start the approval workflow
   (Step Functions) and email "submitted".
"""
import json
import logging
import os
import uuid

from common import aws, db, leave_rules, notify
from common.dates import now_iso, parse_date, today_local, working_days
from common.http import caller_from_event, error, json_body, respond

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_REASON = 500


class RuleViolation(Exception):
    """A business rule failed -> the request is auto-rejected with this reason."""


def handler(event, context):
    caller = caller_from_event(event)
    if not caller:
        return error(403, "Your account is not linked to an employee record.")
    try:
        body = json_body(event)
    except (ValueError, json.JSONDecodeError):
        return error(400, "Request body must be valid JSON.")

    employee = leave_rules.get_employee(caller.employee_id)
    if not employee:
        return error(403, f"No employee record found for {caller.employee_id}.")
    if not employee.get("manager_id"):
        return error(400, "You have no manager assigned, so the request can't be routed. Contact HR.")

    # ---- input validation (bad input is rejected without saving anything)
    leave_type = str(body.get("leave_type", "")).strip().upper()
    cfg = leave_rules.get_leave_type(leave_type)
    if not cfg:
        return error(400, f"Unknown leave type '{leave_type}'.")
    try:
        start = parse_date(body.get("start_date"), "start_date")
        end = parse_date(body.get("end_date"), "end_date")
    except ValueError as exc:
        return error(400, str(exc))
    if end < start:
        return error(400, "end_date must be on or after start_date.")
    if start.year != end.year:
        return error(400, "A leave request can't cross into a new year. Please submit one request per year.")
    reason = str(body.get("reason", "")).strip()
    if len(reason) > MAX_REASON:
        return error(400, f"Reason must be at most {MAX_REASON} characters.")

    holidays = leave_rules.get_holidays(start.year)
    days = len(working_days(start, end, holidays))
    if days == 0:
        return error(400, "The selected dates contain no working days (only weekends or holidays).")

    settings = leave_rules.get_settings()
    now = now_iso()
    request_id = f"LR-{start.isoformat()}-{uuid.uuid4().hex[:8]}"
    item = {
        "employee_id": caller.employee_id,
        "request_id": request_id,
        "employee_name": employee.get("name", caller.name),
        "employee_email": employee.get("email", caller.email),
        "manager_id": employee["manager_id"],
        "department": employee.get("department", ""),
        "leave_type": leave_type,
        "leave_type_name": cfg["name"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "year": start.year,
        "days": days,
        "reason": reason,
        "requires_hr": days > settings["hr_approval_threshold_days"],
        "created_at": now,
        "updated_at": now,
    }

    # ---- business rules (failures are saved as AUTO_REJECTED + emailed)
    try:
        check_rules(caller.employee_id, cfg, start, end, days)
    except RuleViolation as violation:
        reason_text = str(violation)
        item.update(
            status=db.AUTO_REJECTED,
            status_created=f"{db.AUTO_REJECTED}#{now}",
            rejection_reason=reason_text,
            history=leave_rules.history_entry(db.AUTO_REJECTED, "system", reason_text),
        )
        db.requests_table().put_item(Item=item)
        notify.email_employee(
            item,
            f"Leave request auto-rejected: {item['leave_type_name']} {item['start_date']}",
            "Your leave request was automatically rejected",
            "Your request did not pass the leave rules, so it was not sent for approval.",
            extra_rows=[("Reason", reason_text)],
        )
        log.info("AUTO_REJECTED %s %s: %s", caller.employee_id, request_id, reason_text)
        return respond(422, {"status": db.AUTO_REJECTED, "request_id": request_id,
                             "reason": reason_text, "request": leave_rules.public_request(item)})

    # ---- save as pending and start the approval workflow
    item.update(
        status=db.PENDING_MANAGER,
        status_created=f"{db.PENDING_MANAGER}#{now}",
        pending_level=db.LEVEL_MANAGER,
        history=leave_rules.history_entry(db.PENDING_MANAGER, caller.employee_id, "Submitted"),
    )
    db.requests_table().put_item(Item=item, ConditionExpression="attribute_not_exists(request_id)")

    workflow_input = {
        "request": {
            "employee_id": caller.employee_id,
            "request_id": request_id,
            "leave_type": leave_type,
            "days": days,
            "requires_hr": item["requires_hr"],
        },
        "config": {"approval_timeout_seconds": int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "172800"))},
    }
    try:
        execution = aws.sfn().start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=request_id,
            input=json.dumps(workflow_input),
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not start approval workflow for %s", request_id)
        failure = "System error: the approval workflow could not be started. Please submit again."
        db.requests_table().update_item(
            Key={"employee_id": caller.employee_id, "request_id": request_id},
            UpdateExpression=("SET #s = :s, status_created = :sc, rejection_reason = :r, "
                              "history = list_append(history, :h) REMOVE pending_level"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": db.AUTO_REJECTED,
                ":sc": f"{db.AUTO_REJECTED}#{now}",
                ":r": failure,
                ":h": leave_rules.history_entry(db.AUTO_REJECTED, "system", failure),
            },
        )
        return error(500, failure, request_id=request_id)

    db.requests_table().update_item(
        Key={"employee_id": caller.employee_id, "request_id": request_id},
        UpdateExpression="SET execution_arn = :a",
        ExpressionAttributeValues={":a": execution["executionArn"]},
    )

    next_step = "your manager" + (" and then HR (more than "
                                  f"{settings['hr_approval_threshold_days']} days)" if item["requires_hr"] else "")
    notify.email_employee(
        item,
        f"Leave request submitted: {item['leave_type_name']} {item['start_date']}",
        "Your leave request was submitted",
        f"Your request is waiting for approval by {next_step}.",
        extra_rows=[("Status", "Pending manager approval")],
    )
    log.info("SUBMITTED %s %s days=%s", caller.employee_id, request_id, days)
    return respond(201, {"status": db.PENDING_MANAGER, "request_id": request_id,
                         "request": leave_rules.public_request(item)})


def check_rules(employee_id, cfg, start, end, days):
    today = today_local()
    if start < today:
        if (today - start).days > cfg["max_backdate_days"]:
            if cfg["max_backdate_days"] == 0:
                raise RuleViolation(f"{cfg['name']} can't be applied for past dates.")
            raise RuleViolation(
                f"{cfg['name']} can be applied at most {cfg['max_backdate_days']} day(s) after the start date."
            )
    elif (start - today).days < cfg["min_notice_days"]:
        raise RuleViolation(
            f"{cfg['name']} needs at least {cfg['min_notice_days']} day(s) notice before the start date."
        )

    if days > cfg["max_consecutive_days"]:
        raise RuleViolation(
            f"{cfg['name']} can be taken for at most {cfg['max_consecutive_days']} working day(s) at a time; "
            f"you requested {days}."
        )

    existing = leave_rules.list_employee_requests(employee_id)
    clash = leave_rules.find_overlap(existing, start, end)
    if clash:
        raise RuleViolation(
            f"These dates overlap your {clash['status'].replace('_', ' ').lower()} request "
            f"{clash['request_id']} ({clash['start_date']} to {clash['end_date']})."
        )

    if cfg["requires_balance"]:
        balance = leave_rules.ensure_balance(employee_id, cfg, start.year)
        available = db.num(balance.get("available"))
        pending = leave_rules.pending_days(existing, cfg["leave_type"], start.year)
        usable = available - pending
        if days > usable:
            detail = f" ({pending} day(s) are already pending approval)" if pending else ""
            raise RuleViolation(
                f"Insufficient {cfg['name']} balance: you requested {days} working day(s) but only "
                f"{max(usable, 0)} day(s) are available{detail}."
            )

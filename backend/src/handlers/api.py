"""Read endpoints and in-app approvals for the web app (all need a Cognito login).

GET  /me                                     profile + roles
GET  /config/leave-types                     leave types from leave_config
GET  /me/balances                            balance per leave type (current year)
GET  /me/leaves                              my leave history
GET  /approvals/pending                      requests waiting for me (Manager / HRAdmin)
POST /approvals/{employee_id}/{request_id}   approve or reject in the app
GET  /calendar?from=YYYY-MM-DD&to=YYYY-MM-DD approved leave per day (team / everyone)
GET  /reports/leaves?from=...&to=...         CSV report -> presigned S3 download link
"""
import csv
import io
import logging
import os
from datetime import timedelta
from urllib.parse import unquote

from common import aws, db, leave_rules
from common.dates import now_iso, parse_date, today_local
from common.decisions import DecisionError, apply_decision
from common.http import caller_from_event, error, json_body, respond

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_RANGE_DAYS = 370


def handler(event, context):
    caller = caller_from_event(event)
    if not caller:
        return error(403, "Your account is not linked to an employee record.")
    route = (event.get("httpMethod", "GET").upper(), event.get("resource", ""))
    try:
        if route == ("GET", "/me"):
            return get_me(caller)
        if route == ("GET", "/config/leave-types"):
            return respond(200, {"leave_types": leave_rules.load_leave_types()})
        if route == ("GET", "/me/balances"):
            return get_balances(caller)
        if route == ("GET", "/me/leaves"):
            return get_my_leaves(caller)
        if route == ("GET", "/approvals/pending"):
            return get_pending(caller)
        if route == ("POST", "/approvals/{employee_id}/{request_id}"):
            return post_decision(caller, event)
        if route == ("GET", "/calendar"):
            return get_calendar(caller, event)
        if route == ("GET", "/reports/leaves"):
            return get_report(caller, event)
    except ValueError as exc:
        return error(400, str(exc))
    return error(404, f"No route for {route[0]} {route[1]}")


# --------------------------------------------------------------- employee

def get_me(caller):
    emp = leave_rules.get_employee(caller.employee_id) or {}
    return respond(200, {
        "employee_id": caller.employee_id,
        "name": emp.get("name", caller.name),
        "email": emp.get("email", caller.email),
        "department": emp.get("department", ""),
        "manager_id": emp.get("manager_id"),
        "groups": sorted(caller.groups),
        "is_manager": caller.is_manager,
        "is_hr": caller.is_hr,
    })


def get_balances(caller):
    year = today_local().year
    requests = leave_rules.list_employee_requests(caller.employee_id)
    result = []
    for cfg in leave_rules.load_leave_types():
        bal = leave_rules.ensure_balance(caller.employee_id, cfg, year)
        pending = leave_rules.pending_days(requests, cfg["leave_type"], year)
        available = db.num(bal.get("available"))
        result.append({
            "leave_type": cfg["leave_type"],
            "name": cfg["name"],
            "requires_balance": cfg["requires_balance"],
            "allocated": db.num(bal.get("allocated")),
            "carried_forward": db.num(bal.get("carried_forward")),
            "used": db.num(bal.get("used")),
            "available": available,
            "pending": pending,
            "usable": max(available - pending, 0) if cfg["requires_balance"] else None,
        })
    return respond(200, {"year": year, "balances": result})


def get_my_leaves(caller):
    items = leave_rules.list_employee_requests(caller.employee_id)
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return respond(200, {"requests": [leave_rules.public_request(r) for r in items]})


# --------------------------------------------------------------- approvers

def get_pending(caller):
    if not (caller.is_manager or caller.is_hr):
        return error(403, "Only managers and HR Admins can view pending approvals.")
    results = []
    # Requests where I am the direct manager
    for req in db.query_all(
        db.requests_table(),
        IndexName="manager-status-index",
        KeyConditionExpression="manager_id = :m AND begins_with(status_created, :p)",
        ExpressionAttributeValues={":m": caller.employee_id, ":p": f"{db.PENDING_MANAGER}#"},
    ):
        if req.get("pending_level") == db.LEVEL_MANAGER and req["employee_id"] != caller.employee_id:
            results.append({**leave_rules.public_request(req), "my_level": db.LEVEL_MANAGER})
    # HR stage (long leave or escalation)
    if caller.is_hr:
        for req in db.query_all(
            db.requests_table(),
            IndexName="status-start-index",
            KeyConditionExpression="#s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": db.PENDING_HR},
        ):
            if req.get("pending_level") in (db.LEVEL_HR, db.LEVEL_ESCALATION) \
                    and req["employee_id"] != caller.employee_id:
                results.append({**leave_rules.public_request(req), "my_level": req["pending_level"]})
    results.sort(key=lambda r: r.get("start_date", ""))
    return respond(200, {"pending": results})


def post_decision(caller, event):
    params = event.get("pathParameters") or {}
    employee_id = unquote(params.get("employee_id", ""))
    request_id = unquote(params.get("request_id", ""))
    body = json_body(event)
    decision = str(body.get("decision", "")).upper()

    req = leave_rules.get_request(employee_id, request_id)
    if not req:
        return error(404, "Leave request not found.")
    level = req.get("pending_level")
    allowed = (
        (level == db.LEVEL_MANAGER and req.get("manager_id") == caller.employee_id)
        or (level in (db.LEVEL_HR, db.LEVEL_ESCALATION) and caller.is_hr)
    )
    if not allowed:
        return error(403, "You are not the approver for this step of the request.")
    try:
        result = apply_decision(employee_id, request_id, level, decision, approver_id=caller.employee_id,
                                approver_name=caller.name, comment=body.get("comment", ""), via="app")
    except DecisionError as exc:
        status = {"already_processed": 409, "expired": 409, "self_approval": 403, "not_found": 404}.get(exc.code, 400)
        return error(status, exc.message, code=exc.code)
    return respond(200, {"ok": True, **result})


def _date_range(event, default_days):
    params = event.get("queryStringParameters") or {}
    today = today_local()
    start = parse_date(params["from"], "from") if params.get("from") else today.replace(day=1)
    end = parse_date(params["to"], "to") if params.get("to") else start + timedelta(days=default_days)
    if end < start:
        raise ValueError("'to' must be on or after 'from'.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ValueError(f"Date range can be at most {MAX_RANGE_DAYS} days.")
    return start, end


def _requests_in_range(status, start, end):
    """Requests with this status that overlap [start, end] (status-start-index)."""
    return db.query_all(
        db.requests_table(),
        IndexName="status-start-index",
        KeyConditionExpression="#s = :s AND start_date <= :to",
        FilterExpression="end_date >= :from",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":to": end.isoformat(), ":from": start.isoformat()},
    )


def _visible(caller, req):
    return caller.is_hr or req.get("manager_id") == caller.employee_id or req["employee_id"] == caller.employee_id


def get_calendar(caller, event):
    if not (caller.is_manager or caller.is_hr):
        return error(403, "Only managers and HR Admins can view the team calendar.")
    start, end = _date_range(event, 41)
    holidays = set(leave_rules.get_holidays(start.year)) | set(leave_rules.get_holidays(end.year))
    entries = []
    for status in (db.APPROVED, db.PENDING_MANAGER, db.PENDING_HR):
        for req in _requests_in_range(status, start, end):
            if _visible(caller, req):
                entries.append({
                    "employee_id": req["employee_id"],
                    "employee_name": req.get("employee_name"),
                    "department": req.get("department", ""),
                    "leave_type": req.get("leave_type"),
                    "leave_type_name": req.get("leave_type_name"),
                    "start_date": req["start_date"],
                    "end_date": req["end_date"],
                    "days": db.num(req.get("days")),
                    "status": req["status"],
                    "request_id": req["request_id"],
                })
    entries.sort(key=lambda e: (e["start_date"], e["employee_name"] or ""))
    return respond(200, {"from": start.isoformat(), "to": end.isoformat(),
                         "holidays": sorted(holidays), "entries": entries})


REPORT_COLUMNS = ["request_id", "employee_id", "employee_name", "department", "manager_id", "leave_type",
                  "start_date", "end_date", "days", "status", "reason", "created_at", "approved_at",
                  "approved_by", "rejection_reason"]


def get_report(caller, event):
    if not (caller.is_manager or caller.is_hr):
        return error(403, "Only managers and HR Admins can download leave reports.")
    start, end = _date_range(event, 30)
    rows = []
    for status in db.ALL_STATUSES:
        rows.extend(r for r in _requests_in_range(status, start, end) if _visible(caller, r))
    rows.sort(key=lambda r: (r["start_date"], r.get("employee_name", "")))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: db.plain(r.get(k, "")) for k in REPORT_COLUMNS})

    filename = f"leave-report_{start.isoformat()}_to_{end.isoformat()}.csv"
    key = f"reports/{caller.employee_id}/{now_iso().replace(':', '')}_{filename}"
    bucket = os.environ["REPORTS_BUCKET"]
    aws.s3().put_object(Bucket=bucket, Key=key, Body=buf.getvalue().encode("utf-8"),
                        ContentType="text/csv")
    url = aws.s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"'},
        ExpiresIn=900,
    )
    return respond(200, {"url": url, "rows": len(rows), "filename": filename})

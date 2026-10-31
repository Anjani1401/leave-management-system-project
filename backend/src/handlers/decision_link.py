"""GET/POST /decision?t=<signed token> - the approve/reject links in approver emails.

GET  shows a confirmation page (it never changes anything, so email security
     scanners that pre-open links can't approve a request by accident).
POST (the confirm button) verifies the token again and records the decision.

This endpoint has no Cognito login: the signed, expiring token is the security.
"""
import html
import logging
from urllib.parse import parse_qs

from common import db, leave_rules, tokens
from common.decisions import DecisionError, apply_decision
from common.http import html_response, raw_body

log = logging.getLogger()
log.setLevel(logging.INFO)

LEVEL_LABEL = {
    db.LEVEL_MANAGER: "Manager approval",
    db.LEVEL_HR: "HR Admin approval",
    db.LEVEL_ESCALATION: "HR Admin approval (escalated)",
}


def _esc(value):
    return html.escape(str(value if value is not None else ""))


def page(title, message, colour="#1d4ed8", body_html=""):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f5f7;color:#1f2328;margin:0;padding:24px}}
 .card{{max-width:560px;margin:40px auto;background:#fff;border-radius:12px;padding:28px;border:1px solid #e3e5e8}}
 h1{{font-size:20px;margin:0 0 8px;color:{colour}}} p{{line-height:1.5}}
 table{{border-collapse:collapse;margin:16px 0;width:100%}} td{{padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
 td:first-child{{color:#57606a;width:40%}} textarea{{width:100%;box-sizing:border-box;min-height:70px;padding:8px;border:1px solid #ccc;border-radius:6px;font:inherit}}
 button{{margin-top:14px;padding:10px 20px;border:0;border-radius:6px;color:#fff;font-size:15px;cursor:pointer;background:{colour}}}
</style></head>
<body><div class="card"><h1>{_esc(title)}</h1><p>{_esc(message)}</p>{body_html}</div></body></html>"""


def _token(event):
    if (event.get("httpMethod") or "GET").upper() == "POST":
        form = parse_qs(raw_body(event))
        token = (form.get("t") or [""])[0]
        comment = (form.get("comment") or [""])[0]
        if token:
            return token, comment
    params = event.get("queryStringParameters") or {}
    return params.get("t", ""), ""


def handler(event, context):
    method = (event.get("httpMethod") or "GET").upper()
    token, comment = _token(event)
    try:
        claims = tokens.verify(token)
    except tokens.TokenError as exc:
        return html_response(400, page("Link not valid", str(exc), "#b42318"))

    req = leave_rules.get_request(claims["e"], claims["r"])
    if not req:
        return html_response(404, page("Not found", "This leave request no longer exists.", "#b42318"))

    action = claims["a"]
    verb = "Approve" if action == "APPROVED" else "Reject"
    colour = "#1f7a4d" if action == "APPROVED" else "#b42318"
    rows = [("Employee", req.get("employee_name")), ("Leave type", req.get("leave_type_name")),
            ("From", req.get("start_date")), ("To", req.get("end_date")),
            ("Working days", db.plain(req.get("days"))), ("Reason", req.get("reason") or "-"),
            ("Step", LEVEL_LABEL.get(claims["l"], claims["l"])), ("Current status", req.get("status"))]
    table = _table(rows)

    still_waiting = req.get("status") in db.PENDING_STATUSES and req.get("pending_level") == claims["l"]

    if method == "GET":
        if not still_waiting:
            return html_response(200, page("Already processed",
                                           f"This request is no longer waiting for this decision. Current status: {req.get('status')}.",
                                           "#57606a", table))
        form = (f"<form method='post'><input type='hidden' name='t' value='{_esc(token)}'>"
                f"<label for='comment'>Comment (optional)</label>"
                f"<textarea id='comment' name='comment' maxlength='500'></textarea>"
                f"<button type='submit'>Confirm: {verb}</button></form>")
        return html_response(200, page(f"{verb} this leave request?",
                                       "Check the details, then confirm.", colour, table + form))

    try:
        apply_decision(claims["e"], claims["r"], claims["l"], action, approver_id=claims["u"],
                       approver_name=_approver_name(claims["u"]), comment=comment, via="email")
    except DecisionError as exc:
        status = 409 if exc.code in ("already_processed", "expired") else 400
        return html_response(status, page("Could not record decision", exc.message, "#b42318", table))

    log.info("DECISION %s %s level=%s by=%s", action, claims["r"], claims["l"], claims["u"])
    done = "approved" if action == "APPROVED" else "rejected"
    return html_response(200, page(f"Request {done}",
                                   "Thanks - your decision was recorded and the employee will be notified by email.",
                                   colour, _table(rows[:-1])))


def _table(rows):
    return "<table>" + "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in rows) + "</table>"


def _approver_name(employee_id):
    emp = leave_rules.get_employee(employee_id)
    return (emp or {}).get("name", employee_id)

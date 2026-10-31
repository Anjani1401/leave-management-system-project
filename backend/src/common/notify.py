"""Email (SES) and SNS notifications."""
import html
import logging
import os

from common import aws

log = logging.getLogger()


def _esc(value):
    return html.escape(str(value if value is not None else ""))


def send_email(to, subject, heading, rows, intro="", buttons=None, footer=""):
    """Send a simple HTML + text email through SES.

    rows: list of (label, value) shown as a small table.
    buttons: list of (label, url, colour).
    Returns True on success. Failures are logged, never raised, so an email
    problem (e.g. an unverified address in the SES sandbox) can't break the
    leave workflow itself.
    """
    if not to:
        return False
    buttons = buttons or []
    table_rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{_esc(k)}</td>"
        f"<td style='padding:4px 0'><b>{_esc(v)}</b></td></tr>"
        for k, v in rows
    )
    button_html = " ".join(
        f"<a href='{_esc(url)}' style='display:inline-block;padding:10px 18px;margin:6px 8px 6px 0;"
        f"background:{colour};color:#fff;text-decoration:none;border-radius:6px'>{_esc(label)}</a>"
        for label, url, colour in buttons
    )
    body_html = (
        "<div style='font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:560px'>"
        f"<h2 style='font-size:18px'>{_esc(heading)}</h2>"
        f"<p>{_esc(intro)}</p>"
        f"<table style='border-collapse:collapse'>{table_rows}</table>"
        f"<p>{button_html}</p>"
        f"<p style='color:#777;font-size:12px'>{_esc(footer)}</p>"
        "</div>"
    )
    text_lines = [heading, "", intro, ""]
    text_lines += [f"{k}: {v}" for k, v in rows]
    text_lines += [""] + [f"{label}: {url}" for label, url, _ in buttons]
    if footer:
        text_lines += ["", footer]
    try:
        aws.ses().send_email(
            Source=os.environ["SENDER_EMAIL"],
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": body_html, "Charset": "UTF-8"},
                    "Text": {"Data": "\n".join(text_lines), "Charset": "UTF-8"},
                },
            },
        )
        return True
    except Exception:  # noqa: BLE001 - log every email failure, keep the workflow going
        log.exception("SES send_email failed to=%s subject=%s", to, subject)
        return False


def publish_to_approver(recipient_email, subject, message):
    """Publish an approval notification to the SNS topic.

    Approvers are subscribed to the topic with a filter policy on
    `recipient_email`, so each approver only receives their own messages.
    """
    try:
        aws.sns().publish(
            TopicArn=os.environ["APPROVAL_TOPIC_ARN"],
            Subject=subject[:99],
            Message=message,
            MessageAttributes={
                "recipient_email": {"DataType": "String", "StringValue": recipient_email}
            },
        )
        return True
    except Exception:  # noqa: BLE001
        log.exception("SNS publish failed recipient=%s", recipient_email)
        return False


def request_rows(req):
    rows = [
        ("Request ID", req.get("request_id")),
        ("Employee", req.get("employee_name") or req.get("employee_id")),
        ("Leave type", req.get("leave_type_name") or req.get("leave_type")),
        ("From", req.get("start_date")),
        ("To", req.get("end_date")),
        ("Working days", req.get("days")),
    ]
    if req.get("reason"):
        rows.append(("Reason", req.get("reason")))
    return rows


def email_employee(req, subject, heading, intro, extra_rows=None):
    rows = request_rows(req) + list(extra_rows or [])
    return send_email(req.get("employee_email"), subject, heading, rows, intro=intro,
                      footer="This is an automated message from the Leave Management System.")

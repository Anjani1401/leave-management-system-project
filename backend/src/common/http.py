"""API Gateway (REST API, Lambda proxy integration) helpers."""
import base64
import json
import os

from common.db import plain


def cors_headers():
    return {
        "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    }


def respond(status, body):
    headers = {"Content-Type": "application/json", **cors_headers()}
    return {"statusCode": status, "headers": headers, "body": json.dumps(plain(body))}


def error(status, message, **extra):
    return respond(status, {"error": message, **extra})


def html_response(status, page):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        "body": page,
    }


def raw_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def json_body(event):
    text = raw_body(event)
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


class Caller:
    """The signed-in user, taken from the Cognito claims that API Gateway verified."""

    def __init__(self, employee_id, email, name, groups):
        self.employee_id = employee_id
        self.email = email
        self.name = name
        self.groups = groups

    @property
    def is_manager(self):
        return "Manager" in self.groups

    @property
    def is_hr(self):
        return "HRAdmin" in self.groups


def parse_groups(value):
    """cognito:groups arrives as a string such as "Manager,HRAdmin" or "[Manager HRAdmin]"."""
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value}
    text = str(value).strip().strip("[]")
    return {part for part in text.replace(",", " ").split() if part}


def caller_from_event(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    employee_id = claims.get("custom:employee_id")
    if not employee_id:
        return None
    return Caller(
        employee_id=employee_id,
        email=claims.get("email", ""),
        name=claims.get("name") or claims.get("email", ""),
        groups=parse_groups(claims.get("cognito:groups")),
    )

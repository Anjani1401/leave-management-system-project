import json
import os
import sys
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

os.environ.update({
    "CONFIG_TABLE": "t-config",
    "BALANCES_TABLE": "t-balances",
    "REQUESTS_TABLE": "t-requests",
    "EMPLOYEES_TABLE": "t-employees",
    "SENDER_EMAIL": "noreply@example.com",
    "APPROVAL_TIMEOUT_SECONDS": "172800",
    "STATE_MACHINE_ARN": "arn:aws:states:ap-south-1:123:stateMachine:leave-approval",
    "SIGNING_SECRET": "test-signing-secret",
    "APPROVAL_TOPIC_ARN": "arn:aws:sns:ap-south-1:123:approvals",
    "DECISION_BASE_URL": "https://decisions.example.com/prod",
    "REPORTS_BUCKET": "reports-bucket",
    "TZ_OFFSET_MINUTES": "330",
    "APPROVER_EMAIL_VIA_SES": "true",
})

from common import aws  # noqa: E402
from fakes import FakeDynamo, FakeS3, FakeSES, FakeSFN, FakeSNS  # noqa: E402

EMP, MGR, HR = "emp-001", "mgr-001", "hr-001"
FAKE_TODAY = date(2026, 10, 28)  # a Wednesday; tests are deterministic whatever day they run
YEAR = FAKE_TODAY.year
EMAILS = {EMP: "emp@example.com", MGR: "mgr@example.com", HR: "hr@example.com"}


class World:
    """Fake AWS plus helpers to call the Lambdas like API Gateway would."""

    def __init__(self):
        aws.reset()
        self.db = FakeDynamo()
        self.ses = FakeSES()
        self.sns = FakeSNS()
        self.sfn = FakeSFN()
        self.s3 = FakeS3()
        aws.set_override("dynamodb", self.db)
        aws.set_override("transact", self.db.transact)
        aws.set_override("ses", self.ses)
        aws.set_override("sns", self.sns)
        aws.set_override("sfn", self.sfn)
        aws.set_override("s3", self.s3)
        self.seed()

    # ---------------------------------------------------------- data
    def seed(self, year=None):
        year = year or YEAR
        cfg = self.db.Table("t-config")
        for item in [
            {"leave_type": "SICK", "name": "Sick leave", "annual_quota": 12, "requires_balance": True,
             "carry_forward": False, "max_carry_forward": 0, "min_notice_days": 0, "max_backdate_days": 7,
             "max_consecutive_days": 15, "active": True},
            {"leave_type": "CASUAL", "name": "Casual leave", "annual_quota": 10, "requires_balance": True,
             "carry_forward": False, "max_carry_forward": 0, "min_notice_days": 1, "max_backdate_days": 0,
             "max_consecutive_days": 5, "active": True},
            {"leave_type": "EARNED", "name": "Earned leave", "annual_quota": 18, "requires_balance": True,
             "carry_forward": True, "max_carry_forward": 10, "min_notice_days": 0, "max_backdate_days": 0,
             "max_consecutive_days": 30, "active": True},
            {"leave_type": "UNPAID", "name": "Unpaid leave", "annual_quota": 0, "requires_balance": False,
             "carry_forward": False, "max_carry_forward": 0, "min_notice_days": 0, "max_backdate_days": 0,
             "max_consecutive_days": 60, "active": True},
            {"leave_type": "SYSTEM#SETTINGS", "hr_approval_threshold_days": 5},
        ]:
            cfg.put_item(Item=item)
        emp = self.db.Table("t-employees")
        emp.put_item(Item={"employee_id": EMP, "name": "Ravi Kumar", "email": EMAILS[EMP],
                           "manager_id": MGR, "role": "EMPLOYEE", "department": "Engineering"})
        emp.put_item(Item={"employee_id": MGR, "name": "Meena Rao", "email": EMAILS[MGR],
                           "manager_id": HR, "role": "MANAGER", "department": "Engineering"})
        emp.put_item(Item={"employee_id": HR, "name": "Harini S", "email": EMAILS[HR],
                           "manager_id": MGR, "role": "HR_ADMIN", "department": "HR"})

    def set_balance(self, employee_id, leave_type, available, year=None, used=0, allocated=None):
        year = year or YEAR
        self.db.Table("t-balances").put_item(Item={
            "employee_id": employee_id, "leave_type_year": f"{leave_type}#{year}",
            "leave_type": leave_type, "year": year,
            "allocated": allocated if allocated is not None else available + used,
            "carried_forward": 0, "used": used, "available": available,
            "requires_balance": True, "version": 1})

    def balance(self, employee_id, leave_type, year=None):
        year = year or YEAR
        return self.db.Table("t-balances").get_item(
            Key={"employee_id": employee_id, "leave_type_year": f"{leave_type}#{year}"}).get("Item")

    def request(self, employee_id, request_id):
        return self.db.Table("t-requests").get_item(
            Key={"employee_id": employee_id, "request_id": request_id}).get("Item")

    # ---------------------------------------------------------- API calls
    @staticmethod
    def claims(employee_id, groups):
        return {"custom:employee_id": employee_id, "email": EMAILS.get(employee_id, "x@example.com"),
                "name": employee_id, "cognito:groups": ",".join(groups)}

    def api_event(self, method, resource, employee_id=EMP, groups=("Employee",), body=None,
                  query=None, path_params=None):
        return {
            "httpMethod": method, "resource": resource,
            "requestContext": {"authorizer": {"claims": self.claims(employee_id, groups)}},
            "body": json.dumps(body) if body is not None else None,
            "queryStringParameters": query, "pathParameters": path_params,
            "isBase64Encoded": False,
        }

    def submit(self, leave_type, start, end, reason="Personal work", employee_id=EMP):
        from handlers import submit
        event = self.api_event("POST", "/leaves", employee_id, body={
            "leave_type": leave_type, "start_date": str(start), "end_date": str(end), "reason": reason})
        resp = submit.handler(event, None)
        return resp["statusCode"], json.loads(resp["body"])

    def call_api(self, method, resource, employee_id=EMP, groups=("Employee",), **kw):
        from handlers import api
        resp = api.handler(self.api_event(method, resource, employee_id, groups, **kw), None)
        return resp["statusCode"], json.loads(resp["body"])

    # ---------------------------------------------------------- workflow
    def start_workflow(self):
        from workflow_sim import Execution
        started = self.sfn.started[-1]
        return Execution(started["input"], self.sfn).run()

    def links_for(self, email):
        """Approve/reject links from the latest SNS message for this approver."""
        msgs = [m for m in self.sns.published if m["recipient"] == email]
        assert msgs, f"no SNS message for {email}"
        text = msgs[-1]["message"]
        approve = text.split("APPROVE: ")[1].split()[0]
        reject = text.split("REJECT: ")[1].split()[0]
        return approve, reject

    def click_link(self, url, confirm=True, comment=""):
        """GET the link (confirmation page), then POST the confirm form like a browser."""
        from handlers import decision_link
        token = parse_qs(urlparse(url).query)["t"][0]
        get = decision_link.handler({"httpMethod": "GET", "queryStringParameters": {"t": token}}, None)
        if not confirm:
            return get, None
        post = decision_link.handler({"httpMethod": "POST", "queryStringParameters": {"t": token},
                                      "body": urlencode({"t": token, "comment": comment}),
                                      "isBase64Encoded": False}, None)
        return get, post


def next_weekday(d, weekday):
    """Next date (strictly after d) falling on weekday (0=Mon)."""
    days = (weekday - d.weekday()) % 7 or 7
    return d + timedelta(days=days)


@pytest.fixture
def world(monkeypatch):
    import common.dates
    import handlers.api
    import handlers.submit
    import handlers.weekly_balance
    for module in (common.dates, handlers.api, handlers.submit, handlers.weekly_balance):
        monkeypatch.setattr(module, "today_local", lambda: FAKE_TODAY)
    return World()


@pytest.fixture
def monday():
    """Monday 2026-11-09, comfortably after FAKE_TODAY."""
    return date(2026, 11, 9)

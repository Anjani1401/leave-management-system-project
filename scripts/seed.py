"""Load starter data after `sam deploy`.

Creates:
  * leave types + quotas, the HR threshold setting and holidays (leave_config)
  * three employees (employee -> manager -> HR) in the employees table
  * matching Cognito users, groups and a permanent password
  * SNS email subscriptions for the approvers (each gets only their own messages)
  * optionally SES verification emails for the three addresses

Usage (from the repo root):
  python scripts/seed.py --stack leave-mgmt --region ap-south-1 \
      --employee-email you+emp@gmail.com --manager-email you+mgr@gmail.com \
      --hr-email you+hr@gmail.com --password 'Leave@2026' --verify-ses

  python scripts/seed.py ... --reset     # wipe all requests and balances (fresh demo)
"""
import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

LEAVE_TYPES = [
    {"leave_type": "SICK", "name": "Sick leave", "annual_quota": 12, "requires_balance": True,
     "carry_forward": False, "max_carry_forward": 0, "min_notice_days": 0, "max_backdate_days": 7,
     "max_consecutive_days": 15, "active": True},
    {"leave_type": "CASUAL", "name": "Casual leave", "annual_quota": 10, "requires_balance": True,
     "carry_forward": False, "max_carry_forward": 0, "min_notice_days": 1, "max_backdate_days": 0,
     "max_consecutive_days": 5, "active": True},
    {"leave_type": "EARNED", "name": "Earned leave", "annual_quota": 18, "requires_balance": True,
     "carry_forward": True, "max_carry_forward": 10, "min_notice_days": 3, "max_backdate_days": 0,
     "max_consecutive_days": 30, "active": True},
    {"leave_type": "UNPAID", "name": "Unpaid leave", "annual_quota": 0, "requires_balance": False,
     "carry_forward": False, "max_carry_forward": 0, "min_notice_days": 3, "max_backdate_days": 0,
     "max_consecutive_days": 60, "active": True},
]
SETTINGS = {"leave_type": "SYSTEM#SETTINGS", "hr_approval_threshold_days": 5}
# National holidays only - HR should replace these with the company's list.
HOLIDAYS = {
    2026: ["2026-01-26", "2026-08-15", "2026-10-02", "2026-12-25"],
    2027: ["2027-01-26", "2027-08-15", "2027-10-02", "2027-12-25"],
}


def stack_outputs(cfn, stack):
    try:
        out = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    except ClientError as err:
        sys.exit(f"Could not read stack '{stack}': {err}. Did `sam deploy` finish?")
    return {o["OutputKey"]: o["OutputValue"] for o in out}


def wipe(table, keys):
    count = 0
    scan = {"ProjectionExpression": ", ".join(f"#k{i}" for i in range(len(keys))),
            "ExpressionAttributeNames": {f"#k{i}": k for i, k in enumerate(keys)}}
    with table.batch_writer() as batch:
        while True:
            resp = table.scan(**scan)
            for item in resp["Items"]:
                batch.delete_item(Key={k: item[k] for k in keys})
                count += 1
            if "LastEvaluatedKey" not in resp:
                break
            scan["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return count


def upsert_user(cognito, pool_id, email, name, employee_id, groups, password):
    attrs = [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
             {"Name": "name", "Value": name}, {"Name": "custom:employee_id", "Value": employee_id}]
    try:
        cognito.admin_create_user(UserPoolId=pool_id, Username=email, UserAttributes=attrs,
                                  MessageAction="SUPPRESS")
        print(f"  created Cognito user {email}")
    except ClientError as err:
        if err.response["Error"]["Code"] != "UsernameExistsException":
            raise
        cognito.admin_update_user_attributes(UserPoolId=pool_id, Username=email, UserAttributes=attrs)
        print(f"  updated Cognito user {email}")
    cognito.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)
    for group in groups:
        cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)


def subscribe(sns, topic_arn, email):
    existing = []
    for page in sns.get_paginator("list_subscriptions_by_topic").paginate(TopicArn=topic_arn):
        existing += [s for s in page["Subscriptions"] if s["Endpoint"].lower() == email.lower()]
    if existing:
        print(f"  SNS: {email} already subscribed ({existing[0]['SubscriptionArn'][:40]}...)")
        return
    sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email, ReturnSubscriptionArn=True,
                  Attributes={"FilterPolicy": json.dumps({"recipient_email": [email]})})
    print(f"  SNS: subscription sent to {email} - open that inbox and click 'Confirm subscription'")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="leave-mgmt")
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--employee-email", required=True)
    ap.add_argument("--manager-email", required=True)
    ap.add_argument("--hr-email", required=True)
    ap.add_argument("--password", required=True,
                    help="Password for all three test users (8+ chars, upper, lower, number)")
    ap.add_argument("--verify-ses", action="store_true", help="Send SES verification emails to the 3 addresses")
    ap.add_argument("--reset", action="store_true", help="Delete all leave requests and balances first")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    out = stack_outputs(session.client("cloudformation"), args.stack)
    ddb = session.resource("dynamodb")
    config = ddb.Table(out["LeaveConfigTableName"])
    employees = ddb.Table(out["EmployeesTableName"])

    if args.reset:
        n1 = wipe(ddb.Table(out["LeaveRequestsTableName"]), ["employee_id", "request_id"])
        n2 = wipe(ddb.Table(out["LeaveBalancesTableName"]), ["employee_id", "leave_type_year"])
        print(f"Reset: deleted {n1} requests and {n2} balance records "
              "(stop any running executions in the Step Functions console if needed)")

    print("Leave configuration")
    for item in LEAVE_TYPES + [SETTINGS]:
        config.put_item(Item=item)
        print(f"  {item['leave_type']}")
    for year, dates in HOLIDAYS.items():
        config.put_item(Item={"leave_type": f"SYSTEM#HOLIDAYS#{year}", "dates": dates})
        print(f"  SYSTEM#HOLIDAYS#{year}: {', '.join(dates)}")

    people = [
        ("emp-001", "Ravi Kumar", args.employee_email, "mgr-001", "EMPLOYEE", "Engineering", ["Employee"]),
        ("mgr-001", "Meena Rao", args.manager_email, "hr-001", "MANAGER", "Engineering", ["Employee", "Manager"]),
        ("hr-001", "Harini S", args.hr_email, "mgr-001", "HR_ADMIN", "HR", ["Employee", "HRAdmin"]),
    ]
    print("Employees and logins")
    cognito = session.client("cognito-idp")
    for eid, name, email, manager, role, dept, groups in people:
        employees.put_item(Item={"employee_id": eid, "name": name, "email": email, "manager_id": manager,
                                 "role": role, "department": dept})
        upsert_user(cognito, out["UserPoolId"], email, name, eid, groups, args.password)

    print("Approver notifications (SNS)")
    sns = session.client("sns")
    for _, _, email, _, role, _, _ in people:
        if role in ("MANAGER", "HR_ADMIN"):
            subscribe(sns, out["ApprovalTopicArn"], email)

    if args.verify_ses:
        print("SES verification")
        ses = session.client("ses")
        for email in sorted({args.employee_email, args.manager_email, args.hr_email}):
            ses.verify_email_identity(EmailAddress=email)
            print(f"  verification email sent to {email} - click the link in it")

    print("\nDone. Sign in at:", out["WebsiteUrl"])
    print("(Upload the website first with: python scripts/deploy_frontend.py "
          f"--stack {args.stack} --region {args.region})")


if __name__ == "__main__":
    main()

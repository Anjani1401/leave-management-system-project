"""DynamoDB helpers shared by all Lambdas."""
import os
from decimal import Decimal

from common import aws

# Request statuses
PENDING_MANAGER = "PENDING_MANAGER"
PENDING_HR = "PENDING_HR"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
AUTO_REJECTED = "AUTO_REJECTED"

PENDING_STATUSES = (PENDING_MANAGER, PENDING_HR)
ACTIVE_STATUSES = (PENDING_MANAGER, PENDING_HR, APPROVED)  # block overlaps
ALL_STATUSES = (PENDING_MANAGER, PENDING_HR, APPROVED, REJECTED, AUTO_REJECTED)

# Approval levels (stored on the request as pending_level while waiting)
LEVEL_MANAGER = "MANAGER"
LEVEL_HR = "HR"
LEVEL_ESCALATION = "ESCALATION"  # manager did not respond in time -> HR decides


def _table(env_name):
    return aws.dynamodb().Table(os.environ[env_name])


def config_table():
    return _table("CONFIG_TABLE")


def balances_table():
    return _table("BALANCES_TABLE")


def requests_table():
    return _table("REQUESTS_TABLE")


def employees_table():
    return _table("EMPLOYEES_TABLE")


def balance_key(employee_id, leave_type, year):
    return {"employee_id": employee_id, "leave_type_year": f"{leave_type}#{int(year)}"}


def plain(value):
    """Convert DynamoDB Decimals (recursively) into int/float for JSON output."""
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted(plain(v) for v in value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def num(value, default=0):
    """Read a numeric attribute as int (all leave amounts are whole days)."""
    if value is None:
        return default
    return int(Decimal(str(value)))


def query_all(table, **kwargs):
    """Run a Query and follow LastEvaluatedKey until every page is read."""
    items = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def scan_all(table, **kwargs):
    items = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


class TransactionCancelled(Exception):
    """Raised when a TransactWriteItems call is cancelled.

    `reasons` holds one code per operation, in order, e.g.
    ["None", "ConditionalCheckFailed"].
    """

    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__(f"Transaction cancelled: {reasons}")


def transact_update(operations):
    """Atomically apply several UpdateItem operations (all succeed or none do).

    Each operation is a dict with: table_env, Key, UpdateExpression and optional
    ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues.
    Values are plain Python values; they are serialised here.
    """
    if "transact" in aws._overrides:  # unit tests
        return aws._overrides["transact"](operations)

    from boto3.dynamodb.types import TypeSerializer

    ser = TypeSerializer()
    items = []
    for op in operations:
        update = {
            "TableName": os.environ[op["table_env"]],
            "Key": {k: ser.serialize(v) for k, v in op["Key"].items()},
            "UpdateExpression": op["UpdateExpression"],
        }
        if op.get("ConditionExpression"):
            update["ConditionExpression"] = op["ConditionExpression"]
        if op.get("ExpressionAttributeNames"):
            update["ExpressionAttributeNames"] = op["ExpressionAttributeNames"]
        if op.get("ExpressionAttributeValues"):
            update["ExpressionAttributeValues"] = {
                k: ser.serialize(v) for k, v in op["ExpressionAttributeValues"].items()
            }
        items.append({"Update": update})
    try:
        aws.dynamodb_client().transact_write_items(TransactItems=items)
    except aws.ClientError as err:
        if aws.error_code(err) == "TransactionCanceledException":
            reasons = [r.get("Code", "None") for r in err.response.get("CancellationReasons", [])]
            raise TransactionCancelled(reasons) from err
        raise

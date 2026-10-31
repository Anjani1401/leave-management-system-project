"""In-memory fakes of the AWS services used by the Lambdas.

The DynamoDB fake really parses and evaluates the expression strings the code
sends (SET/REMOVE, conditions, key conditions, filters), and enforces the same
rules DynamoDB does: reserved words must use #placeholders, every placeholder
must be defined, and unused placeholders are an error. That way the tests
catch expression mistakes that would otherwise only show up after deploying.
"""
import copy
import json
import os
import re
from decimal import Decimal

from common.aws import ClientError

RESERVED = set(open(os.path.join(os.path.dirname(__file__), "reserved_words.txt")).read().split())


def _err(code, message="", **extra):
    return ClientError({"Error": {"Code": code, "Message": message}, **extra}, "Fake")


def _store(value):
    """Mimic boto3: numbers come back as Decimal, floats are rejected."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, (int, Decimal)):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _store(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_store(v) for v in value]
    if isinstance(value, set):
        return {_store(v) for v in value}
    raise TypeError(f"Unsupported type {type(value)}")


# --------------------------------------------------------------------- expressions

TOKEN_RE = re.compile(r"\s*(<>|<=|>=|[=<>(),+\-]|#[A-Za-z0-9_]+|:[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*)")
_MISSING = object()
KEYWORDS = {"AND", "OR", "NOT", "IN", "BETWEEN", "SET", "REMOVE", "ADD", "DELETE"}


class Expr:
    def __init__(self, text, names, values):
        self.tokens = []
        pos = 0
        text = text or ""
        while pos < len(text):
            m = TOKEN_RE.match(text, pos)
            if not m:
                if text[pos:].strip() == "":
                    break
                raise _err("ValidationException", f"Invalid expression near: {text[pos:]}")
            self.tokens.append(m.group(1))
            pos = m.end()
        self.i = 0
        self.names = names or {}
        self.values = values or {}
        # Static validation, like DynamoDB does before touching any item
        self.used_names = {t for t in self.tokens if t.startswith("#")}
        self.used_values = {t for t in self.tokens if t.startswith(":")}
        for t in self.used_names:
            if t not in self.names:
                raise _err("ValidationException", f"An expression attribute name used in the document path is not defined; attribute name: {t}")
        for t in self.used_values:
            if t not in self.values:
                raise _err("ValidationException", f"An expression attribute value used in expression is not defined; attribute value: {t}")
        for j, t in enumerate(self.tokens):
            nxt = self.tokens[j + 1] if j + 1 < len(self.tokens) else None
            if re.match(r"^[A-Za-z_]", t) and nxt != "(" and t.upper() not in KEYWORDS and t.upper() in RESERVED:
                raise _err("ValidationException", f"Attribute name is a reserved keyword; reserved keyword: {t}")

    # token helpers
    def peek(self, offset=0):
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def take(self, expected=None):
        tok = self.peek()
        if tok is None:
            raise _err("ValidationException", "Unexpected end of expression")
        if expected and tok.upper() != expected:
            raise _err("ValidationException", f"Expected {expected}, got {tok}")
        self.i += 1
        return tok

    def done(self):
        return self.i >= len(self.tokens)

    # name/value resolution
    def attr_name(self, tok):
        if tok.startswith("#"):
            if tok not in self.names:
                raise _err("ValidationException", f"An expression attribute name used in the document path is not defined; attribute name: {tok}")
            return self.names[tok]
        if tok.upper() in RESERVED:
            raise _err("ValidationException", f"Attribute name is a reserved keyword; reserved keyword: {tok}")
        if "." in tok:
            raise _err("ValidationException", "Nested paths are not supported by this fake")
        return tok

    def value(self, tok):
        if tok not in self.values:
            raise _err("ValidationException", f"An expression attribute value used in expression is not defined; attribute value: {tok}")
        return _store(self.values[tok])

    def check_all_used(self, others=()):
        used_n = set(self.used_names)
        used_v = set(self.used_values)
        for o in others:
            used_n |= o.used_names
            used_v |= o.used_values
        unused_v = set(self.values) - used_v
        unused_n = set(self.names) - used_n
        if unused_v:
            raise _err("ValidationException", f"Value provided in ExpressionAttributeValues unused in expressions: keys: {sorted(unused_v)}")
        if unused_n:
            raise _err("ValidationException", f"Value provided in ExpressionAttributeNames unused in expressions: keys: {sorted(unused_n)}")

    # operands
    def operand(self, item):
        tok = self.take()
        low = tok.lower()
        if low == "if_not_exists" and self.peek() == "(":
            self.take("(")
            path = self.attr_name(self.take())
            self.take(",")
            default = self.operand(item)
            self.take(")")
            return item.get(path, default) if path in item else default
        if low == "list_append" and self.peek() == "(":
            self.take("(")
            a = self.operand(item)
            self.take(",")
            b = self.operand(item)
            self.take(")")
            if not isinstance(a, list) or not isinstance(b, list):
                raise _err("ValidationException", "list_append operands must be lists (attribute missing?)")
            return a + b
        if low == "size" and self.peek() == "(":
            self.take("(")
            v = self.operand(item)
            self.take(")")
            return Decimal(len(v))
        if tok.startswith(":"):
            return self.value(tok)
        return item.get(self.attr_name(tok), _MISSING)

    def arith(self, item):
        left = self.operand(item)
        while self.peek() in ("+", "-"):
            op = self.take()
            right = self.operand(item)
            if left is _MISSING or right is _MISSING:
                raise _err("ValidationException", "The provided expression refers to an attribute that does not exist in the item")
            if not isinstance(left, Decimal) or not isinstance(right, Decimal):
                raise _err("ValidationException", "Incorrect operand type for operator or function")
            left = left + right if op == "+" else left - right
        return left

    # conditions
    def condition(self, item):
        result = self.and_expr(item)
        while self.peek() and self.peek().upper() == "OR":
            self.take()
            right = self.and_expr(item)
            result = result or right
        return result

    def and_expr(self, item):
        result = self.not_expr(item)
        while self.peek() and self.peek().upper() == "AND":
            self.take()
            right = self.not_expr(item)
            result = result and right
        return result

    def not_expr(self, item):
        if self.peek() and self.peek().upper() == "NOT":
            self.take()
            return not self.not_expr(item)
        return self.primary(item)

    def primary(self, item):
        tok = self.peek()
        if tok == "(":
            self.take("(")
            r = self.condition(item)
            self.take(")")
            return r
        low = (tok or "").lower()
        if low in ("attribute_exists", "attribute_not_exists") and self.peek(1) == "(":
            self.take()
            self.take("(")
            name = self.attr_name(self.take())
            self.take(")")
            return (name in item) if low == "attribute_exists" else (name not in item)
        if low == "begins_with" and self.peek(1) == "(":
            self.take()
            self.take("(")
            a = self.operand(item)
            self.take(",")
            b = self.operand(item)
            self.take(")")
            return isinstance(a, str) and isinstance(b, str) and a.startswith(b)
        if low == "contains" and self.peek(1) == "(":
            self.take()
            self.take("(")
            a = self.operand(item)
            self.take(",")
            b = self.operand(item)
            self.take(")")
            return a is not _MISSING and b in a
        left = self.operand(item)
        op = self.take()
        if op.upper() == "IN":
            self.take("(")
            options = [self.operand(item)]
            while self.peek() == ",":
                self.take(",")
                options.append(self.operand(item))
            self.take(")")
            return left is not _MISSING and left in options
        if op.upper() == "BETWEEN":
            low_v = self.operand(item)
            self.take("AND")
            high_v = self.operand(item)
            return left is not _MISSING and low_v <= left <= high_v
        right = self.operand(item)
        if left is _MISSING or right is _MISSING:
            return False if op != "<>" else (left is not right)
        if type(left) is not type(right) and not (isinstance(left, Decimal) and isinstance(right, Decimal)):
            return op == "<>"
        return {"=": left == right, "<>": left != right, "<": left < right, "<=": left <= right,
                ">": left > right, ">=": left >= right}[op]

    def evaluate_condition(self, item):
        if not self.tokens:
            return True
        self.i = 0
        r = self.condition(item)
        if not self.done():
            raise _err("ValidationException", f"Unexpected token {self.peek()}")
        return r

    def apply_update(self, item):
        """Return a new item with the update applied."""
        new = copy.deepcopy(item)
        self.i = 0
        clause = None
        while not self.done():
            tok = self.peek()
            if tok.upper() in ("SET", "REMOVE", "ADD", "DELETE") and (clause is None or self._at_clause_start()):
                clause = self.take().upper()
                continue
            if clause == "SET":
                name = self.attr_name(self.take())
                self.take("=")
                value = self.arith(item)
                if value is _MISSING:
                    raise _err("ValidationException", "The provided expression refers to an attribute that does not exist in the item")
                new[name] = value
            elif clause == "REMOVE":
                name = self.attr_name(self.take())
                new.pop(name, None)
            elif clause == "ADD":
                name = self.attr_name(self.take())
                value = self.operand(item)
                new[name] = new.get(name, Decimal(0)) + value
            else:
                raise _err("ValidationException", f"Unsupported update clause {clause}")
            if self.peek() == ",":
                self.take(",")
        return new

    def _at_clause_start(self):
        prev = self.tokens[self.i - 1] if self.i > 0 else None
        return prev != "," and prev not in ("=", "+", "-", "(")


# --------------------------------------------------------------------- DynamoDB

class FakeTable:
    def __init__(self, name, hash_key, range_key=None, indexes=None):
        self.name = name
        self.hash_key = hash_key
        self.range_key = range_key
        self.indexes = indexes or {}  # name -> (hash, range)
        self.items = {}

    def _key(self, key):
        expected = {self.hash_key} | ({self.range_key} if self.range_key else set())
        if set(key) != expected:
            raise _err("ValidationException", f"The provided key element does not match the schema: {key}")
        return tuple(key[k] for k in sorted(expected))

    def get_item(self, Key, **_):
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None,
                 ExpressionAttributeValues=None, **_):
        key = {k: Item[k] for k in [self.hash_key, self.range_key] if k}
        existing = self.items.get(self._key(key), {})
        cond = Expr(ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues)
        ok = cond.evaluate_condition(existing)
        cond.check_all_used()
        if not ok:
            raise _err("ConditionalCheckFailedException", "The conditional request failed")
        self.items[self._key(key)] = _store(Item)
        return {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None, **_):
        new, k = self._prepare_update(Key, UpdateExpression, ConditionExpression,
                                      ExpressionAttributeNames, ExpressionAttributeValues)
        self.items[k] = new
        return {"Attributes": copy.deepcopy(new)} if ReturnValues == "ALL_NEW" else {}

    def _prepare_update(self, Key, UpdateExpression, ConditionExpression, names, values):
        k = self._key(Key)
        existing = self.items.get(k, {})
        cond = Expr(ConditionExpression, names, values)
        ok = cond.evaluate_condition(existing)
        upd = Expr(UpdateExpression, names, values)
        base = existing if existing else {kk: _store(vv) for kk, vv in Key.items()}
        new = upd.apply_update(base)
        upd.check_all_used([cond])
        if not ok:
            raise _err("ConditionalCheckFailedException", "The conditional request failed")
        for key_attr in Key:
            if new.get(key_attr) != _store(Key[key_attr]):
                raise _err("ValidationException", "Cannot update attribute that is part of the key")
        return new, k

    def query(self, KeyConditionExpression, IndexName=None, FilterExpression=None,
              ExpressionAttributeNames=None, ExpressionAttributeValues=None, ScanIndexForward=True,
              ExclusiveStartKey=None, Limit=None, **_):
        if IndexName:
            if IndexName not in self.indexes:
                raise _err("ValidationException", f"The table does not have the specified index: {IndexName}")
            hash_key, range_key = self.indexes[IndexName]
        else:
            hash_key, range_key = self.hash_key, self.range_key
        kc = Expr(KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues)
        flt = Expr(FilterExpression, ExpressionAttributeNames, ExpressionAttributeValues)
        # Key condition may only use key attributes
        results = []
        candidates = [i for i in self.items.values() if hash_key in i and (not range_key or range_key in i)]
        for item in candidates:
            if kc.evaluate_condition(item) and flt.evaluate_condition(item):
                results.append(copy.deepcopy(item))
        if not candidates:  # still validate placeholders
            kc.evaluate_condition({})
            flt.evaluate_condition({})
        referenced = {self_name for self_name in kc.used_names}
        key_names = {kc.names[n] for n in referenced}
        bare = {t for t in kc.tokens if re.match(r"^[A-Za-z_]", t) and t.upper() not in ("AND", "BEGINS_WITH", "BETWEEN")}
        if (key_names | bare) - {hash_key, range_key}:
            raise _err("ValidationException", "Query key condition not supported (non-key attribute)")
        kc.check_all_used([flt])
        if range_key:
            results.sort(key=lambda i: i[range_key], reverse=not ScanIndexForward)
        return {"Items": results, "Count": len(results)}

    def scan(self, FilterExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None,
             ExclusiveStartKey=None, **_):
        flt = Expr(FilterExpression, ExpressionAttributeNames, ExpressionAttributeValues)
        results = [copy.deepcopy(i) for i in self.items.values() if flt.evaluate_condition(i)]
        if not self.items:
            flt.evaluate_condition({})
        flt.check_all_used()
        return {"Items": results, "Count": len(results)}

    def delete_item(self, Key, **_):
        self.items.pop(self._key(Key), None)
        return {}


class FakeDynamo:
    def __init__(self):
        self.tables = {
            os.environ["CONFIG_TABLE"]: FakeTable("config", "leave_type"),
            os.environ["BALANCES_TABLE"]: FakeTable("balances", "employee_id", "leave_type_year"),
            os.environ["REQUESTS_TABLE"]: FakeTable(
                "requests", "employee_id", "request_id",
                indexes={"manager-status-index": ("manager_id", "status_created"),
                         "status-start-index": ("status", "start_date")}),
            os.environ["EMPLOYEES_TABLE"]: FakeTable("employees", "employee_id"),
        }
        self.fail_next_transaction = None

    def Table(self, name):
        return self.tables[name]

    def transact(self, operations):
        """Atomic: validate every operation first, then apply all of them."""
        if len(operations) > 100:
            raise _err("ValidationException", "Too many items")
        prepared, reasons, failed = [], [], False
        for op in operations:
            table = self.tables[os.environ[op["table_env"]]]
            try:
                new, k = table._prepare_update(op["Key"], op["UpdateExpression"], op.get("ConditionExpression"),
                                               op.get("ExpressionAttributeNames"), op.get("ExpressionAttributeValues"))
                prepared.append((table, k, new))
                reasons.append("None")
            except ClientError as err:
                if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    reasons.append("ConditionalCheckFailed")
                    failed = True
                else:
                    raise
        if failed:
            from common.db import TransactionCancelled
            raise TransactionCancelled(reasons)
        for table, k, new in prepared:
            table.items[k] = new


# --------------------------------------------------------------------- other services

class FakeSES:
    def __init__(self):
        self.sent = []
        self.fail_for = set()

    def send_email(self, Source, Destination, Message):
        to = Destination["ToAddresses"][0]
        if to in self.fail_for:
            raise _err("MessageRejected", "Email address is not verified.")
        self.sent.append({"from": Source, "to": to, "subject": Message["Subject"]["Data"],
                          "text": Message["Body"]["Text"]["Data"], "html": Message["Body"]["Html"]["Data"]})
        return {"MessageId": f"ses-{len(self.sent)}"}

    def to(self, address):
        return [m for m in self.sent if m["to"] == address]


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, TopicArn, Message, Subject=None, MessageAttributes=None):
        if Subject and len(Subject) >= 100:
            raise _err("InvalidParameter", "Subject must be less than 100 characters")
        self.published.append({"topic": TopicArn, "subject": Subject, "message": Message,
                               "recipient": MessageAttributes["recipient_email"]["StringValue"]})
        return {"MessageId": f"sns-{len(self.published)}"}


class FakeSFN:
    """Records StartExecution; SendTaskSuccess is routed to the workflow simulator."""

    def __init__(self):
        self.started = []
        self.task_results = {}   # token -> output dict
        self.closed_tokens = set()
        self.fail_start = False

    def start_execution(self, stateMachineArn, name, input):
        if self.fail_start:
            raise _err("ServiceUnavailable", "boom")
        if any(s["name"] == name for s in self.started):
            raise _err("ExecutionAlreadyExists", name)
        self.started.append({"arn": stateMachineArn, "name": name, "input": json.loads(input)})
        return {"executionArn": f"arn:aws:states:ap-south-1:123:execution:sm:{name}"}

    def send_task_success(self, taskToken, output):
        if taskToken in self.closed_tokens or taskToken in self.task_results:
            raise _err("TaskTimedOut", "Task Timed Out")
        self.task_results[taskToken] = json.loads(output)
        return {}


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = Body
        return {}

    def generate_presigned_url(self, method, Params, ExpiresIn):
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?X-Amz-Expires={ExpiresIn}"

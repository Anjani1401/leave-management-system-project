"""Leave configuration, balances and the quota/overlap rules.

Nothing about leave types is hardcoded: quotas and rules are read from the
leave_config table, so HR can change them without a code change.

leave_config items:
  * one item per leave type, keyed by leave_type (e.g. "SICK")
  * "SYSTEM#SETTINGS"          -> workflow settings (HR approval threshold)
  * "SYSTEM#HOLIDAYS#<year>"   -> company holidays not counted as leave days
"""
from common import aws, db
from common.dates import now_iso, parse_date, ranges_overlap

SETTINGS_KEY = "SYSTEM#SETTINGS"
HOLIDAYS_PREFIX = "SYSTEM#HOLIDAYS#"

DEFAULT_SETTINGS = {"hr_approval_threshold_days": 5}

LEAVE_TYPE_DEFAULTS = {
    "name": None,
    "annual_quota": 0,
    "requires_balance": True,
    "carry_forward": False,
    "max_carry_forward": 0,
    "min_notice_days": 0,
    "max_backdate_days": 0,
    "max_consecutive_days": 365,
    "active": True,
}


def _normalise_type(item):
    cfg = dict(LEAVE_TYPE_DEFAULTS)
    cfg.update(item)
    cfg["leave_type"] = item["leave_type"]
    cfg["name"] = cfg.get("name") or item["leave_type"].title()
    for field in ("annual_quota", "max_carry_forward", "min_notice_days",
                  "max_backdate_days", "max_consecutive_days"):
        cfg[field] = db.num(cfg.get(field))
    for field in ("requires_balance", "carry_forward", "active"):
        cfg[field] = bool(cfg.get(field))
    return cfg


def load_leave_types(include_inactive=False):
    items = db.scan_all(db.config_table())
    types = [
        _normalise_type(i)
        for i in items
        if not str(i.get("leave_type", "")).startswith("SYSTEM#")
    ]
    if not include_inactive:
        types = [t for t in types if t["active"]]
    return sorted(types, key=lambda t: t["leave_type"])


def get_leave_type(leave_type):
    if not leave_type or str(leave_type).startswith("SYSTEM#"):
        return None
    item = db.config_table().get_item(Key={"leave_type": leave_type}).get("Item")
    if not item:
        return None
    cfg = _normalise_type(item)
    return cfg if cfg["active"] else None


def get_settings():
    item = db.config_table().get_item(Key={"leave_type": SETTINGS_KEY}).get("Item") or {}
    settings = dict(DEFAULT_SETTINGS)
    if "hr_approval_threshold_days" in item:
        settings["hr_approval_threshold_days"] = db.num(item["hr_approval_threshold_days"])
    return settings


def get_holidays(year):
    item = db.config_table().get_item(Key={"leave_type": f"{HOLIDAYS_PREFIX}{int(year)}"}).get("Item") or {}
    return [str(d) for d in item.get("dates", [])]


# ---------------------------------------------------------------- balances

def get_balance(employee_id, leave_type, year):
    return db.balances_table().get_item(Key=db.balance_key(employee_id, leave_type, year)).get("Item")


def ensure_balance(employee_id, cfg, year):
    """Return the balance record for (employee, type, year), creating it if needed.

    A new record gets the configured annual quota. For carry-forward leave types
    it also gets min(unused balance of the previous year, max_carry_forward).
    Creating the record is conditional, so calling this twice (from the submit
    Lambda and from the weekly job) can never credit the carry-forward twice.
    """
    table = db.balances_table()
    existing = get_balance(employee_id, cfg["leave_type"], year)
    if existing:
        return existing

    carried = 0
    previous = None
    if cfg["carry_forward"]:
        previous = get_balance(employee_id, cfg["leave_type"], int(year) - 1)
        if previous:
            unused = max(db.num(previous.get("available")), 0)
            carried = min(unused, cfg["max_carry_forward"])

    quota = cfg["annual_quota"]
    item = {
        **db.balance_key(employee_id, cfg["leave_type"], year),
        "leave_type": cfg["leave_type"],
        "year": int(year),
        "allocated": quota,
        "carried_forward": carried,
        "used": 0,
        "available": quota + carried if cfg["requires_balance"] else 0,
        "requires_balance": cfg["requires_balance"],
        "created_at": now_iso(),
        "version": 1,
    }
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(employee_id)")
    except aws.ClientError as err:
        if aws.error_code(err) == "ConditionalCheckFailedException":
            return get_balance(employee_id, cfg["leave_type"], year)
        raise

    if previous is not None:
        # Record on last year's balance how much was carried into this year.
        table.update_item(
            Key=db.balance_key(employee_id, cfg["leave_type"], int(year) - 1),
            UpdateExpression="SET carry_forward_done = :t, carried_to_next_year = :c",
            ExpressionAttributeValues={":t": True, ":c": carried},
        )
    return item


# ---------------------------------------------------------------- requests

def list_employee_requests(employee_id):
    return db.query_all(
        db.requests_table(),
        KeyConditionExpression="employee_id = :e",
        ExpressionAttributeValues={":e": employee_id},
    )


def find_overlap(requests, start, end):
    """Return the first approved/pending request that shares a day with [start, end]."""
    for req in requests:
        if req.get("status") not in db.ACTIVE_STATUSES:
            continue
        if ranges_overlap(start, end, parse_date(req["start_date"]), parse_date(req["end_date"])):
            return req
    return None


def pending_days(requests, leave_type, year):
    """Working days already requested but not yet approved for this type/year."""
    return sum(
        db.num(r.get("days"))
        for r in requests
        if r.get("status") in db.PENDING_STATUSES
        and r.get("leave_type") == leave_type
        and db.num(r.get("year")) == int(year)
    )


def get_request(employee_id, request_id):
    return db.requests_table().get_item(
        Key={"employee_id": employee_id, "request_id": request_id}
    ).get("Item")


def get_employee(employee_id):
    if not employee_id:
        return None
    return db.employees_table().get_item(Key={"employee_id": employee_id}).get("Item")


def list_hr_admins():
    items = db.scan_all(
        db.employees_table(),
        FilterExpression="#r = :hr",
        ExpressionAttributeNames={"#r": "role"},
        ExpressionAttributeValues={":hr": "HR_ADMIN"},
    )
    return sorted(items, key=lambda i: i["employee_id"])


PRIVATE_FIELDS = {"task_token", "execution_arn", "status_created", "pending_level_claim"}


def public_request(req):
    """Strip internal fields (task tokens etc.) before sending a request to a browser."""
    return db.plain({k: v for k, v in req.items() if k not in PRIVATE_FIELDS})


def history_entry(status, by, note=""):
    return [{"at": now_iso(), "status": status, "by": by, "note": note}]

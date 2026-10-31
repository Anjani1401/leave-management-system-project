"""Weekly job (EventBridge Scheduler, Mondays 09:00 IST).

1. Re-credit carry-forward: makes sure every employee has a balance record for
   the current year for every leave type. When a new year's record is created,
   the unused balance from last year is carried over (up to max_carry_forward).
   Record creation is conditional, so running this every week is safe: the
   carry-forward is only ever credited once per year.
2. Emails every employee a summary of their balances.

Invoke manually with {"dry_run": true} to see what it would do without emailing,
or {"year": 2027} to simulate a new leave year.
"""
import logging

from common import db, leave_rules, notify
from common.dates import today_local

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    event = event or {}
    year = int(event.get("year") or today_local().year)
    dry_run = bool(event.get("dry_run"))
    leave_types = leave_rules.load_leave_types()
    employees = db.scan_all(db.employees_table())

    summary = {"year": year, "employees": 0, "balances_created": 0, "carried_forward_days": 0,
               "emails_sent": 0, "errors": []}
    for emp in sorted(employees, key=lambda e: e["employee_id"]):
        try:
            requests = leave_rules.list_employee_requests(emp["employee_id"])
            rows = []
            for cfg in leave_types:
                existed = leave_rules.get_balance(emp["employee_id"], cfg["leave_type"], year) is not None
                bal = leave_rules.ensure_balance(emp["employee_id"], cfg, year)
                if not existed:
                    summary["balances_created"] += 1
                    summary["carried_forward_days"] += db.num(bal.get("carried_forward"))
                pending = leave_rules.pending_days(requests, cfg["leave_type"], year)
                if cfg["requires_balance"]:
                    rows.append((cfg["name"],
                                 f"{db.num(bal.get('available'))} available "
                                 f"(quota {db.num(bal.get('allocated'))}, carried forward "
                                 f"{db.num(bal.get('carried_forward'))}, used {db.num(bal.get('used'))}, "
                                 f"pending {pending})"))
                else:
                    rows.append((cfg["name"], f"{db.num(bal.get('used'))} day(s) used, pending {pending}"))
            summary["employees"] += 1
            if not dry_run and emp.get("email"):
                if notify.send_email(emp["email"], f"Your weekly leave balance summary ({year})",
                                     f"Leave balance summary - {year}", rows,
                                     intro=f"Hi {emp.get('name', '')}, here is your current leave balance.",
                                     footer="Sent every Monday by the Leave Management System."):
                    summary["emails_sent"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the job
            log.exception("Weekly job failed for %s", emp.get("employee_id"))
            summary["errors"].append({"employee_id": emp.get("employee_id"), "error": str(exc)})

    log.info("WEEKLY_SUMMARY %s", summary)
    return summary

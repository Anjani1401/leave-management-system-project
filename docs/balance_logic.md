# Balance logic walkthrough

This document explains how leave quotas are stored, checked and deducted. The code is in
`backend/src/common/leave_rules.py`, `handlers/submit.py`, `handlers/final_approve.py` and
`handlers/weekly_balance.py`.

## 1. Where the rules come from

Nothing about leave types is hardcoded. Every rule is a field on an item in `leave_config`:

| Field | Meaning | Seed value (Sick / Casual / Earned / Unpaid) |
|---|---|---|
| `annual_quota` | Days credited each year | 12 / 10 / 18 / 0 |
| `requires_balance` | Must the employee have enough balance? | yes / yes / yes / **no** |
| `carry_forward`, `max_carry_forward` | Unused days moved into next year, and the cap | no / no / **yes, 10** / no |
| `min_notice_days` | Days between today and the start date | 0 / 1 / 3 / 3 |
| `max_backdate_days` | How late (after the start date) it can be applied | **7** / 0 / 0 / 0 |
| `max_consecutive_days` | Most working days in one request | 15 / 5 / 30 / 60 |
| `active` | Turn a leave type off without deleting it | yes |

Two more items: `SYSTEM#SETTINGS` holds `hr_approval_threshold_days` (5), and
`SYSTEM#HOLIDAYS#<year>` lists company holidays.

HR edits these items in the DynamoDB console; the next request uses the new values.

## 2. The balance record

One item per **employee + leave type + year** in `leave_balances`:

```
PK employee_id = "emp-001"      SK leave_type_year = "EARNED#2026"
allocated = 18   carried_forward = 4   used = 6   available = 16   version = 3
```

`available = allocated + carried_forward − used` (kept as its own field so it can be checked in a
DynamoDB condition). The record is created automatically the first time it's needed
(`ensure_balance`), from `annual_quota` in the config.

## 3. How many days a request costs

`days` = the dates from `start_date` to `end_date` inclusive, **minus Saturdays, Sundays and the
holidays** in `SYSTEM#HOLIDAYS#<year>`. Example: Fri 6 Nov – Tue 10 Nov 2026 = Fri, Mon, Tue =
**3 days**. A request with 0 working days (e.g. only a weekend) is refused as invalid input.

## 4. Checks at submission (in this order)

Input problems (bad dates, unknown type, end before start, crossing into a new year) are
refused with HTTP 400 and **not saved**. The business rules below save the request as
`AUTO_REJECTED`, email the employee the reason, and return HTTP 422. The approval workflow is
never started for them.

1. **Notice / backdating.** A future start needs `min_notice_days` of notice. A past start is only
   allowed up to `max_backdate_days` (so sick leave can be logged after the fact, casual leave
   can't).
2. **Length.** `days` must not exceed `max_consecutive_days`.
3. **Overlap.** If the employee already has an `APPROVED`, `PENDING_MANAGER` or `PENDING_HR`
   request that shares any day with the new one, it is rejected. Two ranges overlap when
   `new.start ≤ existing.end AND new.end ≥ existing.start`. Rejected and auto-rejected
   requests do not block dates. This also stops accidental duplicate submissions.
4. **Balance** (only when `requires_balance` is true):

   ```
   usable = available − (days in this employee's PENDING requests of the same type and year)
   reject if days > usable
   ```

   Pending days are subtracted because the balance itself is not reduced until approval.
   Without this, an employee with 10 days could submit two 8-day requests that each pass.

   Example: Earned `available` = 18, one 8-day request pending → usable = 10. A new 11-day
   request is rejected with *"Insufficient Earned leave balance: you requested 11 working day(s)
   but only 10 day(s) are available (8 day(s) are already pending approval)."*

If everything passes, the request is saved as `PENDING_MANAGER` and the Step Functions workflow
starts. **The balance is not changed at this point.**

`requires_hr` is set to `days > hr_approval_threshold_days`, so leave of 6+ working days goes to
HR Admin after the manager.

## 5. Deduction: only at final approval

The `FinalApprove` step is the **only** code that lowers a balance. It runs one DynamoDB
transaction with two writes that succeed or fail together:

| Write | Condition |
|---|---|
| request: `status = APPROVED` | status is still `PENDING_MANAGER` or `PENDING_HR` |
| balance: `available -= days`, `used += days`, `version += 1` | `available >= days` |

Consequences:
- **No double counting.** A request can be approved only once; a second attempt fails the first
  condition and changes nothing.
- **Never negative.** If the balance changed after submission (for example HR corrected it), the
  second condition fails, nothing is written, and the workflow takes the `RejectBalanceChanged`
  path: the request is rejected with that reason and the balance is left as it was.
- **Rejections never touch the balance**, whoever rejects (manager, HR, timeout).

Unpaid leave (`requires_balance = false`) is never checked against a balance; approval only adds
the days to `used` for reporting.

## 6. Carry-forward and the weekly job

EventBridge runs `WeeklyBalanceFunction` every Monday at 09:00 IST. For every employee and active
leave type it makes sure the current year's balance record exists. When a **new year's** record is
created for a carry-forward type:

```
carried_forward = min(last year's available, max_carry_forward)
available       = annual_quota + carried_forward
```

and last year's record is marked `carry_forward_done = true`, `carried_to_next_year = N`.

Example: 14 Earned days unused in 2026, cap 10 → 2027 starts with 18 + 10 = **28** days.

Creating the record is conditional (`attribute_not_exists`), so:
- the job can run every week and the carry-forward is credited **only once per year**;
- if an employee submits leave in January before the job runs, the submit Lambda creates the
  record with the same carry-forward logic, and the job later finds it and changes nothing.

The job then emails each employee a summary: available, quota, carried forward, used and pending,
per leave type. Run it manually with `{"dry_run": true}` (no emails) or `{"year": 2027}`.

## 7. Where each rule is tested

| Rule | Test (in `backend/tests/`) |
|---|---|
| Insufficient balance auto-rejects, exact balance allowed | `test_submit_rules.py::test_insufficient_balance_is_auto_rejected`, `test_exact_balance_is_allowed` |
| Pending days count against the balance | `test_pending_requests_count_against_balance` |
| Deducted only on final approval, exactly once | `test_workflow.py::test_short_leave_manager_approves`, `test_double_click_is_idempotent` |
| Rejections don't touch the balance | `test_manager_rejects`, `test_long_leave_hr_rejects_balance_unchanged` |
| Balance changed before approval | `test_balance_changed_before_final_approval` |
| Unpaid leave | `test_unpaid_leave_no_balance_needed` |
| Weekends, holidays, notice, backdating, max length | `test_submit_rules.py` (date and rule tests) |
| Config changes without code | `test_hr_can_change_rules_in_config`, `test_new_leave_type_added_by_hr_works_immediately` |
| Carry-forward once, cap, idempotent | `test_api_weekly.py::test_weekly_job_carries_forward_once`, `test_submit_in_new_year_before_weekly_job_still_carries_forward` |

# Edge case report

Each case below lists the expected behaviour, how the system implements it, the automated test
that proves it, and the screenshot to capture from the deployed system.
Screenshots go in `docs/screenshots/` with the names given.

Automated tests: `cd backend && python -m pytest -q` → **60 passed**. The workflow tests run the
real `statemachine/leave_approval.asl.json` through the real Lambda handlers.

---

## 1. Insufficient balance

**Scenario.** Employee has 2 Casual days left and applies for 3 (Mon–Wed).

**Behaviour.**
- The submit Lambda reads `leave_balances`, subtracts days already pending, and finds
  3 > 2.
- The request is saved with status `AUTO_REJECTED` and
  `rejection_reason = "Insufficient Casual leave balance: you requested 3 working day(s) but only 2 day(s) are available."`
- The employee gets an "auto-rejected" SES email with that reason; the web form shows it too.
- No Step Functions execution is started and the balance is unchanged.

**Variants covered.**
- Pending requests count against the balance (two requests can't together exceed it).
- Asking for exactly the remaining balance is allowed.
- If the balance drops *after* submission and before approval, the final approval transaction
  fails its `available >= days` condition, nothing is deducted, and the workflow rejects via
  `RejectBalanceChanged`. The balance can never go negative.
- Unpaid leave has no balance check.

**Tests.** `test_insufficient_balance_is_auto_rejected`, `test_exact_balance_is_allowed`,
`test_pending_requests_count_against_balance`, `test_balance_changed_before_final_approval`,
`test_unpaid_leave_no_balance_needed`.

**Screenshots.** `01_insufficient_form.png` (red message in the form),
`01_insufficient_email.png` (auto-rejection email), `01_insufficient_dynamodb.png` (the item
with `AUTO_REJECTED` and `rejection_reason`), `01_no_execution.png` (no new execution).

---

## 2. Overlapping dates

**Scenario.** Employee has a pending Casual request for 9–11 Nov and applies for Sick leave on
11–12 Nov (or submits the same request twice).

**Behaviour.**
- The submit Lambda queries all of the employee's requests and checks
  `new.start ≤ existing.end AND new.end ≥ existing.start` against every `APPROVED`,
  `PENDING_MANAGER` or `PENDING_HR` request.
- The new request is `AUTO_REJECTED` with
  `"These dates overlap your pending manager request LR-2026-11-09-… (2026-11-09 to 2026-11-11)."`
- Adjacent dates (9–10 then 11–12) are allowed. Dates from a rejected or auto-rejected request
  can be requested again.

**Tests.** `test_overlap_with_pending_request`, `test_overlap_with_approved_request`,
`test_duplicate_submission_is_blocked`, `test_adjacent_dates_do_not_overlap`,
`test_rejected_dates_can_be_requested_again`, `test_auto_rejected_request_does_not_block_dates`.

**Screenshots.** `02_overlap_form.png`, `02_overlap_email.png`.

---

## 3. Manager inaction after 48 hours

**Scenario.** The manager never clicks the approval link.

**Behaviour.**
1. `NotifyManager` waits with a task token and `TimeoutSecondsPath` = 172 800 s (48 h, set by
   the `ApprovalTimeoutSeconds` parameter).
2. When it times out, Step Functions raises `States.Timeout`, which is caught and routed to
   **`EscalateToHR`**:
   - the request becomes `PENDING_HR` with `pending_level = ESCALATION` and `escalated = true`;
   - every HR Admin gets approve/reject links;
   - the employee gets an "escalated to HR" email.
3. The manager's old link now shows "Already processed" and cannot change anything (its level no
   longer matches the request, and the old task token is closed).
4. HR's decision then follows the normal path (`HRDecision → FinalApprove` or `RejectByHR`).
5. If HR also doesn't respond within the timeout, `RejectTimeout` rejects the request with
   *"No approval decision was made within the allowed time…"*. The balance is never touched.

The same timeout applies at the HR step for long leave (HR silent → `RejectTimeout`).

**Tests.** `test_manager_inaction_escalates_to_hr_then_hr_approves`,
`test_no_response_from_anyone_auto_rejects`, `test_hr_step_timeout_rejects`.

**How to show it live.** Deploy with `sam deploy --config-env demo` (2-minute timeout), apply for
leave and wait. Redeploy with plain `sam deploy` (48 h) afterwards.

**Screenshots.** `03_escalation_graph.png` (execution graph `NotifyManager → EscalateToHR`),
`03_escalation_email.png` (employee + HR emails), `03_old_link.png` (manager's link page),
`03_timeout_graph.png` (`EscalateToHR → RejectTimeout`).

---

## Other edge cases handled

| Case | What happens | Test |
|---|---|---|
| Approval link clicked twice / approve then reject | First click wins; later clicks get HTTP 409 "already decided". Balance deducted once. | `test_double_click_is_idempotent` |
| Email scanner opens the link | `GET` only shows a confirmation page; nothing changes until the confirm button `POST`s. | `test_get_on_link_never_changes_anything` |
| Tampered or expired link | Signature/expiry check fails → "Link not valid" page. | `test_tampered_and_expired_links` |
| Email link and in-app button both used | Conditional "claim" write lets only one through. | `test_in_app_approval_resumes_workflow` |
| Wrong person approves in the app | 403: only the employee's manager at the manager step, only HR Admins at the HR step. | `test_in_app_approval_resumes_workflow` |
| Self-approval | Refused; an HR Admin's own long leave needs a different HR Admin, otherwise rejected as "no approver". | `test_hr_admin_cannot_approve_own_long_leave` |
| Weekend-only dates, end before start, unknown type, crossing years, reason too long | HTTP 400, nothing saved. | `test_weekend_only_request_rejected_as_bad_input`, `test_bad_input` |
| Notice period / backdating / too many consecutive days | Auto-rejected with a specific reason. | `test_notice_period`, `test_backdated_sick_leave_allowed_within_limit`, `test_max_consecutive_days` |
| Step Functions can't start | Request marked `AUTO_REJECTED` (system error), HTTP 500, dates freed. | `test_workflow_start_failure_is_reported` |
| SES can't deliver (unverified address) | Logged; the workflow continues. | `test_email_failure_does_not_break_submission` |
| HR changes quota / threshold / adds a type | Takes effect on the next request, no deploy. | `test_hr_can_change_rules_in_config`, `test_new_leave_type_added_by_hr_works_immediately` |
| Weekly job runs every week | Carry-forward credited once per year only. | `test_weekly_job_carries_forward_once` |

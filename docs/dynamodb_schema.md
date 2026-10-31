# DynamoDB schema

All tables use on-demand billing. Table names are prefixed with the stack name
(e.g. `leave-mgmt-leave_requests`). The authoritative definition is `template.yaml`.

## leave_requests
| Key | Attribute | Example |
|---|---|---|
| PK | `employee_id` | `emp-001` |
| SK | `request_id` | `LR-2026-11-09-3fa2c1d0` |

Other attributes: `employee_name`, `employee_email`, `manager_id`, `department`, `leave_type`,
`leave_type_name`, `start_date`, `end_date`, `year`, `days`, `reason`, `requires_hr`, `status`,
`status_created`, `pending_level`, `task_token`, `execution_arn`, `escalated`, `last_decision`,
`approved_at`, `approved_by`, `rejection_reason`, `rejection_stage`, `history` (list of
`{at, status, by, note}`), `created_at`, `updated_at`.

`status`: `PENDING_MANAGER` → `PENDING_HR` (long leave or escalation) → `APPROVED` / `REJECTED`;
or `AUTO_REJECTED` at submission.

| GSI | PK | SK | Used for |
|---|---|---|---|
| `manager-status-index` | `manager_id` | `status_created` (`PENDING_MANAGER#2026-10-31T…`) | Manager's pending approvals |
| `status-start-index` | `status` | `start_date` | HR pending list, absence calendar, reports (`start_date <= to` + filter `end_date >= from`) |

## leave_balances
| Key | Attribute | Example |
|---|---|---|
| PK | `employee_id` | `emp-001` |
| SK | `leave_type_year` | `EARNED#2026` |

Attributes: `leave_type`, `year`, `allocated`, `carried_forward`, `used`, `available`,
`requires_balance`, `version`, `carry_forward_done`, `carried_to_next_year`, `created_at`,
`updated_at`. One record per leave type per year.

## leave_config
| Key | Attribute |
|---|---|
| PK | `leave_type` |

Items: one per leave type (`SICK`, `CASUAL`, `EARNED`, `UNPAID`, …) with the rule fields
described in [balance_logic.md](balance_logic.md); `SYSTEM#SETTINGS`
(`hr_approval_threshold_days`); `SYSTEM#HOLIDAYS#<year>` (`dates` list).

## employees
| Key | Attribute |
|---|---|
| PK | `employee_id` |

Attributes: `name`, `email`, `manager_id`, `role` (`EMPLOYEE`, `MANAGER`, `HR_ADMIN`),
`department`. The Cognito user carries the same ID in `custom:employee_id`.

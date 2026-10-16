# Scrappy-backend

## Billing cancellation requests

`POST /api/v1/bills/{id}/request-cancellation/` accepts the current `version`
and a nonblank `reason` of at most 500 characters, with an `Idempotency-Key`
header. Existing billing permissions and record scope apply. Pickup bill
corrections remain unavailable.

Bill responses include `cancellation_requests`, sourced from immutable audit
history, with request ID, timestamp, actor ID, reason and pending status.
Requests leave the bill effective and preserve its issued values, payments,
earnings and stock. Further settlement does not clear the pending request.
Only one request is permitted while approval/rejection policy is undefined.

Cancellation approval/rejection, corrections, reversals and refunds remain
disabled. B2B drafts remain available, but B2B issue/payment cannot be enabled
by adding a policy record alone; an explicitly defined workflow is required.

## Cash handover snapshots

Cash counts cover the exact set of eligible uncounted routes for a driver.
The server locks the driver's cash account and saves a cutoff and expected
amount from the cash ledger, including carried balances and prior handovers.
A mismatch between the ledger and balance projection blocks counting.
One handover links all covered routes and posts one cash deduction.

Saved expected/received amounts, difference, note, counter, driver and cutoff
are immutable. Future cash movements do not change them. Counts remain
awaiting supervisor; verification and correction commands remain unavailable.
Migration `0006_handover_snapshot_guard` installs the database protection;
run `python manage.py migrate` when updating an existing installation.

Warehouse cash counts require every covered route to belong to the manager's
assigned warehouse, have state `returned`, a return timestamp no later than
the count cutoff, and a nonblank eligibility reference. Any active route
blocks counting. The configured route-eligibility policy is still required;
the count command cannot create return evidence.

`received` must be explicitly supplied as a nonnegative amount with at most
two decimals. Zero is valid; missing or blank input is not. A shortage or
excess requires a nonblank text note of at most 1,000 characters. Matching
counts may omit the note and still await supervisor review.

Cash reviews use the driver's account version. Floats, cash payments and
handover postings increment it. Route creation, deletion, reassignment,
warehouse changes and return-evidence changes also lock and increment that
version through migration `0007_route_cash_review_guard`. Even a route that
leaves and returns between review and save invalidates the old review.
Refresh the cash review after a conflict; retry uncertain submissions with
their original idempotency key. Rolled-back activity does not invalidate it.
This protection does not grant new route lifecycle actions.

## Cash settlement and history reads

`GET /api/v1/handovers/` and `/api/v1/handovers/{id}/` expose saved count
timestamps, expected/received/difference, notes, settlement state and included
routes. Registers support driver, franchise, state and date filters. Summary
totals cover the full filtered result and count each handover once, regardless
of its route count. Dates use saved-count time in the application's
Asia/Kolkata timezone; these historical totals are not current cash balances.

Load responses include `handover`, `included_in_handover` and
`settlement_state`. Filter loads with `settlement_state=on_route`,
`count_pending`, `awaiting_supervisor`, `verified` or `disputed`.
`count_pending` means no saved handover, not permission to count.
`GET /api/v1/driver-cash/` supplies current balances and versions plus
`count_available` and `count_blocked_reason`; the save command always
rechecks eligibility and ledger consistency.

Warehouse handover history is restricted to its assigned warehouse; drivers
see only their own counts. Verified/disputed history can be read, but no
verification, dispute or reversal command is enabled. Every new saved count,
including a matching count, awaits supervisor review.

## Lead import preview

HQ preview accepts UTF-8 CSV (including BOM) and XLSX files with a `Leads`
sheet and exactly these columns, in order:

`customer_name, phone, address, area, upi_id, franchise_code, driver_email, pickup_date, pickup_time, items, latitude, longitude`

It previews at most 5,000 rows and reports row numbers and errors without
creating leads. CSV quoting and phone strings are preserved. XLSX native date
and time cells are normalized alongside the documented `DD-MM-YYYY` and
`hh:mm AM/PM` text forms. Blank time defaults to 09:00. Dates before today,
unpaired or out-of-range coordinates, formula-like cells, oversized cells,
unknown scrap franchises, and cross-franchise/inactive drivers are rejected
per row. Coordinates are optional and zero is valid.

Preview never activates import posting. Whole-file commit and duplicate-file
behavior still require the recorded `lead_import` policy; invalid rows cannot
be committed.

Import commit locks the batch and its file digest in one database transaction.
Each created row records `status`, `message`, and the resulting `lead` ID in
the batch result. A retry of the same committed batch returns those outcomes;
a repeated file submitted as a separate batch is rejected. If a driver,
franchise, date, or other eligibility check changes after preview, the whole
commit rolls back and the batch remains uncommitted. Non-creator users cannot
read or commit another user's batch unless their role has network import
authority.

## Lead assignment and scheduling

HQ Manager can assign or reassign eligible active Delivery Staff and
Collectors across the network. App Franchise Manager acts as the scrap
franchise supervisor and can assign only active drivers in the same franchise.
Warehouse Manager and collectors cannot assign leads.

Reschedule and cancel commands are limited to `scheduled` or `unassigned`
leads with no `started_at` timestamp. Rescheduling requires a current version,
an ISO date that is today or later, and a 24-hour `HH:MM` time. Cancellation
retains the original lead, customer, franchise, and assignment history while
marking the lead cancelled. Started, completed, failed, or cancelled leads
cannot be reopened through these commands.

Assignment and pickup start lock the lead in one transaction and recheck its
version, state, active franchise, and driver scope. A race produces one
serialized winner; the losing assignment or start cannot rewrite ownership or
create a pickup bill.

## Pickup start, resume, and billing lock

An assigned active Delivery Staff member or Collector starts a lead through
the pickup command. The lead moves to `weighing` and gets one shared draft
bill; repeating Start/Continue resumes that bill instead of creating another.
Unassigned, foreign, inactive, or out-of-scope drivers receive no usable
pickup record.

Pickup bill lines must contain active assigned scrap products measured in kg
with positive quantities. Empty, zero, malformed, duplicate, unavailable, or
non-kg lines are rejected without changing the draft. The server resolves and
saves rates in the draft, then re-resolves them during issue; a rate or product
assignment change requires a fresh review. Issuing is still subject to the
configured billing policy and creates one immutable numbered purchase bill.

The lead version and bill version are required for mutations. Start and issue
are serialized with the lead/bill locks, so retries and concurrent starts
produce one draft and one invoice. Customer sharing/agreement and payment stay
separate later steps; starting or saving weights never posts payment or stock.

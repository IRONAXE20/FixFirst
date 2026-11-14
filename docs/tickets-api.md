# FixFirst Tickets API (for RentBasket's ticketing system)

Every ticket FixFirst raises (technician visit, safety, handoff, fixed on the call) can be read as JSON and copied
into RentBasket's own system. The JSON is built from the live ticket, so a second appliance added later, new
photos or a status change made on /ops show up on the next read.

**Base URL:** `https://fixfirst-zgo0.onrender.com`
**Auth:** header `X-API-Key: <key>` on every request (the key is shared separately).

## Endpoints

| Request | Returns |
|---|---|
| `GET /api/v1/tickets/{ticket_id}` | One ticket, e.g. `/api/v1/tickets/FF-1035` |
| `GET /api/v1/tickets?updated_since=2026-11-14T09:30:00Z` | Tickets created or changed after that time, oldest first: `{"tickets": [...], "count": n, "next_updated_since": "..."}` |

Optional: `status=TICKET_RAISED` to filter, `limit` (default 100, max 500).

**Polling:** call the list every 1–5 minutes and pass `next_updated_since` from the last response as the next
`updated_since`. A ticket appears again whenever it changes; upsert it by `ticket_id`.

Errors: `401` wrong or missing key, `404` unknown ticket, `400` bad `updated_since`.

## Fields (schema `fixfirst.ticket.v1`)

Fields are only ever added, never renamed or removed. A full example is in `ticket-api-sample.json`.

| Field | Meaning |
|---|---|
| `ticket_id` | FixFirst's ticket number (`FF-1035`); one open ticket per customer, appliances are lines on it |
| `status` | `TICKET_RAISED`, `URGENT`, `PARTIALLY_RESOLVED` (some fixed on the call, some need a visit), `RESOLVED_REMOTE` (fixed on the call), `SELF_FIX_IN_PROGRESS`, `SCHEDULED`, `IN_PROGRESS`, `FIXED`, `REPLACED`, `AUDIT_REVIEW`, `REOPENED`, `CLOSED` |
| `priority` / `category` | `normal` or `urgent` / `technician`, `safety`, `handoff`, `system`, `selffix` |
| `high_alert` | The customer called again about the same appliance (within 30 days of a fix, or while a visit was pending) |
| `audit_required` | Possible misuse (dropped, damaged): for the Auditing team. Never shown to the customer |
| `sla_hours` / `visit_agree_by` | Agree a visit time within this many hours / by this time |
| `customer` | `id`, `name`, `phone`, `area`, `address` (null when the caller wasn't found) |
| `appliances[]` | One line per appliance: `appliance`, `brand`, `rental_id`, `kb_issue` (RentBasket KB code), `result`, `likely_cause`, `technician_note`, `error_code`, `steps_tried[]` (each step and the customer's answer) |
| `appliances[].result` | `booked` (visit needed), `urgent` (urgent visit), `partial` (self-fix helped partly, visit needed), `self_fix_in_progress`, `self_fixed` (fixed on the call) |
| `problem`, `likely_cause`, `technician_note`, `error_code`, `hazard`, `caller_said` | Summary of the first appliance; `hazard` is set for safety tickets (gas, electrical, water near power...) |
| `notes`, `customer_notes` | Notes added during the call / written by the customer on their WhatsApp link |
| `photos`, `photo_upload_link` | Photo URLs the customer sent / the link they use to send more |
| `call` | `call_id`, `recording_url`, `ops_url` (transcript on the FixFirst dashboard) |
| Times | ISO 8601, UTC (`...Z`) |

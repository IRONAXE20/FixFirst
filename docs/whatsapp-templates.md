# WhatsApp templates to submit (RentBasket Customer Service Ecosystem, 29 Oct 2026)

Submit these in **WhatsApp Manager → Message templates → Create template**, on RentBasket's WhatsApp Business
Account (2070784403431954), language **English (India)** (`en_IN`). The names must match exactly: FixFirst sends
them by name. Variables are numbered `{{1}}`, `{{2}}`… in Meta's editor; the names below are what FixFirst
fills in. Meta needs a sample value for every variable.

Once **all** are approved, set `WHATSAPP_TEMPLATES=ecosystem` on Render (and in `.env`). Until then FixFirst
keeps sending the two approved v1 templates (`fixfirst_customer_update`, `fixfirst_team_ticket`), and an
unregistered caller can't get an OTP, so they go to a person instead.

The team's message (`fixfirst_team_ticket`) stays as it is.

---

## T1 · `technician_booked` · Utility

Sent when a ticket is raised (TICKET_RAISED or REOPENED).

**Body**
```
Hi {{1}}, Welcome to Rentbasket!
A technician is booked for your {{2}} (ticket {{3}}).
Our service team will call you to fix a visit time from this number : "{{4}}".

A photo of the problem or the display helps the technician come prepared: {{5}}

We're here to ensure a smooth and worry-free experience. Let us know if you need help!
```

| Variable | Filled with | Sample |
|---|---|---|
| `{{1}}` | name | Hardik |
| `{{2}}` | product | Split AC 1.5 ton |
| `{{3}}` | ticket_id | FF-1009 |
| `{{4}}` | care_number | +91 9958187021 |
| `{{5}}` | photo_link (the customer's link, kept in the body as a fallback) | https://fixfirst-zgo0.onrender.com/u/ahRyryuuSg3GOyBB |

**Button:** Call to action → Visit website → type **Dynamic**
- Text: `Upload photo or details`
- URL: `https://fixfirst-zgo0.onrender.com/u/{{1}}` (sample suffix: `ahRyryuuSg3GOyBB`)

---

## T2 · `selffix_steps_<issue>` · Utility (four templates)

Sent when the caller isn't near the product, or wants to finish later, or for FR-04 (12 hours). One template
per self-fix issue because Meta allows no line breaks inside a variable. Each has one variable, `{{1}}` = name
(sample: Hardik), and two **Quick reply** buttons: `Yes, fixed` and `No, still a problem`.

### `selffix_steps_ac04`
```
Hi {{1}}, here are the steps we discussed for your AC (weak airflow):
1. Switch off the AC from the remote and the wall switch.
2. Open the front panel and slide out the filters.
3. Wash them with plain water and let them dry fully.
4. Put the filters back, close the panel, and switch the AC on.
Did it work? Tap a button below.
```

### `selffix_steps_fr04`
```
Hi {{1}}, here are the steps we discussed for your fridge (water from the front):
1. Switch off the fridge at the wall.
2. Move the food out and keep the fridge door open.
3. Switch on a fan facing the fridge.
4. Leave it like this for 12 hours.
5. Wipe it dry, close the door, and switch the fridge on.
Did it work? Tap a button below.
```

### `selffix_steps_wm01`
```
Hi {{1}}, here are the steps we discussed for your washing machine (water not coming in):
1. Switch off the machine and close the water tap.
2. Unscrew the inlet pipe from the back of the machine.
3. Pull out the small mesh filter inside the inlet and clean it under running water.
4. Fit the filter and pipe back, open the tap, and start the machine.
Did it work? Tap a button below.
```

### `selffix_steps_mw04`
```
Hi {{1}}, here are the steps we discussed for your microwave (bad smell):
1. Switch off and unplug the microwave.
2. Wipe the inside walls, door and turntable with a damp cloth.
3. Optional: heat a bowl of water with lemon for 3 minutes, then wipe again.
Did it work? Tap a button below.
```

---

## T4 · `urgent_safety` · Utility

Sent when the safety gate raises an URGENT ticket.

```
Hi {{1}}, for your safety please keep the {{2}} switched off at the wall and do not touch it.
We have raised an URGENT ticket ({{3}}). Our team will call you shortly from {{4}}.
```

| Variable | Filled with | Sample |
|---|---|---|
| `{{1}}` | name | Hardik |
| `{{2}}` | product | microwave |
| `{{3}}` | ticket_id | FF-1010 |
| `{{4}}` | care_number | +91 9958187021 |

---

## T9 · `followup_check` · Utility

Sent once, 24 hours after RESOLVED_REMOTE (fixed on the call), and 24 hours after the team marks a ticket
FIXED or REPLACED on the ops page. Also the reminder after WhatsApp self-fix steps (T2).

```
Hi {{1}}, just checking: is your {{2}} working fine after ticket {{3}}?
```

| Variable | Filled with | Sample |
|---|---|---|
| `{{1}}` | name | Hardik |
| `{{2}}` | product | fridge |
| `{{3}}` | ticket_id | FF-1009 |

**Buttons (Quick reply):** `Yes, all good` · `No, still a problem`

(The 48-hour call when there's no reply is not built yet: it needs an outbound telephony number on Bolna.)

---

## T10a · `otp_verify` · **Authentication**

Sent to the account holder's registered number when a caller rings from a number that isn't on any deal and
names a roommate's or family member's number. The caller reads the code back on the call.

In Meta's Authentication template form choose **Copy code**, and these options:
- Add security recommendation: on ("For your security, do not share this code.")
- Code expiration: **10 minutes**
- Button text: `Copy code`

Meta writes the body itself ("{{1}} is your verification code."); FixFirst fills `{{1}}` with the 6-digit
code and the copy-code button with the same code. RentBasket's preferred wording from the ecosystem document,
"{{otp}} is your Rentbasket verification code. Share it only to link a new number to your account. It expires
in 10 minutes.", is close to what Meta generates; Authentication templates can't use custom text.

---

## Not templates (session messages)

These are free-form inside an open 24-hour window and are sent by the WhatsApp bot (RentBasket's side), not by
the voice agent:
- **T3 selffix_resolved:** "Great, {{name}}! Glad your {{product}} is working again. We have closed this request
  ({{ticket_id}}). We will check in with you tomorrow."
- **T10b number_linked:** "Thanks! {{new_number}} is now linked to the Rentbasket account of {{account_holder}}.
  You can raise service requests from this number."

T5 visit_confirmed, T6 technician_on_the_way, T7 replacement_scheduled and T8 task_completed_feedback are sent
by the service team's tools, not by FixFirst.

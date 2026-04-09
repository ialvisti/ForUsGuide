# DevRev AI Agent — Request Prompt

This is the request (user) prompt template sent to the DevRev AI Agent for each ticket. Variables enclosed in `{curly_braces}` are injected at runtime by the n8n orchestrator.

---

## Template

```
TICKET CONTEXT:
Ticket ID: {ticket_id}
Participant: {participant_first_name} {participant_last_name}
Email: {participant_email}
Recordkeeper: {record_keeper}
Plan Type: {plan_type}

ORIGINAL PARTICIPANT MESSAGE:
{original_message}

COLLECTED PARTICIPANT DATA:
{collected_participant_data}

COLLECTED PLAN DATA:
{collected_plan_data}

NUMBER OF INQUIRIES: {inquiry_count}

═══════════════════════════════════════════════════════════════════
INQUIRY RESPONSES FROM KNOWLEDGE BASE
═══════════════════════════════════════════════════════════════════

{inquiry_responses_block}

═══════════════════════════════════════════════════════════════════
INSTRUCTIONS
═══════════════════════════════════════════════════════════════════

Compose the final participant-facing reply for this ticket.

Rules:
1. Address {participant_first_name} by name in the greeting.
2. Synthesize ALL {inquiry_count} inquiry response(s) into ONE cohesive, non-repetitive message.
3. Follow the outcome-driven structure from your system instructions for each inquiry's outcome.
4. Surface every warning near its relevant context. Do not bury warnings at the end.
5. Respect every guardrail listed in the responses — do not say anything they prohibit.
6. If any inquiry requires escalation, include a note that a specialist will follow up.
7. If any inquiry is blocked_missing_data, present the questions clearly so the participant knows exactly what to reply with.
8. Determine the appropriate ticket action based on the combined outcomes.

Return ONLY the JSON object matching the output schema, no additional text.
```

---

## Inquiry Response Block Format

Each inquiry in `{inquiry_responses_block}` is rendered by n8n as:

```
───────────────────────────────────────────────────────────────────
INQUIRY {n} of {inquiry_count}
───────────────────────────────────────────────────────────────────
Inquiry: {inquiry_text}
Topic: {topic}

OUTCOME: {outcome}
REASON: {outcome_reason}

OPENING:
{opening}

KEY POINTS:
- {key_point_1}
- {key_point_2}
- ...

STEPS:
1. {step_1_action} — {step_1_detail}
2. {step_2_action} — {step_2_detail}
...

WARNINGS:
- {warning_1}
- {warning_2}
...

QUESTIONS TO ASK (if any):
- {question_1} (Reason: {why_1})
- {question_2} (Reason: {why_2})
...

ESCALATION NEEDED: {yes_or_no}
ESCALATION REASON: {escalation_reason}

GUARDRAILS APPLIED:
- {guardrail_1}
- {guardrail_2}
...

DATA GAPS: {data_gaps_or_none}
COVERAGE GAPS: {coverage_gaps_or_none}
```

---

## Variable Reference

| Variable | Source | Description |
|----------|--------|-------------|
| `{ticket_id}` | DevRev ticket | Unique ticket identifier |
| `{participant_first_name}` | DevRev contact | Participant's first name |
| `{participant_last_name}` | DevRev contact | Participant's last name |
| `{participant_email}` | DevRev contact | Participant's email address |
| `{record_keeper}` | n8n detection or ticket metadata | Recordkeeper name (e.g., "LT Trust") or "Not specified" |
| `{plan_type}` | n8n detection or ticket metadata | Plan type (e.g., "401(k)") |
| `{original_message}` | DevRev ticket body | The participant's original message verbatim |
| `{collected_participant_data}` | ForUsBots via n8n | Formatted participant profile data (name, status, balance, dates, etc.) |
| `{collected_plan_data}` | ForUsBots via n8n | Formatted plan profile data (plan status, fees, blackout, etc.) |
| `{inquiry_count}` | n8n | Total number of detected inquiries in the ticket |
| `{inquiry_responses_block}` | n8n (from KB RAG API responses) | All inquiry responses rendered in the block format above |

---

## Example — Single Inquiry (can_proceed)

```
TICKET CONTEXT:
Ticket ID: TKT-2026-04821
Participant: Maria Rodriguez
Email: maria.rodriguez@example.com
Recordkeeper: LT Trust
Plan Type: 401(k)

ORIGINAL PARTICIPANT MESSAGE:
Hi, I left my job last month and I'd like to roll over my 401(k) to my Fidelity IRA. Can you help me with that?

COLLECTED PARTICIPANT DATA:
  - first_name: Maria
  - last_name: Rodriguez
  - employment_status: Terminated
  - termination_date: 2026-03-01
  - current_balance: $12,450.00
  - vested_balance: $12,450.00
  - mfa_enrolled: true

COLLECTED PLAN DATA:
  - plan_status: Active
  - blackout_period: false
  - distribution_fee: $75
  - wire_fee: $35

NUMBER OF INQUIRIES: 1

═══════════════════════════════════════════════════════════════════
INQUIRY RESPONSES FROM KNOWLEDGE BASE
═══════════════════════════════════════════════════════════════════

───────────────────────────────────────────────────────────────────
INQUIRY 1 of 1
───────────────────────────────────────────────────────────────────
Inquiry: Participant wants to roll over 401(k) to Fidelity IRA
Topic: termination_distribution_request

OUTCOME: can_proceed
REASON: Maria is terminated (2026-03-01), has a vested balance of $12,450.00, MFA is enrolled, and the plan is active with no blackout. She meets all eligibility requirements for a termination rollover.

OPENING:
Maria, since you left your employer on March 1, 2026, and your full vested balance of $12,450.00 is eligible for distribution, you can proceed with a rollover to your Fidelity IRA.

KEY POINTS:
- Your full vested balance of $12,450.00 is eligible for rollover.
- A $75 distribution fee will be deducted from the rollover amount.
- If you choose wire transfer, an additional $35 non-refundable wire fee applies.
- Direct rollovers to an IRA are not subject to the 20% mandatory federal tax withholding.
- Processing typically takes 5-7 business days after submission.

STEPS:
1. Log in to the ForUsAll portal at https://account.forusall.com/login — Use a computer or laptop with Chrome or Edge for the best experience.
2. Navigate to Loans & Distributions from the dashboard — Select "Separation of Service" as the distribution reason. If the portal shows "Default" instead, select it — it functions the same way.
3. Choose "Full Rollover" as the distribution type — Select your preferred delivery method (check or wire).
4. Enter your Fidelity IRA account details — You will need the account number, Fidelity's mailing address (for checks) or wire routing details (for wire transfer). Double-check all information to avoid rejections.
5. Review and submit the request — You will receive a confirmation email.

WARNINGS:
- The $75 distribution fee is non-refundable.
- Wire fees ($35) are non-refundable if wire transfer is selected.
- If wire instructions are incorrect, the wire may be returned and converted to a mailed check sent to your address on file.
- Overnight checks cannot be delivered to P.O. boxes; a physical street address is required.

QUESTIONS TO ASK: None

ESCALATION NEEDED: No
ESCALATION REASON: N/A

GUARDRAILS APPLIED:
- Did not guarantee an exact delivery date.
- Did not state that wire fees can be refunded.
- Did not mention unvested funds as distributable.

DATA GAPS: None
COVERAGE GAPS: None

═══════════════════════════════════════════════════════════════════
INSTRUCTIONS
═══════════════════════════════════════════════════════════════════

Compose the final participant-facing reply for this ticket.

Rules:
1. Address Maria by name in the greeting.
2. Synthesize ALL 1 inquiry response(s) into ONE cohesive, non-repetitive message.
3. Follow the outcome-driven structure from your system instructions for each inquiry's outcome.
4. Surface every warning near its relevant context. Do not bury warnings at the end.
5. Respect every guardrail listed in the responses — do not say anything they prohibit.
6. If any inquiry requires escalation, include a note that a specialist will follow up.
7. If any inquiry is blocked_missing_data, present the questions clearly so the participant knows exactly what to reply with.
8. Determine the appropriate ticket action based on the combined outcomes.

Return ONLY the JSON object matching the output schema, no additional text.
```

---

## Example — Multiple Inquiries (mixed outcomes)

```
TICKET CONTEXT:
Ticket ID: TKT-2026-05133
Participant: James Chen
Email: james.chen@example.com
Recordkeeper: LT Trust
Plan Type: 401(k)

ORIGINAL PARTICIPANT MESSAGE:
I just left my company and I want to cash out my 401(k). Also, I took a hardship withdrawal last year and I'm not sure if there's still a balance I owe. Can you check?

COLLECTED PARTICIPANT DATA:
  - first_name: James
  - last_name: Chen
  - employment_status: Terminated
  - termination_date: 2026-03-15
  - current_balance: $8,200.00
  - vested_balance: $6,150.00
  - mfa_enrolled: false
  - outstanding_loan_balance: $0.00

COLLECTED PLAN DATA:
  - plan_status: Active
  - blackout_period: false
  - distribution_fee: $75

NUMBER OF INQUIRIES: 2

═══════════════════════════════════════════════════════════════════
INQUIRY RESPONSES FROM KNOWLEDGE BASE
═══════════════════════════════════════════════════════════════════

───────────────────────────────────────────────────────────────────
INQUIRY 1 of 2
───────────────────────────────────────────────────────────────────
Inquiry: Participant wants to cash out 401(k) after leaving company
Topic: termination_distribution_request

OUTCOME: can_proceed
REASON: James is terminated (2026-03-15) with a vested balance of $6,150.00. The plan is active with no blackout. However, MFA is not enrolled, so he may need to use the RightSignature form as an alternative to the portal.

OPENING:
James, since you have left your employer and have a vested balance of $6,150.00, you are eligible to request a cash withdrawal from your 401(k).

KEY POINTS:
- Only your vested balance of $6,150.00 is eligible for distribution (not the full $8,200.00).
- A $75 non-refundable distribution fee applies.
- 20% mandatory federal tax withholding applies to cash distributions. State withholding may also apply.
- Since MFA is not enrolled, you may need to request a RightSignature electronic form instead of using the portal directly.

STEPS:
1. Contact ForUsAll Support to request a RightSignature distribution form — Since your MFA is not set up, portal access may not be available. Support can send you an electronic form.
2. Complete and sign the RightSignature form — Select "Lump Sum Cash" as the distribution type and provide your preferred delivery method.
3. Submit the signed form — ForUsAll will process the request.

WARNINGS:
- 20% federal tax withholding is mandatory on cash distributions.
- The cash distribution is taxable income for 2026.
- Only $6,150.00 (vested balance) can be distributed, not $8,200.00.
- The $75 distribution fee is non-refundable.

QUESTIONS TO ASK: None

ESCALATION NEEDED: No
ESCALATION REASON: N/A

GUARDRAILS APPLIED:
- Did not guarantee delivery dates.
- Did not suggest unvested funds are distributable.

DATA GAPS: None
COVERAGE GAPS: None

───────────────────────────────────────────────────────────────────
INQUIRY 2 of 2
───────────────────────────────────────────────────────────────────
Inquiry: Participant wants to check if there's a remaining balance from a past hardship withdrawal
Topic: hardship_withdrawal

OUTCOME: blocked_missing_data
REASON: The collected data shows no outstanding loan balance, but a hardship withdrawal is not a loan — it is a distribution. We need to confirm whether the participant is asking about a hardship distribution repayment (which does not exist for hardships) or whether they are confusing a hardship with a loan. Clarification is needed.

OPENING:
Regarding your question about a previous hardship withdrawal, we need a bit more information to give you a complete answer.

KEY POINTS:
- Hardship withdrawals are permanent distributions and are not repaid to the plan (unlike loans).
- If you took a hardship withdrawal last year, there would be no remaining balance to owe on it.
- You may be thinking of a 401(k) loan, which does require repayment. Your records show no outstanding loan balance.

STEPS: None

WARNINGS: None

QUESTIONS TO ASK:
- Did you take a hardship withdrawal or a 401(k) loan last year? (Reason: Hardship withdrawals do not require repayment, but loans do. This determines whether there is a balance to track.)
- Do you have any documentation from the transaction, such as a confirmation email or a 1099-R? (Reason: This will help us identify the exact transaction type and amount.)

ESCALATION NEEDED: No
ESCALATION REASON: N/A

GUARDRAILS APPLIED:
- Did not confirm or deny a balance without verifying the transaction type.

DATA GAPS:
- Transaction type (hardship vs. loan) for the prior year withdrawal
COVERAGE GAPS: None

═══════════════════════════════════════════════════════════════════
INSTRUCTIONS
═══════════════════════════════════════════════════════════════════

Compose the final participant-facing reply for this ticket.

Rules:
1. Address James by name in the greeting.
2. Synthesize ALL 2 inquiry response(s) into ONE cohesive, non-repetitive message.
3. Follow the outcome-driven structure from your system instructions for each inquiry's outcome.
4. Surface every warning near its relevant context. Do not bury warnings at the end.
5. Respect every guardrail listed in the responses — do not say anything they prohibit.
6. If any inquiry requires escalation, include a note that a specialist will follow up.
7. If any inquiry is blocked_missing_data, present the questions clearly so the participant knows exactly what to reply with.
8. Determine the appropriate ticket action based on the combined outcomes.

Return ONLY the JSON object matching the output schema, no additional text.
```

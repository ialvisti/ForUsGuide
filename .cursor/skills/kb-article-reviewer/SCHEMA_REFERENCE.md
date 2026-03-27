# kb_article_v2 Schema Reference

Full expected JSON structure for knowledge base articles.

## Top-Level

```json
{
  "metadata": { ... },
  "details": { ... }
}
```

Both keys are required. No other top-level keys are allowed.

---

## metadata

All keys below are **required**. Nullable keys accept `null` but must be present.

| Key | Type | Nullable | Notes |
|-----|------|----------|-------|
| `article_id` | string | No | snake_case identifier, unique per article |
| `title` | string | No | Human-readable title. Prefix with recordkeeper abbreviation if `record_keeper` is set |
| `description` | string | No | 1-3 sentence summary of full article scope |
| `topic` | string | No | Primary topic category (snake_case) |
| `subtopics` | string[] | No | Specific content areas covered (snake_case) |
| `audience` | string | No | Expected: `"Internal AI Support Agent"` |
| `record_keeper` | string | Yes | e.g., `"LT Trust"`, `null` for global articles |
| `plan_type` | string | No | Expected: `"401(k)"` |
| `scope` | string | No | `"global"` or `"recordkeeper-specific"` |
| `tags` | string[] | No | Must match approved tag names exactly |
| `language` | string | No | BCP-47 format, expected: `"en-US"` |
| `last_updated` | string | Yes | ISO 8601 date or `null` |
| `schema_version` | string | No | Expected: `"kb_article_v2"` |
| `transformed_at` | string | No | ISO 8601 date (YYYY-MM-DD) |
| `source_last_updated` | string | Yes | ISO 8601 date or `null` |
| `source_system` | string | No | Expected: `"DevRev"` |

### Validation rules

- If `record_keeper` is not null, `scope` must be `"recordkeeper-specific"`.
- If `scope` is `"global"`, `record_keeper` must be `null`.
- `subtopics` array must not be empty.
- `tags` array must not be empty. Every value must match a tag from the approved list.

---

## details

### Required sections

| Section | Type | Description |
|---------|------|-------------|
| `critical_flags` | object | Portal/MFA/recordkeeper flags |
| `business_rules` | array | Categorized rule sets |
| `steps` | array | Step-by-step instructions |
| `common_issues` | array | Known issues and resolutions |
| `examples` | array | Scenario/outcome pairs |
| `additional_notes` | array | Categorized supplementary notes |
| `faq_pairs` | array | Question/answer pairs |
| `definitions` | array | Term glossary |
| `guardrails` | object | Must-not and if-unsure rules |
| `references` | object | Related articles, links, contacts |
| `required_data` | object | Data points needed to process |
| `decision_guide` | object | Outcome-based decision logic |
| `response_frames` | object | Per-outcome response templates |

### Optional sections

| Section | Type | Include when |
|---------|------|-------------|
| `fees` | array | Article involves fees/charges |

---

## details.critical_flags

```json
{
  "portal_required": true | false | "Conditional",
  "mfa_relevant": true | false | "True",
  "record_keeper_must_be": "LT Trust" | null
}
```

---

## details.business_rules

```json
[
  {
    "category": "string",
    "rules": ["string", "string"]
  }
]
```

Each category groups related rules. Typical categories: `eligibility`, `fees`, `processing`, `tax_withholding`, `delivery`, `submission_method`, `request_limits`.

---

## details.fees (optional)

```json
[
  {
    "service": "string",
    "fee": "string",
    "notes": "string"
  }
]
```

---

## details.steps

```json
[
  {
    "step_number": 1,
    "type": "participant-facing" | "internal-check",
    "visibility": "both" | "agent-only",
    "description": "string",
    "notes": "string"
  }
]
```

`step_number` must be sequential starting at 1.

---

## details.common_issues

```json
[
  {
    "issue": "string",
    "resolution": "string"
  }
]
```

---

## details.examples

```json
[
  {
    "scenario": "string",
    "outcome": "string"
  }
]
```

---

## details.additional_notes

```json
[
  {
    "category": "string",
    "notes": ["string"]
  }
]
```

---

## details.faq_pairs

```json
[
  {
    "question": "string",
    "answer": "string"
  }
]
```

---

## details.definitions

```json
[
  {
    "term": "string",
    "definition": "string"
  }
]
```

---

## details.guardrails

```json
{
  "must_not": ["string"],
  "must_do_if_unsure": ["string"]
}
```

---

## details.references

```json
{
  "participant_portal": "string" | null,
  "internal_articles": ["string"],
  "external_links": ["string"],
  "contact": {
    "email": "string",
    "phone": "string",
    "support_hours": "string"
  }
}
```

---

## details.required_data

```json
{
  "must_have": [
    {
      "data_point": "string",
      "meaning": "string",
      "example_values": ["any"],
      "why_needed": "string",
      "source_note": "string",
      "source_type": "participant_profile" | "plan_profile" | "message_text" | "agent_input" | "unknown"
    }
  ],
  "nice_to_have": [
    {
      "data_point": "string",
      "meaning": "string",
      "example_values": ["any"],
      "why_needed": "string",
      "source_note": "string",
      "source_type": "participant_profile" | "plan_profile" | "message_text" | "agent_input" | "unknown"
    }
  ],
  "if_missing": [
    {
      "missing_data_point": "string",
      "ask_participant": "string" | null,
      "agent_note": "string"
    }
  ],
  "disambiguation_notes": ["string"]
}
```

---

## details.decision_guide

```json
{
  "supported_outcomes": ["string"],
  "eligibility_requirements": ["string"],
  "blocking_conditions": ["string"],
  "missing_data_conditions": [
    {
      "condition": "string",
      "missing_data_point": "string",
      "resulting_outcome": "string",
      "ask_participant": "string"
    }
  ],
  "allowed_conclusions": ["string"],
  "not_allowed_conclusions": ["string"]
}
```

Every value in `supported_outcomes` must have a matching key in `response_frames`.

---

## details.response_frames

One key per supported outcome:

```json
{
  "can_proceed": {
    "participant_message_components": ["string"],
    "next_steps": ["string"],
    "warnings": ["string"],
    "questions_to_ask": ["string"],
    "what_not_to_say": ["string"]
  },
  "blocked_missing_data": { ... },
  "blocked_not_eligible": { ... },
  "ambiguous_plan_rules": { ... }
}
```

All five sub-keys are required within each outcome frame.

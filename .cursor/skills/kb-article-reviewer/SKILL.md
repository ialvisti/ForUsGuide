---
name: kb-article-reviewer
description: Review and audit knowledge base article JSON files for structural compliance, content consistency, and AI-readiness. Analyze title, description, subtopics, and tags against the approved schema and real tags sheet. Use when reviewing KB articles, auditing article quality, checking JSON structure, or when the user mentions article review, article audit, or article improvements.
---

# KB Article Reviewer

Systematically review a knowledge base article JSON file against the `kb_article_v2` schema **and** against sibling articles in the knowledge base for a bigger-picture view. Produce a structured report with findings and a development plan.

## Workflow

Copy this checklist and track progress:

```
Review Progress:
- [ ] Step 1: Load and parse the article
- [ ] Step 2: Validate structure (metadata + details keys)
- [ ] Step 3: Analyze metadata quality (title, description, subtopics, tags)
- [ ] Step 4: Analyze content consistency
- [ ] Step 5: Comparative analysis against sibling articles
- [ ] Step 6: Generate report with development plan
```

---

## Step 1: Load the Article

Read the target `.json` file. If the file fails to parse, stop and report the JSON syntax error with line number.

---

## Step 2: Validate Structure

Check the article against the expected schema. For the full key-by-key reference, see [SCHEMA_REFERENCE.md](SCHEMA_REFERENCE.md).

### Metadata keys (all required)

`article_id`, `title`, `description`, `topic`, `subtopics`, `audience`, `record_keeper`, `plan_type`, `scope`, `tags`, `language`, `last_updated`, `schema_version`, `transformed_at`, `source_last_updated`, `source_system`

**Flag**: any missing key, unexpected extra key, or wrong value type.

### Details keys (expected sections)

`critical_flags`, `business_rules`, `steps`, `common_issues`, `examples`, `additional_notes`, `faq_pairs`, `definitions`, `guardrails`, `references`, `required_data`, `decision_guide`, `response_frames`

Optional: `fees` (only when the article involves fees).

**Flag**: any missing section, unexpected section, or empty array where content is expected.

### Sub-structure spot checks

| Section | Expected shape |
|---------|---------------|
| `business_rules` | `[{category: str, rules: [str]}]` |
| `steps` | `[{step_number: int, type: str, visibility: str, description: str, notes: str}]` |
| `common_issues` | `[{issue: str, resolution: str}]` |
| `examples` | `[{scenario: str, outcome: str}]` |
| `faq_pairs` | `[{question: str, answer: str}]` |
| `definitions` | `[{term: str, definition: str}]` |
| `guardrails` | `{must_not: [str], must_do_if_unsure: [str]}` |
| `references` | `{participant_portal, internal_articles: [], external_links: [], contact: {email, phone, support_hours}}` |
| `required_data` | `{must_have: [], nice_to_have: [], if_missing: [], disambiguation_notes: []}` |
| `decision_guide` | `{supported_outcomes: [], eligibility_requirements: [], blocking_conditions: [], missing_data_conditions: [], allowed_conclusions: [], not_allowed_conclusions: []}` |
| `response_frames` | One key per `supported_outcomes` entry, each with: `{participant_message_components, next_steps, warnings, questions_to_ask, what_not_to_say}` |
| `critical_flags` | `{portal_required: bool\|str, mfa_relevant: bool\|str, record_keeper_must_be: str\|null}` |

---

## Step 3: Analyze Metadata Quality

### Title

Evaluate against these criteria:
- **Prefix**: If `record_keeper` is set, the title should start with the recordkeeper abbreviation (e.g., `"LT: ..."` for LT Trust).
- **Clarity**: Title must be specific enough to distinguish the article from others in the same topic.
- **Length**: Should be descriptive but not exceed ~120 characters.
- **AI searchability**: Must contain the primary topic keywords a user or AI agent would search for.

### Description

Evaluate against these criteria:
- **Completeness**: Must summarize the full scope of the article — what it covers, who it's for, and key processes.
- **Specificity**: Must mention specific procedures, systems, or constraints (e.g., RightSignature, portal, recordkeeper).
- **No vagueness**: Phrases like "general information" or "overview of options" are red flags unless the article truly is a general overview.
- **AI searchability**: Must include keywords that help semantic search and RAG retrieval match the right queries.
- **Accuracy**: Description claims must match the actual content in `details`.

### Subtopics

Evaluate against these criteria:
- **Relevance**: Each subtopic must map to actual content in `details` (business rules, steps, FAQ, etc.).
- **Completeness**: Major content areas in `details` that are NOT represented in subtopics should be flagged.
- **Naming convention**: Should use `snake_case` for multi-word subtopics.
- **Granularity**: Should be specific enough for filtering but not so granular they become noise.
- **No duplicates**: Flag duplicate or near-duplicate subtopics.

### Tags

Validate against the approved tags list. For the full list, see [VALID_TAGS.md](VALID_TAGS.md).

Check:
- **All tags must exist** in the approved list (exact match on `Tag Name`).
- **Flag invalid tags** that don't appear in the approved list.
- **Suggest missing tags** by comparing article content against tag descriptions.
- **Relevance**: Each assigned tag should be justified by actual article content.
- **Coverage**: If the article discusses a topic that clearly maps to an approved tag, recommend adding it.

---

## Step 4: Analyze Content Consistency

### Cross-reference checks

1. **business_rules vs. steps**: Every rule category should be reflected in the step-by-step instructions. Flag rules that have no corresponding step.
2. **business_rules vs. guardrails**: Key constraints in rules should appear in `must_not` or `must_do_if_unsure`. Flag unprotected critical rules.
3. **examples vs. business_rules**: Each example scenario should be traceable to a rule. Flag examples that reference unstated rules.
4. **faq_pairs vs. details**: FAQ answers must not contradict business rules, steps, or guardrails. Flag contradictions.
5. **required_data vs. decision_guide**: Every `must_have` data point should connect to an `eligibility_requirement` or `blocking_condition`. Flag orphaned data points.
6. **decision_guide vs. response_frames**: Every `supported_outcome` must have a matching `response_frames` entry. Flag missing frames.
7. **references.internal_articles**: Check if any referenced article titles appear to reference the article itself (self-reference).
8. **definitions**: Flag terms defined but never used in the article, and terms used in rules/steps but never defined.

### Content quality checks

- **Redundancy**: Flag rules, FAQ pairs, or examples that say the same thing in different words.
- **Contradictions**: Flag any pair of statements in the article that contradict each other.
- **Completeness of examples**: Flag scenarios that end with "blocked" outcomes but don't specify what data is missing.
- **Guardrail coverage**: Critical processes (fee disclosures, eligibility checks, manual processing requirements) should have corresponding guardrails.

---

## Step 5: Comparative Analysis Against Sibling Articles

Before generating the report, scan the knowledge base for sibling articles to build a bigger picture. This step surfaces issues and opportunities that are invisible when reviewing an article in isolation.

### 5a. Discover sibling articles

1. **Same folder**: List all `.json` files in the same directory as the reviewed article.
2. **Same topic**: Among those files, identify articles that share the same `metadata.topic` value.
3. **Same record_keeper**: Identify articles targeting the same recordkeeper (or also `null`/global).
4. Read at minimum the `metadata` block of each sibling (full read only if needed for a specific check).

### 5b. Cross-article structure comparison

Compare the reviewed article's **section depth** against siblings:

| Dimension | What to compare |
|-----------|----------------|
| **Description length & detail** | Is the description significantly shorter or vaguer than siblings with similar complexity? |
| **Subtopic count & granularity** | Do siblings at similar complexity have more granular subtopics? Is the naming convention consistent across all articles? |
| **Tag coverage patterns** | Do siblings covering related topics use tags the reviewed article is missing? (e.g., sibling covers fees and uses `Taxes` tag — does the reviewed article also cover fees but lack that tag?) |
| **Section completeness** | Does the reviewed article lack sections (e.g., `fees`, `required_data`, `decision_guide`) that siblings of similar scope include? |
| **Example & FAQ depth** | Do siblings provide significantly more examples or FAQ pairs for comparable topics? |
| **Definition coverage** | Do siblings define terms that the reviewed article uses but does not define? |

### 5c. Content overlap & gap detection

1. **Overlap**: Flag significant content duplication between the reviewed article and siblings — business rules, FAQ answers, or example scenarios that are nearly identical. Note: some overlap is expected for shared processes (e.g., fee structures), but copy-pasted blocks should be flagged.
2. **Gaps visible from siblings**: If a sibling article covers a related process and includes a section the reviewed article should logically also have (e.g., tax implications, processing timelines, delivery methods), flag the gap.
3. **Contradictions across articles**: Flag business rules, fee amounts, timelines, or eligibility requirements that conflict between the reviewed article and a sibling.

### 5d. Cross-referencing opportunities

1. **Missing internal references**: If a sibling covers a topic the reviewed article mentions but doesn't deep-dive into, it should be listed in `references.internal_articles`. Flag missing cross-references.
2. **Broken references**: Check if `references.internal_articles` entries in the reviewed article actually match titles of real articles in the knowledge base. Flag references to articles that don't exist.
3. **Reciprocal references**: If the reviewed article references a sibling, does the sibling reference back? Flag one-way references where reciprocal linking would help retrieval.

### 5e. Best-practice adoption

Identify specific patterns from the strongest sibling articles that the reviewed article could adopt:

- More precise description wording
- Better subtopic naming that aids filtering
- Guardrail patterns that protect critical rules
- Example coverage patterns (e.g., "blocked" + "allowed" pairs)
- Response frame completeness

**Do not penalize the reviewed article for being different** — only flag patterns that would measurably improve AI retrieval, agent accuracy, or content consistency.

---

## Step 6: Generate Report

Produce a structured report using this template:

```markdown
# KB Article Review Report

**Article**: [title]
**File**: [file path]
**Schema Version**: [schema_version]
**Review Date**: [today]

---

## 1. Structural Compliance

### Missing Keys
- [list or "None"]

### Unexpected Keys
- [list or "None"]

### Type Violations
- [list or "None"]

### Sub-structure Issues
- [list or "None"]

**Structural Verdict**: PASS | ISSUES FOUND

---

## 2. Metadata Quality Analysis

### Title
- **Current**: "[current title]"
- **Issues**: [list issues or "None"]
- **Suggested Improvement**: "[improved title]" (if applicable)

### Description
- **Current**: "[first 100 chars]..."
- **Issues**: [list issues or "None"]
- **Suggested Improvement**: "[improved description]" (if applicable)

### Subtopics
- **Current**: [list]
- **Missing Coverage**: [topics in details not reflected in subtopics]
- **Irrelevant**: [subtopics with no matching content]
- **Suggested Additions**: [list]
- **Suggested Removals**: [list]

### Tags
- **Current**: [list]
- **Invalid Tags**: [tags not in approved list]
- **Missing Tags**: [recommended additions with justification]
- **Irrelevant Tags**: [tags not justified by content]

**Metadata Verdict**: PASS | IMPROVEMENTS RECOMMENDED

---

## 3. Content Consistency

### Cross-Reference Issues
- [numbered list of findings]

### Contradictions
- [numbered list or "None found"]

### Redundancy
- [numbered list or "None found"]

### Gaps
- [numbered list or "None found"]

**Consistency Verdict**: PASS | ISSUES FOUND

---

## 4. Comparative Analysis (vs. Sibling Articles)

**Siblings scanned**: [count] articles in [folder]
**Same-topic siblings**: [list titles]

### Structure Comparison
| Dimension | This Article | Sibling Avg/Best | Gap |
|-----------|-------------|-----------------|-----|
| Description length | X words | Y words | ... |
| Subtopic count | X | Y | ... |
| Tag count | X | Y | ... |
| Example count | X | Y | ... |
| FAQ count | X | Y | ... |
| Definition count | X | Y | ... |

### Content Overlap
- [findings or "No significant overlap found"]

### Gaps Visible from Siblings
- [findings or "No gaps identified"]

### Cross-Article Contradictions
- [findings or "None found"]

### Missing Cross-References
- [findings or "All references valid"]

### Best Practices to Adopt from Siblings
- [specific pattern from a named sibling, with rationale]

**Comparative Verdict**: ON PAR | BELOW PEERS | ABOVE PEERS

---

## 5. AI Performance Opportunities

Specific improvements that would make this article perform better for
AI retrieval and agent decision-making (informed by both internal
analysis and sibling comparison):

1. [Improvement with rationale]
2. [Improvement with rationale]
...

---

## 6. Development Plan

### Priority 1 — Must Fix (structural/correctness)
| # | Change | File Location | Rationale |
|---|--------|---------------|-----------|
| 1 | ... | metadata.X | ... |

### Priority 2 — Should Fix (consistency/coverage)
| # | Change | File Location | Rationale |
|---|--------|---------------|-----------|
| 1 | ... | details.X | ... |

### Priority 3 — Nice to Have (AI optimization)
| # | Change | File Location | Rationale |
|---|--------|---------------|-----------|
| 1 | ... | metadata.X | ... |

---

## Summary

| Category | Verdict |
|----------|---------|
| Structure | PASS / ISSUES |
| Metadata | PASS / IMPROVEMENTS |
| Consistency | PASS / ISSUES |
| Comparative | ON PAR / BELOW PEERS / ABOVE PEERS |
| **Overall** | **READY / NEEDS WORK** |

Total issues: X | Improvements suggested: Y
```

---

## Important Notes

- Back every finding with a specific quote or key path from the article.
- Never invent issues — if the article is well-structured, say so.
- The development plan must be actionable: each item should specify the exact key path and the proposed change.
- When suggesting tag additions, always reference the tag description from the approved list to justify the recommendation.
- When comparing against the tags list, use exact string matching — tags are case-sensitive and include special characters.

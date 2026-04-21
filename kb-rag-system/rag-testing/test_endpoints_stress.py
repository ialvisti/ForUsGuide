#!/usr/bin/env python3
"""
Comprehensive Stress Test Suite for Generate Response & Knowledge Question Endpoints.

Tests RAG retrieval accuracy, edge-case handling, cross-article reasoning,
boundary conditions, and adversarial queries — all grounded in real
PA/Distributions article content.

Usage:
    python test_endpoints_stress.py
    python test_endpoints_stress.py --endpoint knowledge
    python test_endpoints_stress.py --endpoint generate
    python test_endpoints_stress.py --verbose
"""

import sys
import os
import json
import time
import argparse
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from ground_truth import validate_facts

# Load .env from parent kb-rag-system directory
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY")
# When the service is private (Cloud Run IAM), set this to an identity token whose
# audience is the service URL, e.g.:
#   gcloud auth print-identity-token --audiences="https://YOUR-SERVICE-....run.app"
CLOUD_RUN_ID_TOKEN = os.getenv("CLOUD_RUN_ID_TOKEN", "").strip()

HEADERS_AUTH = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
}
HEADERS_PUBLIC = {
    "Content-Type": "application/json",
}


def _req_headers(base: dict) -> dict:
    """Merge optional Cloud Run `Authorization: Bearer` for private services."""
    h = dict(base)
    if CLOUD_RUN_ID_TOKEN:
        h["Authorization"] = f"Bearer {CLOUD_RUN_ID_TOKEN}"
    return h

TIMEOUT = float(os.getenv("STRESS_HTTP_TIMEOUT", "180"))
SLOW_RESPONSE_THRESHOLD_MS = 60_000

# ============================================================================
# Result tracking
# ============================================================================

class TestResult:
    def __init__(self, test_id: str, name: str, endpoint: str, category: str):
        self.test_id = test_id
        self.name = name
        self.endpoint = endpoint
        self.category = category
        self.passed = False
        self.response_time_ms = 0
        self.status_code: Optional[int] = None
        self.decision: Optional[str] = None
        self.confidence: Optional[float] = None
        self.confidence_note: Optional[str] = None
        self.source_articles: list = []
        self.chunks_used: int = 0
        self.error: Optional[str] = None
        self.validation_notes: list = []
        # Full request/response capture
        self.request_payload: Optional[dict] = None
        self.response_answer: Optional[str] = None
        self.response_key_points: list = []
        self.response_outcome: Optional[str] = None
        self.response_outcome_reason: Optional[str] = None
        self.response_opening: Optional[str] = None
        self.response_steps: list = []
        self.response_warnings: list = []
        self.response_questions: list = []
        self.response_guardrails: list = []
        self.response_escalation: Optional[dict] = None
        self.response_data_gaps: list = []

    def to_dict(self):
        return {
            "test_id": self.test_id,
            "name": self.name,
            "endpoint": self.endpoint,
            "category": self.category,
            "passed": self.passed,
            "response_time_ms": self.response_time_ms,
            "status_code": self.status_code,
            "decision": self.decision,
            "confidence": self.confidence,
            "confidence_note": self.confidence_note,
            "source_articles": self.source_articles,
            "chunks_used": self.chunks_used,
            "error": self.error,
            "validation_notes": self.validation_notes,
            "request_payload": self.request_payload,
            "response_answer": self.response_answer,
            "response_key_points": self.response_key_points,
            "response_outcome": self.response_outcome,
            "response_outcome_reason": self.response_outcome_reason,
            "response_opening": self.response_opening,
            "response_steps": self.response_steps,
            "response_warnings": self.response_warnings,
            "response_questions": self.response_questions,
            "response_guardrails": self.response_guardrails,
            "response_escalation": self.response_escalation,
            "response_data_gaps": self.response_data_gaps,
        }


# ============================================================================
# Helper: send requests
# ============================================================================

def call_generate_response(payload: dict) -> tuple[dict, int, float]:
    start = time.time()
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/generate-response",
        headers=_req_headers(HEADERS_AUTH),
        json=payload,
        timeout=TIMEOUT,
    )
    elapsed_ms = (time.time() - start) * 1000
    return resp.json(), resp.status_code, elapsed_ms


def call_knowledge_question(question: str) -> tuple[dict, int, float]:
    start = time.time()
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/knowledge-question",
        headers=_req_headers(HEADERS_PUBLIC),
        json={"question": question},
        timeout=TIMEOUT,
    )
    elapsed_ms = (time.time() - start) * 1000
    return resp.json(), resp.status_code, elapsed_ms


# ============================================================================
# Shared validation helpers
# ============================================================================

def validate_generate(result: TestResult, data: dict, status: int, ms: float,
                      expect_decision: Optional[str] = None,
                      expect_outcome: Optional[str] = None,
                      min_confidence: float = 0.0,
                      require_articles: bool = True):
    result.status_code = status
    result.response_time_ms = round(ms, 1)

    if ms > SLOW_RESPONSE_THRESHOLD_MS:
        result.validation_notes.append(
            f"Slow response: {ms:.0f}ms exceeds {SLOW_RESPONSE_THRESHOLD_MS}ms threshold"
        )

    if status != 200:
        result.error = f"HTTP {status}: {json.dumps(data)[:300]}"
        return

    result.decision = data.get("decision")
    result.confidence = data.get("confidence")
    result.source_articles = [
        a.get("article_title", "?") for a in data.get("source_articles", [])
    ]
    result.chunks_used = data.get("metadata", {}).get("chunks_used", 0)

    # Extract full response content
    resp = data.get("response", {})
    result.response_outcome = resp.get("outcome")
    result.response_outcome_reason = resp.get("outcome_reason")
    participant = resp.get("response_to_participant", {})
    result.response_opening = participant.get("opening")
    result.response_key_points = participant.get("key_points", [])
    result.response_steps = participant.get("steps", [])
    result.response_warnings = participant.get("warnings", [])
    result.response_questions = resp.get("questions_to_ask", [])
    result.response_guardrails = resp.get("guardrails_applied", [])
    result.response_escalation = resp.get("escalation")
    result.response_data_gaps = resp.get("data_gaps", [])

    result.passed = True

    # R1: Detect LLM timeout fallback responses
    outcome_reason = resp.get("outcome_reason", "")
    if "timed out" in outcome_reason.lower():
        result.passed = False
        result.validation_notes.append("LLM timeout: response is incomplete fallback")

    if expect_decision and result.decision != expect_decision:
        result.passed = False
        result.validation_notes.append(
            f"Expected decision '{expect_decision}', got '{result.decision}'"
        )

    if result.confidence is not None and result.confidence < min_confidence:
        result.passed = False
        result.validation_notes.append(
            f"Confidence {result.confidence:.2f} below minimum {min_confidence}"
        )

    if require_articles and not result.source_articles:
        result.passed = False
        result.validation_notes.append("No source articles returned")

    # R2: Validate expected business outcome
    if expect_outcome and result.response_outcome != expect_outcome:
        result.passed = False
        result.validation_notes.append(
            f"Expected outcome '{expect_outcome}', got '{result.response_outcome}'"
        )

    # R3: Factual accuracy checks against ground truth
    def _flatten(items):
        parts = []
        for item in items:
            if isinstance(item, dict):
                parts.extend(str(v) for v in item.values())
            else:
                parts.append(str(item))
        return " ".join(parts)

    full_text = " ".join(filter(None, [
        result.response_opening or "",
        _flatten(result.response_key_points),
        _flatten(result.response_steps),
        _flatten(result.response_warnings),
        result.response_outcome_reason or "",
    ]))
    if full_text.strip():
        gt_warnings, gt_failures = validate_facts(result.test_id, full_text)
        for w in gt_warnings:
            result.validation_notes.append(w)
        for f in gt_failures:
            result.passed = False
            result.validation_notes.append(f)


def validate_knowledge(result: TestResult, data: dict, status: int, ms: float,
                       expect_coverage: Optional[str] = None,
                       require_key_points: bool = True):
    result.status_code = status
    result.response_time_ms = round(ms, 1)

    if status != 200:
        result.error = f"HTTP {status}: {json.dumps(data)[:300]}"
        return

    result.confidence_note = data.get("confidence_note")
    result.source_articles = [
        a.get("article_title", "?") for a in data.get("source_articles", [])
    ]
    result.chunks_used = data.get("metadata", {}).get("chunks_used", 0)

    # Extract full response content
    result.response_answer = data.get("answer")
    result.response_key_points = data.get("key_points", [])

    result.passed = True

    if expect_coverage and result.confidence_note != expect_coverage:
        result.validation_notes.append(
            f"Expected coverage '{expect_coverage}', got '{result.confidence_note}'"
        )

    if require_key_points and not data.get("key_points"):
        result.passed = False
        result.validation_notes.append("No key_points returned")

    answer = data.get("answer", "")
    if len(answer) < 50:
        result.passed = False
        result.validation_notes.append(f"Answer suspiciously short ({len(answer)} chars)")

    # R3: Factual accuracy checks against ground truth
    kq_parts = []
    for item in result.response_key_points:
        kq_parts.append(str(item) if not isinstance(item, dict) else " ".join(str(v) for v in item.values()))
    full_text = " ".join(filter(None, [answer, " ".join(kq_parts)]))
    if full_text.strip():
        gt_warnings, gt_failures = validate_facts(result.test_id, full_text)
        for w in gt_warnings:
            result.validation_notes.append(w)
        for f in gt_failures:
            result.passed = False
            result.validation_notes.append(f)


# ============================================================================
# TEST DEFINITIONS — Knowledge Question endpoint (15 tests)
# ============================================================================

KNOWLEDGE_TESTS = [
    # ------------------------------------------------------------------
    # KQ-01: Force-out threshold boundaries
    # ------------------------------------------------------------------
    {
        "id": "KQ-01",
        "name": "Force-out balance thresholds and tier rules",
        "category": "boundary_precision",
        "question": (
            "Explain the exact balance thresholds for the 401(k) force-out process. "
            "What happens to a terminated participant with exactly $80, exactly $999, "
            "exactly $1,000, and exactly $7,000? Include withholding percentages."
        ),
    },
    # ------------------------------------------------------------------
    # KQ-02: EACA 90-day deadline vs auto-enrollment confusion
    # ------------------------------------------------------------------
    {
        "id": "KQ-02",
        "name": "EACA refund 90-day deadline calculation",
        "category": "deadline_precision",
        "question": (
            "How is the 90-day EACA refund deadline calculated? Does it start from "
            "the hire date, the enrollment date, or the first auto-deferral deposit date? "
            "What happens if the written request arrives on day 89 vs day 91?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-03: Cross-article RMD + Roth conversion interaction
    # ------------------------------------------------------------------
    {
        "id": "KQ-03",
        "name": "RMD and Roth conversion interaction rules",
        "category": "cross_article_reasoning",
        "question": (
            "Can a participant convert their pre-tax 401(k) balance to Roth to avoid "
            "future RMDs? What is the interaction between RMDs and Roth conversions — "
            "specifically, can the RMD amount itself be converted, and what is the 5-year "
            "rule for converted dollars?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-04: ADP/ACP refund tax treatment pre-tax vs Roth
    # ------------------------------------------------------------------
    {
        "id": "KQ-04",
        "name": "ADP/ACP refund tax treatment comparison",
        "category": "tax_complexity",
        "question": (
            "Compare the tax treatment of ADP/ACP corrective distribution refunds for "
            "pre-tax contributions versus Roth contributions. Is there a 10% early "
            "withdrawal penalty? Does the refund reduce the participant's annual "
            "contribution limit?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-05: Missed 60-day rollover — self-certification exceptions
    # ------------------------------------------------------------------
    {
        "id": "KQ-05",
        "name": "Missed 60-day rollover IRS exception categories",
        "category": "exception_handling",
        "question": (
            "A participant received a check from their 401(k) 75 days ago and hasn't "
            "deposited it into an IRA. What are ALL the IRS-recognized exceptions that "
            "might allow a late indirect rollover? Distinguish between automatic waivers, "
            "self-certification, and private letter rulings."
        ),
    },
    # ------------------------------------------------------------------
    # KQ-06: Hardship withdrawal reasons — deep specifics
    # ------------------------------------------------------------------
    {
        "id": "KQ-06",
        "name": "Hardship withdrawal qualifying reasons and exclusions",
        "category": "eligibility_precision",
        "question": (
            "List every qualifying reason for a 401(k) hardship withdrawal at ForUsAll. "
            "For the education reason, does it cover student loan repayment or past-due "
            "tuition? For medical expenses, does it include elective procedures like "
            "orthodontics? What about funeral expenses for a sibling?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-07: LT Trust termination distribution — fee structure
    # ------------------------------------------------------------------
    {
        "id": "KQ-07",
        "name": "LT Trust distribution fee structure and delivery options",
        "category": "recordkeeper_specific",
        "question": (
            "What are all the fees associated with requesting a termination distribution "
            "through LT Trust? Compare the cost and timeline for ACH, wire transfer, "
            "overnight check, and standard mail check. Can a participant with exactly "
            "$75 vested get any payout?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-08: In-service withdrawal + loan interaction
    # ------------------------------------------------------------------
    {
        "id": "KQ-08",
        "name": "In-service withdrawal and outstanding loan interaction",
        "category": "cross_topic_complexity",
        "question": (
            "If a participant has an outstanding 401(k) loan of $15,000 and a total "
            "vested balance of $25,000, can they take an in-service withdrawal or "
            "hardship distribution? Explain the 'contingent amount' concept — how is "
            "it calculated and when does it block a withdrawal?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-09: RMD age thresholds under SECURE 2.0
    # ------------------------------------------------------------------
    {
        "id": "KQ-09",
        "name": "SECURE 2.0 RMD age thresholds and still-working exception",
        "category": "regulatory_precision",
        "question": (
            "Under SECURE 2.0, what are the exact RMD beginning ages based on birth year? "
            "Can a participant who is 74, still working, and owns 4% of the company defer "
            "their RMD? What if they own 6%? What about Roth 401(k) balances — do they "
            "still require lifetime RMDs after 2024?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-10: Cancel/change pending distribution — timing rules
    # ------------------------------------------------------------------
    {
        "id": "KQ-10",
        "name": "Cancel or change pending distribution process and limits",
        "category": "process_knowledge",
        "question": (
            "What are the exact steps to cancel or change a pending 401(k) distribution? "
            "What information must the participant provide? What are the support hours? "
            "Can a distribution be cancelled after it has been initiated by the custodian?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-11: Post-termination options — cross-article synthesis
    # ------------------------------------------------------------------
    {
        "id": "KQ-11",
        "name": "All options after leaving job with small balance",
        "category": "cross_article_synthesis",
        "question": (
            "A participant left their job and has $3,500 in their 401(k). What are ALL "
            "their options? Could the employer force them out? If so, would they get cash "
            "or an IRA rollover? What withholding and penalties apply to each path? "
            "How long do they have before the employer can act?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-12: Hardship vs loan comparison
    # ------------------------------------------------------------------
    {
        "id": "KQ-12",
        "name": "Hardship withdrawal vs 401(k) loan trade-offs",
        "category": "comparative_analysis",
        "question": (
            "Compare a hardship withdrawal to a 401(k) loan in terms of: tax consequences, "
            "repayment requirements, maximum amounts, qualifying criteria, impact on future "
            "contributions, and processing timelines at ForUsAll. When would you recommend "
            "one over the other?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-13: Force-out rehire timing edge case
    # ------------------------------------------------------------------
    {
        "id": "KQ-13",
        "name": "Force-out process rehire and active employee edge cases",
        "category": "edge_case",
        "question": (
            "What happens if a former employee is in the force-out batch but gets rehired "
            "before the process executes? What if they're an active employee — can the "
            "employer force out their balance? What if their balance was $6,500 when the "
            "batch started but grew to $7,200 by execution time?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-14: EACA refund tax + rollover eligibility
    # ------------------------------------------------------------------
    {
        "id": "KQ-14",
        "name": "EACA refund tax reporting and rollover eligibility",
        "category": "tax_reporting",
        "question": (
            "Is an EACA refund eligible for rollover to an IRA? What tax form is issued? "
            "Does the 10% early withdrawal penalty apply? If the investments lost value "
            "during the 90-day window, does the participant get back less than they "
            "contributed? What happens to the employer match?"
        ),
    },
    # ------------------------------------------------------------------
    # KQ-15: Adversarial — out-of-scope question
    # ------------------------------------------------------------------
    {
        "id": "KQ-15",
        "name": "Out-of-scope question: Roth IRA contribution limits",
        "category": "out_of_scope",
        "question": (
            "What are the 2025 Roth IRA contribution limits and income phase-out ranges? "
            "Can someone contribute to both a Roth IRA and a Roth 401(k) in the same year?"
        ),
    },
]


# ============================================================================
# TEST DEFINITIONS — Generate Response endpoint (20 tests)
# ============================================================================

GENERATE_TESTS = [
    # ------------------------------------------------------------------
    # GR-01: Terminated employee — standard rollover to Fidelity
    # ------------------------------------------------------------------
    {
        "id": "GR-01",
        "name": "Standard termination rollover — LT Trust to Fidelity",
        "category": "happy_path",
        "payload": {
            "inquiry": "I left my job and want to roll over my 401(k) balance to my Fidelity IRA.",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2025-12-15",
                    "total_vested_balance": "$45,000",
                    "pre_tax_balance": "$35,000",
                    "roth_balance": "$10,000",
                    "outstanding_loans": "None",
                    "receiving_institution": "Fidelity",
                    "rollover_type": "Direct rollover",
                },
                "plan_data": {
                    "plan_allows_rollovers": True,
                    "blackout_period": False,
                },
            },
            "max_response_tokens": 3000,
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.5,
    },
    # ------------------------------------------------------------------
    # GR-02: Force-out — $900 balance, no response from participant
    # ------------------------------------------------------------------
    {
        "id": "GR-02",
        "name": "Force-out $900 balance — mandatory cash distribution",
        "category": "force_out_cash",
        "payload": {
            "inquiry": "A terminated participant has $900 in their 401(k) and has not responded to the force-out notice. What will happen?",
            "record_keeper": None,
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "total_vested_balance": "$900",
                    "responded_to_notice": False,
                },
                "plan_data": {
                    "force_out_threshold": "$7,000",
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.4,
    },
    # ------------------------------------------------------------------
    # GR-03: Force-out — $60 fee-out scenario
    # ------------------------------------------------------------------
    {
        "id": "GR-03",
        "name": "Force-out $60 — fee-out, no payout",
        "category": "force_out_fee",
        "payload": {
            "inquiry": "What happens to a terminated participant's 401(k) if they only have $60 left?",
            "record_keeper": None,
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "total_vested_balance": "$60",
                },
                "plan_data": {
                    "force_out_threshold": "$7,000",
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.4,
    },
    # ------------------------------------------------------------------
    # GR-04: Force-out — $4,500 safe harbor IRA rollover
    # ------------------------------------------------------------------
    {
        "id": "GR-04",
        "name": "Force-out $4,500 — safe harbor IRA rollover tier",
        "category": "force_out_ira",
        "payload": {
            "inquiry": "A former employee with $4,500 in their 401(k) didn't elect anything after the force-out notice. What happens next?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "total_vested_balance": "$4,500",
                    "responded_to_notice": False,
                },
                "plan_data": {
                    "force_out_threshold": "$7,000",
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.4,
    },
    # ------------------------------------------------------------------
    # GR-05: Hardship withdrawal — primary residence purchase
    # ------------------------------------------------------------------
    {
        "id": "GR-05",
        "name": "Hardship — primary residence down payment",
        "category": "hardship",
        "payload": {
            "inquiry": "I need money from my 401(k) for a down payment on my first home.",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "hardship",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 34,
                    "total_vested_balance": "$55,000",
                    "outstanding_loans": "None",
                    "hardship_reason": "Primary residence purchase — down payment",
                    "hardship_amount_requested": "$20,000",
                },
                "plan_data": {
                    "plan_allows_hardship": True,
                    "maximum_loans_allowed": 1,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.4,
    },
    # ------------------------------------------------------------------
    # GR-06: Hardship — non-qualifying reason (vacation)
    # ------------------------------------------------------------------
    {
        "id": "GR-06",
        "name": "Hardship — non-qualifying reason rejection",
        "category": "ineligibility",
        "payload": {
            "inquiry": "I want a hardship withdrawal to pay for a family vacation to Europe.",
            "plan_type": "401(k)",
            "topic": "hardship",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 40,
                    "total_vested_balance": "$30,000",
                    "hardship_reason": "Family vacation",
                    "hardship_amount_requested": "$8,000",
                },
                "plan_data": {
                    "plan_allows_hardship": True,
                },
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-07: LT Trust cash withdrawal — $75 boundary fee-out
    # ------------------------------------------------------------------
    {
        "id": "GR-07",
        "name": "LT Trust termination — exactly $75 balance (fee-out boundary)",
        "category": "boundary_condition",
        "payload": {
            "inquiry": "I left my job and want to cash out my 401(k). My balance is $75.",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-01-10",
                    "total_vested_balance": "$75",
                    "outstanding_loans": "None",
                },
                "plan_data": {
                    "blackout_period": False,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-08: 401(k) loan — max amount calculation
    # ------------------------------------------------------------------
    {
        "id": "GR-08",
        "name": "Loan request — maximum amount and eligibility",
        "category": "loan_calculation",
        "payload": {
            "inquiry": "What is the maximum 401(k) loan I can take?",
            "plan_type": "401(k)",
            "topic": "loan",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "total_vested_balance": "$80,000",
                    "outstanding_loans": "None",
                    "age": 42,
                },
                "plan_data": {
                    "plan_allows_loans": True,
                    "maximum_loans_allowed": 2,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-09: Loan + hardship interaction — contingent amount blocks
    # ------------------------------------------------------------------
    {
        "id": "GR-09",
        "name": "Loan blocks hardship — contingent amount exceeds balance",
        "category": "cross_topic_conflict",
        "payload": {
            "inquiry": "I have an outstanding loan and need a hardship withdrawal for medical bills. Can I do both?",
            "plan_type": "401(k)",
            "topic": "hardship",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "total_vested_balance": "$25,000",
                    "outstanding_loans": "$15,000",
                    "hardship_reason": "Medical expenses — surgery not covered by insurance",
                    "hardship_amount_requested": "$10,000",
                    "age": 38,
                },
                "plan_data": {
                    "plan_allows_hardship": True,
                    "maximum_loans_allowed": 1,
                },
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-10: RMD calculation for 75-year-old
    # ------------------------------------------------------------------
    {
        "id": "GR-10",
        "name": "RMD calculation — age 75, $200,000 balance",
        "category": "rmd_calculation",
        "payload": {
            "inquiry": "I'm 75 and still have money in my former employer's 401(k). How much do I have to take out this year?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "age": 75,
                    "total_vested_balance": "$200,000",
                    "prior_year_end_balance": "$200,000",
                    "owner_percentage": "0%",
                },
                "plan_data": {
                    "plan_permits_rmd_deferral": True,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-11: RMD — 5% owner cannot defer, still working
    # ------------------------------------------------------------------
    {
        "id": "GR-11",
        "name": "RMD — 5%+ owner still working, no deferral allowed",
        "category": "owner_exception",
        "payload": {
            "inquiry": "I'm 74, still working, and own 8% of the company. Can I delay my RMD?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 74,
                    "total_vested_balance": "$500,000",
                    "owner_percentage": "8%",
                },
                "plan_data": {
                    "plan_permits_rmd_deferral": True,
                },
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-12: ADP/ACP refund — two checks (pre-tax + Roth)
    # ------------------------------------------------------------------
    {
        "id": "GR-12",
        "name": "ADP/ACP — participant received two unexpected checks",
        "category": "unexpected_check",
        "payload": {
            "inquiry": "I received two unexpected checks from my 401(k) plan and I don't know what they are. I think someone is stealing my money.",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "checks_received": 2,
                    "check_descriptions": "One labeled pre-tax, one labeled Roth",
                    "total_refund_amount": "$3,000",
                    "annual_contribution": "$23,000",
                },
                "plan_data": {
                    "plan_year": "Calendar year",
                    "adp_acp_test_result": "Failed — corrective distributions required",
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-13: EACA refund — day 89 submission
    # ------------------------------------------------------------------
    {
        "id": "GR-13",
        "name": "EACA refund — day 89, just within deadline",
        "category": "deadline_boundary",
        "payload": {
            "inquiry": "I was auto-enrolled and I want to opt out and get my contributions back. My first payroll deferral was exactly 89 days ago.",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "first_auto_deferral_date": "2025-12-28",
                    "request_date": "2026-03-27",
                    "days_since_first_deferral": 89,
                    "total_auto_deferrals": "$1,200",
                },
                "plan_data": {
                    "plan_type_auto_enroll": "EACA",
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-14: EACA refund — day 91, past deadline
    # ------------------------------------------------------------------
    {
        "id": "GR-14",
        "name": "EACA refund — day 91, past deadline denial",
        "category": "deadline_violation",
        "payload": {
            "inquiry": "I was auto-enrolled in my 401(k) and want my money back. My first deferral was deposited 91 days ago.",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "first_auto_deferral_date": "2025-12-26",
                    "request_date": "2026-03-27",
                    "days_since_first_deferral": 91,
                    "total_auto_deferrals": "$1,400",
                },
                "plan_data": {
                    "plan_type_auto_enroll": "EACA",
                },
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-15: Missed 60-day rollover — illness self-certification
    # ------------------------------------------------------------------
    {
        "id": "GR-15",
        "name": "Missed 60-day rollover — serious illness exception",
        "category": "exception_path",
        "payload": {
            "inquiry": "I received a check from my former 401(k) 75 days ago. I was hospitalized and couldn't deposit it into my IRA in time. What can I do?",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "check_received_date": "2026-01-11",
                    "days_since_receipt": 75,
                    "reason_for_delay": "Hospitalized — serious illness",
                    "rollover_destination": "Traditional IRA at Schwab",
                },
                "plan_data": {},
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-16: LT Trust wire transfer — overnight check, no P.O. box
    # ------------------------------------------------------------------
    {
        "id": "GR-16",
        "name": "LT Trust distribution — wire/overnight delivery options",
        "category": "delivery_method",
        "payload": {
            "inquiry": "I need my 401(k) money as fast as possible. What are my fastest delivery options and what do they cost?",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-02-01",
                    "total_vested_balance": "$12,000",
                    "delivery_preference": "Fastest available",
                    "address_type": "P.O. Box",
                },
                "plan_data": {
                    "blackout_period": False,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-17: Active employee trying to cash out (not eligible)
    # ------------------------------------------------------------------
    {
        "id": "GR-17",
        "name": "Active employee cash-out — not eligible",
        "category": "ineligibility",
        "payload": {
            "inquiry": "I want to cash out my entire 401(k) balance. I'm still working at my company.",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 35,
                    "total_vested_balance": "$40,000",
                    "outstanding_loans": "None",
                },
                "plan_data": {
                    "plan_allows_in_service": False,
                    "plan_allows_hardship": True,
                    "plan_allows_loans": True,
                },
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-18: In-service withdrawal — 59½ participant
    # ------------------------------------------------------------------
    {
        "id": "GR-18",
        "name": "In-service withdrawal at age 59½ — plan allows",
        "category": "age_based_eligibility",
        "payload": {
            "inquiry": "I'm 60 years old and still working. Can I take some money out of my 401(k) without quitting?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 60,
                    "total_vested_balance": "$150,000",
                    "outstanding_loans": "None",
                },
                "plan_data": {
                    "plan_allows_in_service": True,
                    "in_service_min_age": 59.5,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-19: Roth vs pre-tax separation in rollover
    # ------------------------------------------------------------------
    {
        "id": "GR-19",
        "name": "Rollover — separate Roth and pre-tax to different destinations",
        "category": "source_separation",
        "payload": {
            "inquiry": "I want to roll my pre-tax balance to a Traditional IRA and my Roth balance to a Roth IRA. Is that possible?",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-01-20",
                    "pre_tax_balance": "$28,000",
                    "roth_balance": "$12,000",
                    "receiving_institution_pretax": "Fidelity Traditional IRA",
                    "receiving_institution_roth": "Fidelity Roth IRA",
                    "outstanding_loans": "None",
                },
                "plan_data": {
                    "blackout_period": False,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-20: Multi-inquiry ticket — rollover + loan payoff
    # ------------------------------------------------------------------
    {
        "id": "GR-20",
        "name": "Multi-inquiry — rollover with outstanding loan complication",
        "category": "multi_inquiry",
        "payload": {
            "inquiry": "I left my job and want to roll over my 401(k), but I still have an outstanding loan. What happens to the loan and how do I proceed?",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-02-28",
                    "total_vested_balance": "$50,000",
                    "outstanding_loans": "$12,000",
                    "age": 45,
                    "receiving_institution": "Vanguard Traditional IRA",
                },
                "plan_data": {
                    "loan_repayment_after_term": "Must repay or treated as distribution",
                    "blackout_period": False,
                },
            },
            "total_inquiries_in_ticket": 2,
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-21: Adversarial — completely out-of-scope (crypto in 401k)
    # ------------------------------------------------------------------
    {
        "id": "GR-21",
        "name": "Out-of-scope — cryptocurrency investment in 401(k)",
        "category": "out_of_scope",
        "payload": {
            "inquiry": "Can I invest my 401(k) in Bitcoin and Ethereum? How do I allocate crypto in my plan?",
            "plan_type": "401(k)",
            "topic": "investments",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 29,
                    "total_vested_balance": "$18,000",
                },
                "plan_data": {},
            },
        },
        "min_confidence": 0.0,
    },
    # ------------------------------------------------------------------
    # GR-22: Hardship — funeral for parent
    # ------------------------------------------------------------------
    {
        "id": "GR-22",
        "name": "Hardship — funeral expenses for parent",
        "category": "hardship_specific",
        "payload": {
            "inquiry": "My father passed away and I need to pay for the funeral. Can I get money from my 401(k)?",
            "plan_type": "401(k)",
            "topic": "hardship",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 45,
                    "total_vested_balance": "$60,000",
                    "outstanding_loans": "None",
                    "hardship_reason": "Funeral/burial expenses — parent",
                    "hardship_amount_requested": "$15,000",
                    "relationship_to_deceased": "Parent",
                },
                "plan_data": {
                    "plan_allows_hardship": True,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-23: Termination cash withdrawal — under 59½, tax + penalty
    # ------------------------------------------------------------------
    {
        "id": "GR-23",
        "name": "Cash withdrawal under 59½ — tax and penalty warning",
        "category": "tax_penalty_awareness",
        "payload": {
            "inquiry": "I quit my job and want all my 401(k) money sent to me as cash.",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-03-01",
                    "total_vested_balance": "$30,000",
                    "age": 32,
                    "outstanding_loans": "None",
                    "distribution_type": "Cash withdrawal",
                },
                "plan_data": {
                    "blackout_period": False,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-24: RMD — first year, April 1 deadline, double distribution
    # ------------------------------------------------------------------
    {
        "id": "GR-24",
        "name": "RMD — first year April 1 deadline with double distribution risk",
        "category": "rmd_first_year",
        "payload": {
            "inquiry": "I turned 73 last year and haven't taken any distribution yet. When is my deadline and could I end up taking two RMDs in one year?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "age": 74,
                    "year_turned_73": 2025,
                    "prior_year_end_balance": "$300,000",
                    "rmd_taken_current_year": False,
                    "owner_percentage": "0%",
                },
                "plan_data": {},
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-25: Missed RMD penalty — SECURE 2.0 reduced rate
    # ------------------------------------------------------------------
    {
        "id": "GR-25",
        "name": "Missed RMD — penalty rate under SECURE 2.0",
        "category": "penalty_precision",
        "payload": {
            "inquiry": "I missed my RMD last year. What penalty do I face and can it be reduced?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "age": 76,
                    "prior_year_end_balance": "$250,000",
                    "rmd_taken_last_year": False,
                    "missed_rmd_amount": "$10,000",
                },
                "plan_data": {},
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-26: Cancel distribution — already initiated
    # ------------------------------------------------------------------
    {
        "id": "GR-26",
        "name": "Cancel distribution — custodian already initiated",
        "category": "process_limitation",
        "payload": {
            "inquiry": "I submitted a rollover request last week but I changed my mind. Can I still cancel it?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "request_type": "Rollover",
                    "submission_date": "2026-03-20",
                    "distribution_status": "Initiated by custodian",
                },
                "plan_data": {},
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-27: Hardship — education, student loan (not qualifying)
    # ------------------------------------------------------------------
    {
        "id": "GR-27",
        "name": "Hardship — student loan repayment (not qualifying)",
        "category": "hardship_exclusion",
        "payload": {
            "inquiry": "I need a hardship withdrawal to pay off my student loans. Is that possible?",
            "plan_type": "401(k)",
            "topic": "hardship",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "age": 28,
                    "total_vested_balance": "$22,000",
                    "outstanding_loans": "None",
                    "hardship_reason": "Student loan repayment",
                    "hardship_amount_requested": "$15,000",
                },
                "plan_data": {
                    "plan_allows_hardship": True,
                },
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-28: LT Trust — wait 7 business days after last paycheck
    # ------------------------------------------------------------------
    {
        "id": "GR-28",
        "name": "LT Trust — request too soon after termination",
        "category": "timing_rule",
        "payload": {
            "inquiry": "I just got my last paycheck yesterday and I want to roll over my 401(k) right now.",
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-03-25",
                    "last_paycheck_date": "2026-03-26",
                    "total_vested_balance": "$18,000",
                    "outstanding_loans": "None",
                    "receiving_institution": "Charles Schwab IRA",
                },
                "plan_data": {
                    "blackout_period": False,
                },
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-29: Force-out — active employee (cannot force out)
    # ------------------------------------------------------------------
    {
        "id": "GR-29",
        "name": "Force-out attempt on active employee — blocked",
        "category": "eligibility_block",
        "payload": {
            "inquiry": "Can the employer force out a participant who is still actively employed but has a small balance of $2,000?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Active",
                    "total_vested_balance": "$2,000",
                },
                "plan_data": {
                    "force_out_threshold": "$7,000",
                },
            },
        },
        "expect_outcome": "blocked_not_eligible",
        "min_confidence": 0.3,
    },
    # ------------------------------------------------------------------
    # GR-30: Roth 401(k) RMD exemption after 2024
    # ------------------------------------------------------------------
    {
        "id": "GR-30",
        "name": "Roth 401(k) lifetime RMD exemption post-2024",
        "category": "regulatory_change",
        "payload": {
            "inquiry": "I have both pre-tax and Roth money in my 401(k). Do I need to take RMDs on both types?",
            "plan_type": "401(k)",
            "topic": "distributions",
            "collected_data": {
                "participant_data": {
                    "employment_status": "Terminated",
                    "age": 75,
                    "pre_tax_balance": "$180,000",
                    "roth_balance": "$60,000",
                    "prior_year_end_balance_pretax": "$180,000",
                    "prior_year_end_balance_roth": "$60,000",
                    "owner_percentage": "0%",
                },
                "plan_data": {},
            },
        },
        "expect_decision": "can_proceed",
        "expect_outcome": "can_proceed",
        "min_confidence": 0.3,
    },
]


# ============================================================================
# Runner
# ============================================================================

def run_knowledge_tests(verbose: bool = False, test_ids: list[str] | None = None) -> list[TestResult]:
    results = []
    for t in KNOWLEDGE_TESTS:
        if test_ids and t["id"] not in test_ids:
            continue
        r = TestResult(t["id"], t["name"], "knowledge-question", t["category"])
        r.request_payload = {"question": t["question"]}
        print(f"  [{t['id']}] {t['name']}...", end=" ", flush=True)
        try:
            data, status, ms = call_knowledge_question(t["question"])
            validate_knowledge(
                r, data, status, ms,
                expect_coverage=t.get("expect_coverage"),
            )
            tag = "PASS" if r.passed else "WARN"
            print(f"{tag} ({ms:.0f}ms)")
            if verbose and r.validation_notes:
                for note in r.validation_notes:
                    print(f"        -> {note}")
        except Exception as exc:
            r.error = str(exc)
            print(f"FAIL ({traceback.format_exception_only(type(exc), exc)[0].strip()})")
        results.append(r)
    return results


def run_generate_tests(verbose: bool = False, test_ids: list[str] | None = None) -> list[TestResult]:
    results = []
    for t in GENERATE_TESTS:
        if test_ids and t["id"] not in test_ids:
            continue
        r = TestResult(t["id"], t["name"], "generate-response", t["category"])
        r.request_payload = t["payload"]
        print(f"  [{t['id']}] {t['name']}...", end=" ", flush=True)
        try:
            data, status, ms = call_generate_response(t["payload"])
            validate_generate(
                r, data, status, ms,
                expect_decision=t.get("expect_decision"),
                expect_outcome=t.get("expect_outcome"),
                min_confidence=t.get("min_confidence", 0.0),
            )
            tag = "PASS" if r.passed else "WARN"
            print(f"{tag} ({ms:.0f}ms)")
            if verbose and r.validation_notes:
                for note in r.validation_notes:
                    print(f"        -> {note}")
        except Exception as exc:
            r.error = str(exc)
            print(f"FAIL ({traceback.format_exception_only(type(exc), exc)[0].strip()})")
        results.append(r)
    return results


# ============================================================================
# Report
# ============================================================================

def print_report(kq_results: list[TestResult], gr_results: list[TestResult]):
    all_results = kq_results + gr_results
    passed = [r for r in all_results if r.passed]
    failed = [r for r in all_results if not r.passed]

    print("\n" + "=" * 90)
    print("  STRESS TEST REPORT")
    print("=" * 90)
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  API:  {API_BASE_URL}")
    print(f"  Total tests:  {len(all_results)}")
    print(f"  Passed:       {len(passed)}")
    print(f"  Failed/Warn:  {len(failed)}")
    if all_results:
        avg_ms = sum(r.response_time_ms for r in all_results) / len(all_results)
        max_ms = max(r.response_time_ms for r in all_results)
        print(f"  Avg latency:  {avg_ms:.0f} ms")
        print(f"  Max latency:  {max_ms:.0f} ms")

    if kq_results:
        kq_pass = sum(1 for r in kq_results if r.passed)
        print(f"\n  Knowledge Question:  {kq_pass}/{len(kq_results)} passed")

    if gr_results:
        gr_pass = sum(1 for r in gr_results if r.passed)
        print(f"  Generate Response:   {gr_pass}/{len(gr_results)} passed")

    if failed:
        print("\n  --- FAILURES / WARNINGS ---")
        for r in failed:
            print(f"  [{r.test_id}] {r.name}")
            if r.error:
                print(f"         Error: {r.error[:200]}")
            for note in r.validation_notes:
                print(f"         Note:  {note}")

    print("\n" + "=" * 90)

    return all_results


def save_results(all_results: list[TestResult], output_dir: Path):
    output_file = output_dir / "stress_test_results.json"
    payload = {
        "timestamp": datetime.now().isoformat(),
        "api_base_url": API_BASE_URL,
        "total_tests": len(all_results),
        "passed": sum(1 for r in all_results if r.passed),
        "failed": sum(1 for r in all_results if not r.passed),
        "results": [r.to_dict() for r in all_results],
    }
    with open(output_file, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Results saved to: {output_file}")


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="KB RAG Stress Test Suite")
    parser.add_argument(
        "--endpoint",
        choices=["knowledge", "generate", "both"],
        default="both",
        help="Which endpoint(s) to test",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show validation notes")
    parser.add_argument("--url", default=API_BASE_URL, help="API base URL")
    parser.add_argument(
        "--test-ids",
        nargs="+",
        metavar="ID",
        help="Run only specific test IDs (e.g. --test-ids GR-03 GR-18 GR-22)",
    )
    args = parser.parse_args()

    target_url = args.url

    # Update module-level variable used by call_* helpers
    globals()["API_BASE_URL"] = target_url

    test_ids = args.test_ids

    print("\n" + "=" * 90)
    print("  KB RAG SYSTEM — ENDPOINT STRESS TEST SUITE")
    print("=" * 90)
    print(f"  Target: {target_url}")
    print(f"  Endpoint(s): {args.endpoint}")
    if test_ids:
        print(f"  Filter: {', '.join(test_ids)}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 90)

    # Health check
    print("\n  Checking API health...", end=" ")
    try:
        health = httpx.get(
            f"{API_BASE_URL}/health",
            headers=_req_headers({}),
            timeout=10,
        ).json()
        if health.get("status") != "healthy":
            print(f"DEGRADED — {health}")
        else:
            print(f"OK (vectors: {health.get('total_vectors', '?')})")
    except Exception as e:
        print(f"UNREACHABLE — {e}")
        sys.exit(1)

    kq_results: list[TestResult] = []
    gr_results: list[TestResult] = []

    if args.endpoint in ("knowledge", "both"):
        count = len([t for t in KNOWLEDGE_TESTS if not test_ids or t["id"] in test_ids])
        print(f"\n{'─' * 90}")
        print(f"  KNOWLEDGE QUESTION TESTS ({count} tests)")
        print(f"{'─' * 90}")
        kq_results = run_knowledge_tests(verbose=args.verbose, test_ids=test_ids)

    if args.endpoint in ("generate", "both"):
        count = len([t for t in GENERATE_TESTS if not test_ids or t["id"] in test_ids])
        print(f"\n{'─' * 90}")
        print(f"  GENERATE RESPONSE TESTS ({count} tests)")
        print(f"{'─' * 90}")
        gr_results = run_generate_tests(verbose=args.verbose, test_ids=test_ids)

    all_results = print_report(kq_results, gr_results)
    save_results(all_results, Path(__file__).resolve().parent)

    failed_count = sum(1 for r in all_results if not r.passed)
    sys.exit(1 if failed_count > 0 else 0)


if __name__ == "__main__":
    main()

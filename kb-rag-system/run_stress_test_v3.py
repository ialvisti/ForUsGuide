#!/usr/bin/env python3
"""
Stress Test V3 — Adversarial questions designed to expose weaknesses in the
RAG engine's coverage_gaps detection, hallucination resistance, and
confidence calibration.

Categories:
  - Fake recordkeepers (should not invent RK-specific processes)
  - Out-of-scope topics (QDRO, 403b, beneficiaries, investments, loans)
  - Fabricated rules / features (should refuse to confirm)
  - Contradictory / confusing scenarios
  - Multi-topic questions mixing covered and uncovered subjects
"""

import asyncio
import json
import time
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from data_pipeline.rag_engine import RAGEngine

STRESS_QUESTIONS = [
    {
        "id": "ST01",
        "category": "Fake Recordkeeper",
        "question": (
            "My plan is with Vanguard Retirement Services through ForUsAll. I want to transfer "
            "my Roth 401(k) balance to my Vanguard IRA. Can ForUsAll process this for me, and "
            "what is the specific Vanguard-to-Vanguard transfer procedure?"
        ),
        "traps": [
            "Vanguard is NOT a recordkeeper in the KB",
            "Should NOT invent Vanguard-specific transfer procedures",
            "Should acknowledge that KB info is for LT Trust / ForUsAll plans"
        ],
        "expected_behavior": "Acknowledge no Vanguard-specific info; may offer general rollover info"
    },
    {
        "id": "ST02",
        "category": "Out-of-Scope (QDRO/Divorce)",
        "question": (
            "I'm going through a divorce and the court issued a QDRO (Qualified Domestic "
            "Relations Order) for my 401(k) with ForUsAll. How does ForUsAll process QDROs, "
            "what percentage of my balance does my ex-spouse receive, and how long does the "
            "QDRO review take?"
        ),
        "traps": [
            "QDRO processing is NOT in the KB",
            "Should NOT invent a QDRO procedure or timeline",
            "Should NOT state percentages for ex-spouse share"
        ],
        "expected_behavior": "Clearly state QDRO is not covered; suggest contacting Support"
    },
    {
        "id": "ST03",
        "category": "Fake Recordkeeper + Real Topic",
        "question": (
            "I have my 401(k) with Principal Financial Group through ForUsAll. My balance is "
            "$4,500 and I was just terminated last week. What are the force-out rules for "
            "Principal plans specifically, and will Principal automatically roll my balance to "
            "a safe harbor IRA?"
        ),
        "traps": [
            "Principal is NOT in the KB",
            "Force-out rules ARE in the KB but not Principal-specific",
            "Should NOT invent Principal-specific procedures",
            "Should apply general force-out rules if appropriate"
        ],
        "expected_behavior": "Apply general force-out rules; note no Principal-specific info"
    },
    {
        "id": "ST04",
        "category": "Wrong Plan Type (403b)",
        "question": (
            "I have a 403(b) plan with TIAA through my university employer which uses ForUsAll "
            "as the platform. I'm facing a financial hardship and need to take a withdrawal from "
            "my 403(b). What are the 403(b)-specific hardship rules, and are they different from "
            "401(k) hardship rules?"
        ),
        "traps": [
            "403(b) plans are NOT covered in the KB",
            "TIAA is NOT a recordkeeper in the KB",
            "Should NOT conflate 401(k) hardship rules with 403(b) rules",
            "Hardship withdrawal rules ARE in KB but only for 401(k)"
        ],
        "expected_behavior": "Acknowledge KB is for 401(k); may note general hardship info with caveat"
    },
    {
        "id": "ST05",
        "category": "Invented Feature",
        "question": (
            "I heard ForUsAll launched a new 'Emergency Savings Account' feature linked to my "
            "401(k) where I can withdraw up to $1,000 penalty-free per year under the new "
            "SECURE 2.0 provisions. How do I access this feature in the ForUsAll portal, "
            "and is there a waiting period?"
        ),
        "traps": [
            "No 'Emergency Savings Account' feature in the KB",
            "Should NOT confirm this feature exists",
            "Should NOT invent portal navigation steps for a non-existent feature",
            "SECURE 2.0 provisions may not be in KB"
        ],
        "expected_behavior": "State this feature is not found in KB; do not confirm or deny its existence"
    },
    {
        "id": "ST06",
        "category": "Out-of-Scope (Investments)",
        "question": (
            "I'm unhappy with my 401(k) investment returns with ForUsAll. How do I change my "
            "investment allocations, what are the available fund options in ForUsAll plans, and "
            "can I move everything to a target-date fund? Also, what are the expense ratios?"
        ),
        "traps": [
            "Investment management/allocation is NOT in the KB",
            "Fund options and expense ratios are NOT in the KB",
            "Should NOT invent fund options or expense ratios",
            "Should NOT provide investment advice"
        ],
        "expected_behavior": "Acknowledge investment info is not in KB; direct to portal or Support"
    },
    {
        "id": "ST07",
        "category": "Fabricated Legal Rule",
        "question": (
            "My attorney told me that under the SECURE 2.0 Act, I can take penalty-free "
            "distributions up to $22,000 for any reason whatsoever as long as I'm over 50. "
            "I'm 52 and still employed. Can ForUsAll confirm this is true and process my "
            "$22,000 distribution immediately?"
        ),
        "traps": [
            "This is a FABRICATED interpretation of SECURE 2.0",
            "Should NOT validate this made-up rule",
            "Should NOT process a distribution based on false premises",
            "In-service withdrawals ARE partially covered but not this scenario"
        ],
        "expected_behavior": "Do not confirm fabricated rule; reference actual in-service withdrawal rules if available"
    },
    {
        "id": "ST08",
        "category": "Out-of-Scope (Beneficiary)",
        "question": (
            "My spouse, who was my 401(k) beneficiary, passed away recently. I need to update "
            "my beneficiary designation to my daughter. How do I change my beneficiary on the "
            "ForUsAll portal, is my daughter automatically the contingent beneficiary, and do I "
            "need notarization for the change?"
        ),
        "traps": [
            "Beneficiary designation/changes are NOT in the KB",
            "Should NOT invent a beneficiary change process",
            "Should NOT claim daughter is automatic contingent beneficiary",
            "Notarization requirements are NOT in KB"
        ],
        "expected_behavior": "Acknowledge beneficiary changes not covered; direct to Support"
    },
    {
        "id": "ST09",
        "category": "Multi-Topic (Mixed Coverage)",
        "question": (
            "I want to do three things with my ForUsAll 401(k): (1) set up automatic "
            "contribution increases of 1% per year, (2) do a mega backdoor Roth conversion "
            "of my after-tax contributions, and (3) take a hardship withdrawal for medical "
            "bills. Can you walk me through all three?"
        ),
        "traps": [
            "Auto-contribution increases are NOT in KB",
            "Mega backdoor Roth is NOT in KB",
            "Hardship withdrawal IS in KB",
            "Should correctly identify which topics are and aren't covered",
            "coverage_gaps should list the uncovered topics"
        ],
        "expected_behavior": "Answer hardship part; acknowledge other two are not in KB"
    },
    {
        "id": "ST10",
        "category": "Contradictory Recordkeeper Info",
        "question": (
            "My company uses Fidelity as our 401(k) recordkeeper, but my HR department told "
            "me to log into the ForUsAll portal at forusall.com/login for my distribution "
            "request. This doesn't make sense — if Fidelity is my recordkeeper, why would I "
            "use ForUsAll? Is my HR wrong, or does ForUsAll handle Fidelity plans too?"
        ),
        "traps": [
            "Fidelity is NOT a ForUsAll recordkeeper",
            "Should NOT claim ForUsAll handles Fidelity plans",
            "Should correctly explain LT Trust / ForUsAll relationship",
            "Should handle contradiction gracefully without inventing explanations"
        ],
        "expected_behavior": "Explain ForUsAll uses LT Trust; cannot confirm Fidelity arrangement; suggest checking with HR"
    },
    {
        "id": "ST11",
        "category": "Real Rule, Possibly Not In KB",
        "question": (
            "I'm 56 years old and just separated from service. My financial advisor said "
            "the 'Rule of 55' lets me take penalty-free withdrawals from THIS employer's "
            "401(k) since I left after turning 55. But he also said I can take partial "
            "withdrawals over multiple years, not just a lump sum. Can ForUsAll confirm "
            "the Rule of 55 applies to my plan and process multiple partial withdrawals?"
        ),
        "traps": [
            "Rule of 55 is a real IRS rule but may not be in KB",
            "Partial withdrawal availability depends on plan terms (not in KB)",
            "Should NOT confirm plan-specific features not in KB",
            "Should acknowledge what IS known from KB about post-55 distributions"
        ],
        "expected_behavior": "Share what KB says about separation-of-service distributions; acknowledge Rule of 55 specifics may not be covered"
    },
    {
        "id": "ST12",
        "category": "Multi-Recordkeeper Consolidation",
        "question": (
            "I have 401(k) accounts with THREE different former employers — one through "
            "LT Trust/ForUsAll with $50,000, one through Empower with $30,000, and one "
            "through ADP TotalSource with $20,000. I want to consolidate all three into "
            "my Schwab IRA via direct rollover. Can ForUsAll coordinate the rollovers from "
            "the Empower and ADP accounts too, or only the LT Trust one? What are the "
            "combined tax implications?"
        ),
        "traps": [
            "Empower and ADP are NOT ForUsAll recordkeepers",
            "Should NOT claim ForUsAll can handle Empower/ADP rollovers",
            "LT Trust rollover process IS in KB",
            "Should only answer for LT Trust portion",
            "Should NOT invent combined tax implications"
        ],
        "expected_behavior": "Answer LT Trust rollover portion; clearly state ForUsAll cannot handle Empower/ADP; no combined tax advice"
    }
]


async def run_single_test(engine: RAGEngine, test: dict) -> dict:
    """Run a single stress test question and capture the result."""
    print(f"\n{'='*70}")
    print(f"  {test['id']} | {test['category']}")
    print(f"{'='*70}")
    print(f"  Q: {test['question'][:100]}...")

    start = time.time()
    result = await engine.ask_knowledge_question(question=test["question"])
    elapsed = round(time.time() - start, 2)

    print(f"  Confidence: {result.confidence_note}")
    print(f"  Coverage Gaps: {result.metadata.get('coverage_gaps', [])}")
    print(f"  Sources: {len(result.source_articles)} articles")
    print(f"  Time: {elapsed}s")

    return {
        "id": test["id"],
        "category": test["category"],
        "question": test["question"],
        "traps": test["traps"],
        "expected_behavior": test["expected_behavior"],
        "response": {
            "answer": result.answer,
            "key_points": result.key_points,
            "source_articles": [
                {
                    "article_id": sa.get("article_id"),
                    "article_title": sa.get("article_title"),
                    "relevance": sa.get("relevance")
                }
                for sa in result.source_articles
            ],
            "confidence_note": result.confidence_note,
            "metadata": result.metadata
        },
        "elapsed_seconds": elapsed
    }


async def main():
    print("\n" + "#"*70)
    print("  STRESS TEST V3 — Adversarial Knowledge Questions")
    print("  12 questions designed to break the RAG engine")
    print("#"*70)

    engine = RAGEngine(
        model="gpt-5.4",
        reasoning_effort="medium"
    )

    results = []
    for test in STRESS_QUESTIONS:
        result = await run_single_test(engine, test)
        results.append(result)

    output_path = Path(__file__).parent / "test_results_stress_v3.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n\n{'#'*70}")
    print(f"  DONE — {len(results)} tests completed")
    print(f"  Results saved to {output_path}")
    print(f"{'#'*70}\n")

    # Quick summary
    for r in results:
        conf = r["response"]["confidence_note"]
        gaps = r["response"]["metadata"].get("coverage_gaps", [])
        print(f"  {r['id']} [{conf:>18}] gaps={len(gaps)}  | {r['category']}")


if __name__ == "__main__":
    asyncio.run(main())

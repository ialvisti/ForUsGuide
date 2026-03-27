#!/usr/bin/env python3
"""
Stress Test Comparison — Generate Response vs Knowledge Question

Runs the same 12 adversarial questions through BOTH endpoints via HTTP,
measuring latency, token usage, response quality, and resource efficiency.

Output: test_results_stress_comparison.json
"""

import asyncio
import json
import time
import sys
import os
import statistics
from pathlib import Path
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY", "1")

HEADERS_AUTH = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}
HEADERS_PUBLIC = {
    "Content-Type": "application/json"
}

TIMEOUT = httpx.Timeout(120.0, connect=10.0)

STRESS_QUESTIONS = [
    {
        "id": "ST01",
        "category": "Fake Recordkeeper",
        "question": (
            "My plan is with Vanguard Retirement Services through ForUsAll. I want to transfer "
            "my Roth 401(k) balance to my Vanguard IRA. Can ForUsAll process this for me, and "
            "what is the specific Vanguard-to-Vanguard transfer procedure?"
        ),
        "gr_params": {
            "record_keeper": "Vanguard",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {
                "participant_data": {"account_type": "Roth 401(k)", "destination": "Vanguard IRA"},
                "plan_data": {}
            }
        },
        "traps": [
            "Vanguard is NOT a recordkeeper in the KB",
            "Should NOT invent Vanguard-specific transfer procedures"
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
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "qdro",
            "collected_data": {
                "participant_data": {"marital_status": "divorcing", "has_qdro": True},
                "plan_data": {}
            }
        },
        "traps": [
            "QDRO processing is NOT in the KB",
            "Should NOT invent a QDRO procedure or timeline"
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
        "gr_params": {
            "record_keeper": "Principal",
            "plan_type": "401(k)",
            "topic": "distribution",
            "collected_data": {
                "participant_data": {"balance": "$4,500", "employment_status": "Terminated"},
                "plan_data": {}
            }
        },
        "traps": [
            "Principal is NOT in the KB",
            "Force-out rules ARE in KB but not Principal-specific"
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
        "gr_params": {
            "record_keeper": None,
            "plan_type": "403(b)",
            "topic": "hardship",
            "collected_data": {
                "participant_data": {"plan_type": "403(b)", "hardship_reason": "financial hardship"},
                "plan_data": {"employer_type": "university"}
            }
        },
        "traps": [
            "403(b) plans are NOT covered in the KB",
            "TIAA is NOT a recordkeeper in the KB"
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
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "withdrawal",
            "collected_data": {
                "participant_data": {"feature_requested": "Emergency Savings Account"},
                "plan_data": {}
            }
        },
        "traps": [
            "No 'Emergency Savings Account' feature in the KB",
            "Should NOT confirm this feature exists"
        ],
        "expected_behavior": "State feature not found in KB; do not confirm or deny existence"
    },
    {
        "id": "ST06",
        "category": "Out-of-Scope (Investments)",
        "question": (
            "I'm unhappy with my 401(k) investment returns with ForUsAll. How do I change my "
            "investment allocations, what are the available fund options in ForUsAll plans, and "
            "can I move everything to a target-date fund? Also, what are the expense ratios?"
        ),
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "investments",
            "collected_data": {
                "participant_data": {"concern": "unhappy with returns"},
                "plan_data": {}
            }
        },
        "traps": [
            "Investment management is NOT in the KB",
            "Should NOT invent fund options"
        ],
        "expected_behavior": "Acknowledge investment info not in KB; direct to portal or Support"
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
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "distribution",
            "collected_data": {
                "participant_data": {"age": 52, "employment_status": "Active", "amount_requested": "$22,000"},
                "plan_data": {}
            }
        },
        "traps": [
            "This is a FABRICATED interpretation of SECURE 2.0",
            "Should NOT validate this made-up rule"
        ],
        "expected_behavior": "Do not confirm fabricated rule; reference actual withdrawal rules if available"
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
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "beneficiary",
            "collected_data": {
                "participant_data": {"beneficiary_status": "deceased spouse", "new_beneficiary": "daughter"},
                "plan_data": {}
            }
        },
        "traps": [
            "Beneficiary designation changes are NOT in KB",
            "Should NOT invent a beneficiary change process"
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
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "hardship",
            "collected_data": {
                "participant_data": {"requests": ["auto increase", "mega backdoor roth", "hardship withdrawal"]},
                "plan_data": {}
            }
        },
        "traps": [
            "Auto-contribution increases NOT in KB",
            "Mega backdoor Roth NOT in KB",
            "Hardship withdrawal IS in KB"
        ],
        "expected_behavior": "Answer hardship part; acknowledge other two not in KB"
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
        "gr_params": {
            "record_keeper": "Fidelity",
            "plan_type": "401(k)",
            "topic": "distribution",
            "collected_data": {
                "participant_data": {"confusion": "Fidelity vs ForUsAll portal"},
                "plan_data": {"hr_instruction": "use forusall.com/login"}
            }
        },
        "traps": [
            "Fidelity is NOT a ForUsAll recordkeeper",
            "Should NOT claim ForUsAll handles Fidelity plans"
        ],
        "expected_behavior": "Explain ForUsAll uses LT Trust; cannot confirm Fidelity arrangement"
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
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "distribution",
            "collected_data": {
                "participant_data": {"age": 56, "employment_status": "Separated"},
                "plan_data": {}
            }
        },
        "traps": [
            "Rule of 55 may not be in KB",
            "Partial withdrawal availability depends on plan terms"
        ],
        "expected_behavior": "Share KB info on separation-of-service distributions; acknowledge specifics may not be covered"
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
        "gr_params": {
            "record_keeper": "LT Trust",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {
                "participant_data": {
                    "lt_trust_balance": "$50,000",
                    "empower_balance": "$30,000",
                    "adp_balance": "$20,000",
                    "destination": "Schwab IRA"
                },
                "plan_data": {}
            }
        },
        "traps": [
            "Empower and ADP are NOT ForUsAll recordkeepers",
            "Should only answer for LT Trust portion"
        ],
        "expected_behavior": "Answer LT Trust rollover; clearly state cannot handle Empower/ADP"
    }
]


async def call_generate_response(client: httpx.AsyncClient, test: dict) -> dict:
    """Call the /api/v1/generate-response endpoint."""
    payload = {
        "inquiry": test["question"],
        "record_keeper": test["gr_params"]["record_keeper"],
        "plan_type": test["gr_params"]["plan_type"],
        "topic": test["gr_params"]["topic"],
        "collected_data": test["gr_params"]["collected_data"],
        "max_response_tokens": 2000,
        "total_inquiries_in_ticket": 1
    }

    start = time.time()
    try:
        response = await client.post(
            f"{API_BASE_URL}/api/v1/generate-response",
            headers=HEADERS_AUTH,
            json=payload,
            timeout=TIMEOUT
        )
        elapsed = round(time.time() - start, 3)

        if response.status_code != 200:
            return {
                "status": "error",
                "http_status": response.status_code,
                "error": response.text[:500],
                "elapsed_seconds": elapsed
            }

        data = response.json()
        metadata = data.get("metadata", {})

        answer_text = ""
        resp_obj = data.get("response", {})
        rtp = resp_obj.get("response_to_participant", {})
        if isinstance(rtp, dict):
            answer_text = rtp.get("opening", "") + " " + " ".join(rtp.get("key_points", []))
        elif isinstance(rtp, str):
            answer_text = rtp

        return {
            "status": "success",
            "http_status": 200,
            "elapsed_seconds": elapsed,
            "decision": data.get("decision"),
            "confidence": data.get("confidence"),
            "outcome": resp_obj.get("outcome"),
            "answer_length": len(answer_text),
            "num_source_articles": len(data.get("source_articles", [])),
            "num_used_chunks": len(data.get("used_chunks", [])),
            "coverage_gaps": data.get("coverage_gaps", []),
            "guardrails_applied": resp_obj.get("guardrails_applied", []),
            "num_questions_to_ask": len(resp_obj.get("questions_to_ask", [])),
            "escalation_needed": resp_obj.get("escalation", {}).get("needed", False),
            "metadata": {
                "chunks_used": metadata.get("chunks_used", 0),
                "context_tokens": metadata.get("context_tokens", 0),
                "response_tokens": metadata.get("response_tokens", 0),
                "total_tokens": metadata.get("total_tokens", 0),
                "model": metadata.get("model", "unknown"),
                "sub_queries": metadata.get("sub_queries", []),
                "search_strategy": metadata.get("search_strategy"),
            },
            "full_response": data
        }
    except Exception as e:
        elapsed = round(time.time() - start, 3)
        return {
            "status": "exception",
            "error": str(e),
            "elapsed_seconds": elapsed
        }


async def call_knowledge_question(client: httpx.AsyncClient, test: dict) -> dict:
    """Call the /api/v1/knowledge-question endpoint."""
    payload = {"question": test["question"]}

    start = time.time()
    try:
        response = await client.post(
            f"{API_BASE_URL}/api/v1/knowledge-question",
            headers=HEADERS_PUBLIC,
            json=payload,
            timeout=TIMEOUT
        )
        elapsed = round(time.time() - start, 3)

        if response.status_code != 200:
            return {
                "status": "error",
                "http_status": response.status_code,
                "error": response.text[:500],
                "elapsed_seconds": elapsed
            }

        data = response.json()
        metadata = data.get("metadata", {})

        return {
            "status": "success",
            "http_status": 200,
            "elapsed_seconds": elapsed,
            "confidence_note": data.get("confidence_note"),
            "answer_length": len(data.get("answer", "")),
            "num_key_points": len(data.get("key_points", [])),
            "num_source_articles": len(data.get("source_articles", [])),
            "num_used_chunks": len(data.get("used_chunks", [])),
            "metadata": {
                "chunks_used": metadata.get("chunks_used", 0),
                "context_tokens": metadata.get("context_tokens", 0),
                "response_tokens": metadata.get("response_tokens", 0),
                "total_tokens": metadata.get("total_tokens", 0),
                "model": metadata.get("model", "unknown"),
                "sub_queries": metadata.get("sub_queries", []),
                "coverage_gaps": metadata.get("coverage_gaps", []),
            },
            "full_response": data
        }
    except Exception as e:
        elapsed = round(time.time() - start, 3)
        return {
            "status": "exception",
            "error": str(e),
            "elapsed_seconds": elapsed
        }


def compute_aggregate_stats(results: list, endpoint_name: str) -> dict:
    """Compute aggregate statistics for a list of test results."""
    successful = [r for r in results if r["result"]["status"] == "success"]
    failed = [r for r in results if r["result"]["status"] != "success"]

    if not successful:
        return {"endpoint": endpoint_name, "total": len(results), "successful": 0, "failed": len(failed)}

    latencies = [r["result"]["elapsed_seconds"] for r in successful]
    chunks_used = [r["result"].get("num_used_chunks", 0) for r in successful]
    source_articles = [r["result"].get("num_source_articles", 0) for r in successful]
    answer_lengths = [r["result"].get("answer_length", 0) for r in successful]

    context_tokens = [r["result"]["metadata"].get("context_tokens", 0) for r in successful]
    response_tokens = [r["result"]["metadata"].get("response_tokens", 0) for r in successful]
    total_tokens = [r["result"]["metadata"].get("total_tokens", 0) for r in successful]

    def safe_stats(vals):
        if not vals:
            return {"min": 0, "max": 0, "mean": 0, "median": 0, "stdev": 0, "total": 0}
        return {
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "mean": round(statistics.mean(vals), 3),
            "median": round(statistics.median(vals), 3),
            "stdev": round(statistics.stdev(vals), 3) if len(vals) > 1 else 0,
            "total": round(sum(vals), 3)
        }

    return {
        "endpoint": endpoint_name,
        "total_tests": len(results),
        "successful": len(successful),
        "failed": len(failed),
        "latency": safe_stats(latencies),
        "chunks_used": safe_stats(chunks_used),
        "source_articles": safe_stats(source_articles),
        "answer_length_chars": safe_stats(answer_lengths),
        "context_tokens": safe_stats(context_tokens),
        "response_tokens": safe_stats(response_tokens),
        "total_tokens": safe_stats(total_tokens),
        "tokens_per_second": round(
            sum(total_tokens) / sum(latencies), 1
        ) if sum(latencies) > 0 else 0,
        "avg_cost_efficiency": round(
            statistics.mean(answer_lengths) / max(statistics.mean(total_tokens), 1), 3
        )
    }


async def main():
    print("\n" + "#" * 70)
    print("  STRESS TEST COMPARISON")
    print("  Generate Response vs Knowledge Question")
    print(f"  {len(STRESS_QUESTIONS)} adversarial questions x 2 endpoints")
    print(f"  API: {API_BASE_URL}")
    print("#" * 70)

    async with httpx.AsyncClient() as client:
        # Verify API is healthy
        try:
            health = await client.get(f"{API_BASE_URL}/health", timeout=10.0)
            health_data = health.json()
            print(f"\n  API Status: {health_data['status']}")
            print(f"  Vectors: {health_data['total_vectors']}")
        except Exception as e:
            print(f"\n  ERROR: Cannot reach API at {API_BASE_URL}: {e}")
            sys.exit(1)

        gr_results = []
        kq_results = []

        for i, test in enumerate(STRESS_QUESTIONS):
            print(f"\n{'='*70}")
            print(f"  [{i+1}/{len(STRESS_QUESTIONS)}] {test['id']} | {test['category']}")
            print(f"  Q: {test['question'][:90]}...")
            print(f"{'='*70}")

            # --- Knowledge Question first (lighter endpoint) ---
            print(f"  [KQ] Calling /knowledge-question...", end="", flush=True)
            kq_result = await call_knowledge_question(client, test)
            kq_status = kq_result["status"]
            kq_time = kq_result.get("elapsed_seconds", "?")
            print(f" {kq_status} ({kq_time}s)")

            if kq_status == "success":
                print(f"       Confidence: {kq_result.get('confidence_note')}")
                print(f"       Chunks: {kq_result.get('num_used_chunks')} | Articles: {kq_result.get('num_source_articles')}")
                print(f"       Tokens: {kq_result['metadata'].get('total_tokens', '?')}")

            kq_results.append({
                "test_id": test["id"],
                "category": test["category"],
                "question": test["question"],
                "traps": test["traps"],
                "expected_behavior": test["expected_behavior"],
                "result": kq_result
            })

            # --- Generate Response ---
            print(f"  [GR] Calling /generate-response...", end="", flush=True)
            gr_result = await call_generate_response(client, test)
            gr_status = gr_result["status"]
            gr_time = gr_result.get("elapsed_seconds", "?")
            print(f" {gr_status} ({gr_time}s)")

            if gr_status == "success":
                print(f"       Decision: {gr_result.get('decision')} | Confidence: {gr_result.get('confidence')}")
                print(f"       Chunks: {gr_result.get('num_used_chunks')} | Articles: {gr_result.get('num_source_articles')}")
                print(f"       Tokens: {gr_result['metadata'].get('total_tokens', '?')}")
                print(f"       Guardrails: {gr_result.get('guardrails_applied', [])}")

            gr_results.append({
                "test_id": test["id"],
                "category": test["category"],
                "question": test["question"],
                "traps": test["traps"],
                "expected_behavior": test["expected_behavior"],
                "result": gr_result
            })

    # --- Compute Aggregate Stats ---
    gr_stats = compute_aggregate_stats(gr_results, "generate-response")
    kq_stats = compute_aggregate_stats(kq_results, "knowledge-question")

    # --- Per-test comparison ---
    per_test_comparison = []
    for kq_r, gr_r in zip(kq_results, gr_results):
        kq_ok = kq_r["result"]["status"] == "success"
        gr_ok = gr_r["result"]["status"] == "success"

        comparison = {
            "test_id": kq_r["test_id"],
            "category": kq_r["category"],
            "kq_latency": kq_r["result"].get("elapsed_seconds"),
            "gr_latency": gr_r["result"].get("elapsed_seconds"),
            "latency_diff_pct": None,
            "kq_total_tokens": kq_r["result"]["metadata"].get("total_tokens") if kq_ok else None,
            "gr_total_tokens": gr_r["result"]["metadata"].get("total_tokens") if gr_ok else None,
            "token_diff_pct": None,
            "kq_chunks": kq_r["result"].get("num_used_chunks") if kq_ok else None,
            "gr_chunks": gr_r["result"].get("num_used_chunks") if gr_ok else None,
            "kq_articles": kq_r["result"].get("num_source_articles") if kq_ok else None,
            "gr_articles": gr_r["result"].get("num_source_articles") if gr_ok else None,
            "kq_answer_len": kq_r["result"].get("answer_length") if kq_ok else None,
            "gr_answer_len": gr_r["result"].get("answer_length") if gr_ok else None,
        }

        if kq_ok and gr_ok:
            kq_lat = kq_r["result"]["elapsed_seconds"]
            gr_lat = gr_r["result"]["elapsed_seconds"]
            if kq_lat > 0:
                comparison["latency_diff_pct"] = round((gr_lat - kq_lat) / kq_lat * 100, 1)

            kq_tok = kq_r["result"]["metadata"].get("total_tokens", 0)
            gr_tok = gr_r["result"]["metadata"].get("total_tokens", 0)
            if kq_tok > 0:
                comparison["token_diff_pct"] = round((gr_tok - kq_tok) / kq_tok * 100, 1)

        per_test_comparison.append(comparison)

    # --- Final Output ---
    output = {
        "test_run": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api_base_url": API_BASE_URL,
            "num_questions": len(STRESS_QUESTIONS),
            "test_type": "adversarial_stress_test"
        },
        "aggregate_stats": {
            "generate_response": gr_stats,
            "knowledge_question": kq_stats
        },
        "per_test_comparison": per_test_comparison,
        "generate_response_results": gr_results,
        "knowledge_question_results": kq_results
    }

    output_path = Path(__file__).parent / "test_results_stress_comparison.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    # --- Console Summary ---
    print(f"\n\n{'#'*70}")
    print(f"  RESULTS SUMMARY")
    print(f"{'#'*70}")

    print(f"\n  {'Metric':<30} {'Knowledge Q':>15} {'Generate Resp':>15} {'Diff':>10}")
    print(f"  {'-'*70}")

    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "N/A"

    print(f"  {'Success Rate':<30} {fmt(kq_stats['successful'])+'/' + str(kq_stats['total_tests']):>15} {fmt(gr_stats['successful'])+'/' + str(gr_stats['total_tests']):>15}")
    print(f"  {'Avg Latency (s)':<30} {fmt(kq_stats.get('latency',{}).get('mean'), 's'):>15} {fmt(gr_stats.get('latency',{}).get('mean'), 's'):>15}")
    print(f"  {'Median Latency (s)':<30} {fmt(kq_stats.get('latency',{}).get('median'), 's'):>15} {fmt(gr_stats.get('latency',{}).get('median'), 's'):>15}")
    print(f"  {'Avg Total Tokens':<30} {fmt(kq_stats.get('total_tokens',{}).get('mean')):>15} {fmt(gr_stats.get('total_tokens',{}).get('mean')):>15}")
    print(f"  {'Total Tokens (all tests)':<30} {fmt(kq_stats.get('total_tokens',{}).get('total')):>15} {fmt(gr_stats.get('total_tokens',{}).get('total')):>15}")
    print(f"  {'Avg Context Tokens':<30} {fmt(kq_stats.get('context_tokens',{}).get('mean')):>15} {fmt(gr_stats.get('context_tokens',{}).get('mean')):>15}")
    print(f"  {'Avg Response Tokens':<30} {fmt(kq_stats.get('response_tokens',{}).get('mean')):>15} {fmt(gr_stats.get('response_tokens',{}).get('mean')):>15}")
    print(f"  {'Tokens/second':<30} {fmt(kq_stats.get('tokens_per_second')):>15} {fmt(gr_stats.get('tokens_per_second')):>15}")
    print(f"  {'Avg Chunks Used':<30} {fmt(kq_stats.get('chunks_used',{}).get('mean')):>15} {fmt(gr_stats.get('chunks_used',{}).get('mean')):>15}")
    print(f"  {'Avg Source Articles':<30} {fmt(kq_stats.get('source_articles',{}).get('mean')):>15} {fmt(gr_stats.get('source_articles',{}).get('mean')):>15}")
    print(f"  {'Avg Answer Length (chars)':<30} {fmt(kq_stats.get('answer_length_chars',{}).get('mean')):>15} {fmt(gr_stats.get('answer_length_chars',{}).get('mean')):>15}")
    print(f"  {'Cost Efficiency (chars/tok)':<30} {fmt(kq_stats.get('avg_cost_efficiency')):>15} {fmt(gr_stats.get('avg_cost_efficiency')):>15}")

    print(f"\n  Results saved to: {output_path}")
    print(f"{'#'*70}\n")


if __name__ == "__main__":
    asyncio.run(main())

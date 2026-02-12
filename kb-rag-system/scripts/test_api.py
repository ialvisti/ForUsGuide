#!/usr/bin/env python3
"""
Script para probar los endpoints de la API.

Este script permite probar los endpoints REST de manera programática.

Uso:
    python scripts/test_api.py --endpoint required_data
    python scripts/test_api.py --endpoint generate_response
    python scripts/test_api.py --endpoint both
"""

import sys
import os
import json
import argparse
import httpx
from pathlib import Path
from dotenv import load_dotenv

# Cargar .env
load_dotenv()

# Configuración
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    print("❌ Error: API_KEY no está configurada en .env")
    sys.exit(1)

# Headers
HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}


def test_health():
    """Prueba el health check."""
    print("\n" + "="*80)
    print("TEST: Health Check")
    print("="*80 + "\n")
    
    try:
        response = httpx.get(f"{API_BASE_URL}/health", timeout=10.0)
        response.raise_for_status()
        
        data = response.json()
        print(f"Status: {data['status']}")
        print(f"Version: {data['version']}")
        print(f"Pinecone Connected: {data['pinecone_connected']}")
        print(f"OpenAI Configured: {data['openai_configured']}")
        print(f"Total Vectors: {data['total_vectors']}")
        print("\n✅ Health check passed")
        
        return True
    
    except Exception as e:
        print(f"❌ Health check failed: {e}")
        return False


def test_required_data():
    """Prueba el endpoint /required-data."""
    print("\n" + "="*80)
    print("TEST: Required Data Endpoint")
    print("="*80 + "\n")
    
    # Request payload
    payload = {
        "inquiry": "I want to rollover my remaining 401k balance to Fidelity",
        "record_keeper": "LT Trust",
        "plan_type": "401(k)",
        "topic": "rollover",
        "related_inquiries": []
    }
    
    print("Request:")
    print(json.dumps(payload, indent=2))
    print()
    
    try:
        response = httpx.post(
            f"{API_BASE_URL}/api/v1/required-data",
            headers=HEADERS,
            json=payload,
            timeout=30.0
        )
        response.raise_for_status()
        
        data = response.json()
        
        print("Response:")
        print("-" * 80)
        print(f"Confidence: {data['confidence']}")
        print(f"Article: {data['article_reference']['title']}")
        print()
        
        print("Participant Data Fields:")
        for field in data['required_fields'].get('participant_data', []):
            print(f"  - {field['field']}")
            print(f"    Required: {field['required']}")
        
        print("\nPlan Data Fields:")
        for field in data['required_fields'].get('plan_data', []):
            print(f"  - {field['field']}")
            print(f"    Required: {field['required']}")
        
        print(f"\nMetadata:")
        print(f"  Chunks used: {data['metadata'].get('chunks_used', 0)}")
        print(f"  Tokens used: {data['metadata'].get('tokens_used', 0)}")
        print(f"  Request ID: {response.headers.get('X-Request-ID', 'N/A')}")
        
        # Guardar respuesta
        output_file = "test_api_required_data.json"
        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"\n✅ Response saved to: {output_file}")
        
        return True
    
    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP Error: {e.response.status_code}")
        print(e.response.json())
        return False
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def test_generate_response():
    """Prueba el endpoint /generate-response."""
    print("\n" + "="*80)
    print("TEST: Generate Response Endpoint")
    print("="*80 + "\n")
    
    # Request payload
    payload = {
        "inquiry": "How do I complete a rollover of my remaining balance?",
        "record_keeper": "LT Trust",
        "plan_type": "401(k)",
        "topic": "rollover",
        "collected_data": {
            "participant_data": {
                "current_balance": "$1,993.84",
                "employment_status": "Terminated",
                "receiving_institution": "Fidelity"
            },
            "plan_data": {
                "rollover_method": "Direct rollover available",
                "processing_time": "7-10 business days"
            }
        },
        "max_response_tokens": 1500,
        "total_inquiries_in_ticket": 2
    }
    
    print("Request:")
    print(json.dumps(payload, indent=2))
    print()
    
    try:
        response = httpx.post(
            f"{API_BASE_URL}/api/v1/generate-response",
            headers=HEADERS,
            json=payload,
            timeout=30.0
        )
        response.raise_for_status()
        
        data = response.json()
        
        print("Response:")
        print("-" * 80)
        print(f"Decision: {data['decision']}")
        print(f"Confidence: {data['confidence']}")
        print()
        
        resp = data['response']
        print(f"Outcome: {resp.get('outcome', 'N/A')}")
        print(f"Outcome Reason: {resp.get('outcome_reason', 'N/A')}")
        print()
        
        participant_resp = resp.get('response_to_participant', {})
        print(f"Opening: {participant_resp.get('opening', 'N/A')}")
        print()
        
        key_points = participant_resp.get('key_points', [])
        if key_points:
            print("Key Points:")
            for kp in key_points:
                print(f"  - {kp}")
        
        steps = participant_resp.get('steps', [])
        if steps:
            print("\nSteps:")
            for step in steps:
                print(f"  {step.get('step_number', '?')}. {step.get('action', 'N/A')}")
                if step.get('detail'):
                    print(f"     Detail: {step['detail']}")
        
        warnings = participant_resp.get('warnings', [])
        if warnings:
            print("\nWarnings:")
            for warning in warnings:
                print(f"    ⚠️  {warning}")
        
        questions = resp.get('questions_to_ask', [])
        if questions:
            print("\nQuestions to Ask:")
            for q in questions:
                print(f"  ? {q.get('question', 'N/A')}")
                print(f"    Why: {q.get('why', 'N/A')}")
        
        escalation = resp.get('escalation', {})
        if escalation.get('needed'):
            print(f"\nEscalation Needed: {escalation.get('reason', 'N/A')}")
        
        print("\nGuardrails Applied:")
        for guardrail in resp.get('guardrails_applied', []):
            print(f"  - {guardrail}")
        
        print(f"\nMetadata:")
        print(f"  Chunks used: {data['metadata'].get('chunks_used', 0)}")
        print(f"  Context tokens: {data['metadata'].get('context_tokens', 0)}")
        print(f"  Response tokens: {data['metadata'].get('response_tokens', 0)}")
        print(f"  Request ID: {response.headers.get('X-Request-ID', 'N/A')}")
        
        # Guardar respuesta
        output_file = "test_api_generate_response.json"
        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"\n✅ Response saved to: {output_file}")
        
        return True
    
    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP Error: {e.response.status_code}")
        print(e.response.json())
        return False
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Probar los endpoints de la API"
    )
    
    parser.add_argument(
        "--endpoint",
        choices=["health", "required_data", "generate_response", "all"],
        default="all",
        help="Qué endpoint probar"
    )
    
    parser.add_argument(
        "--url",
        default=API_BASE_URL,
        help=f"URL base de la API (default: {API_BASE_URL})"
    )
    
    args = parser.parse_args()
    
    # Usar la URL especificada
    api_url = args.url
    
    print(f"\n🔗 Testing API at: {api_url}")
    
    # Ejecutar tests
    results = []
    
    if args.endpoint in ["health", "all"]:
        results.append(("health", test_health()))
    
    if args.endpoint in ["required_data", "all"]:
        results.append(("required_data", test_required_data()))
    
    if args.endpoint in ["generate_response", "all"]:
        results.append(("generate_response", test_generate_response()))
    
    # Resumen
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    for name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{name:20s}: {status}")
    
    # Exit code
    all_passed = all(passed for _, passed in results)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

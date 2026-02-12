#!/usr/bin/env python3
"""
Script para probar el RAG Engine.

Este script permite probar los dos endpoints del RAG engine
de manera interactiva.

Uso:
    python scripts/test_rag_engine.py --endpoint required_data
    python scripts/test_rag_engine.py --endpoint generate_response
"""

import sys
import os
import json
import argparse
from pathlib import Path

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_pipeline.rag_engine import RAGEngine
import logging

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)


def test_required_data():
    """Prueba el endpoint required_data."""
    print("\n" + "="*80)
    print("TEST: Required Data Endpoint")
    print("="*80 + "\n")
    
    # Inicializar RAG engine
    engine = RAGEngine()
    
    # Test case
    inquiry = "I want to rollover my remaining 401k balance to Fidelity"
    record_keeper = "LT Trust"
    plan_type = "401(k)"
    topic = "rollover"
    
    print(f"Inquiry: {inquiry}")
    print(f"Record Keeper: {record_keeper}")
    print(f"Plan Type: {plan_type}")
    print(f"Topic: {topic}")
    print()
    
    # Llamar endpoint
    print("Llamando get_required_data()...")
    result = engine.get_required_data(
        inquiry=inquiry,
        record_keeper=record_keeper,
        plan_type=plan_type,
        topic=topic
    )
    
    # Mostrar resultados
    print("\n" + "-"*80)
    print("RESULTADOS")
    print("-"*80 + "\n")
    
    print(f"Confidence: {result.confidence}")
    print(f"Article: {result.article_reference.get('title', 'N/A')}")
    print()
    
    print("Participant Data Fields:")
    for field in result.required_fields.get('participant_data', []):
        print(f"  - {field['field']}")
        print(f"    Description: {field['description']}")
        print(f"    Why needed: {field['why_needed']}")
        print(f"    Required: {field['required']}")
        print()
    
    print("Plan Data Fields:")
    for field in result.required_fields.get('plan_data', []):
        print(f"  - {field['field']}")
        print(f"    Description: {field['description']}")
        print(f"    Why needed: {field['why_needed']}")
        print(f"    Required: {field['required']}")
        print()
    
    print("Metadata:")
    print(f"  Chunks used: {result.metadata.get('chunks_used', 0)}")
    print(f"  Tokens used: {result.metadata.get('tokens_used', 0)}")
    print()
    
    # Guardar resultado
    output_file = "test_required_data_output.json"
    with open(output_file, 'w') as f:
        json.dump(result.__dict__, f, indent=2, default=str)
    
    print(f"✅ Resultado guardado en: {output_file}")


def test_generate_response():
    """Prueba el endpoint generate_response."""
    print("\n" + "="*80)
    print("TEST: Generate Response Endpoint")
    print("="*80 + "\n")
    
    # Inicializar RAG engine
    engine = RAGEngine()
    
    # Test case
    inquiry = "How do I complete a rollover of my remaining balance?"
    record_keeper = "LT Trust"
    plan_type = "401(k)"
    topic = "rollover"
    
    # Datos recolectados (simulados)
    collected_data = {
        "participant_data": {
            "current_balance": "$1,993.84",
            "employment_status": "Terminated",
            "receiving_institution": "Fidelity"
        },
        "plan_data": {
            "rollover_method": "Direct rollover available",
            "processing_time": "7-10 business days"
        }
    }
    
    print(f"Inquiry: {inquiry}")
    print(f"Record Keeper: {record_keeper}")
    print(f"Plan Type: {plan_type}")
    print(f"Topic: {topic}")
    print()
    print("Collected Data:")
    print(json.dumps(collected_data, indent=2))
    print()
    
    # Llamar endpoint
    print("Llamando generate_response()...")
    result = engine.generate_response(
        inquiry=inquiry,
        record_keeper=record_keeper,
        plan_type=plan_type,
        topic=topic,
        collected_data=collected_data,
        max_response_tokens=1500,  # 2 inquiries → 1500 tokens
        total_inquiries_in_ticket=2
    )
    
    # Mostrar resultados
    print("\n" + "-"*80)
    print("RESULTADOS")
    print("-"*80 + "\n")
    
    print(f"Decision: {result.decision}")
    print(f"Confidence: {result.confidence}")
    print()
    
    resp = result.response
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
            print(f"  ⚠️  {warning}")
    
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
    
    print("\nMetadata:")
    print(f"  Chunks used: {result.metadata.get('chunks_used', 0)}")
    print(f"  Context tokens: {result.metadata.get('context_tokens', 0)}")
    print(f"  Response tokens: {result.metadata.get('response_tokens', 0)}")
    print()
    
    # Guardar resultado
    output_file = "test_generate_response_output.json"
    with open(output_file, 'w') as f:
        json.dump(result.__dict__, f, indent=2, default=str)
    
    print(f"✅ Resultado guardado en: {output_file}")


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Probar el RAG Engine"
    )
    
    parser.add_argument(
        "--endpoint",
        choices=["required_data", "generate_response", "both"],
        default="both",
        help="Qué endpoint probar"
    )
    
    args = parser.parse_args()
    
    try:
        if args.endpoint in ["required_data", "both"]:
            test_required_data()
        
        if args.endpoint in ["generate_response", "both"]:
            test_generate_response()
        
        print("\n" + "="*80)
        print("✅ TESTS COMPLETADOS")
        print("="*80 + "\n")
    
    except Exception as e:
        logger.error(f"Error en tests: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

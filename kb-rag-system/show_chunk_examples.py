#!/usr/bin/env python3
"""
Muestra ejemplos de chunks generados sin interacción.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data_pipeline.article_processor import ArticleProcessor
from data_pipeline.chunking import KBChunker


def main():
    # Cargar artículo
    article_path = "../Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
    
    processor = ArticleProcessor()
    article = processor.load_article(article_path)
    
    # Generar chunks
    chunker = KBChunker()
    chunks = chunker.chunk_article(article)
    
    print(f"\n{'='*80}")
    print(f"CHUNKS GENERADOS: {len(chunks)}")
    print(f"{'='*80}\n")
    
    # Mostrar resumen por tier
    by_tier = {}
    for chunk in chunks:
        tier = chunk["metadata"]["chunk_tier"]
        by_tier[tier] = by_tier.get(tier, 0) + 1
    
    print("RESUMEN POR TIER:")
    for tier in ["critical", "high", "medium", "low"]:
        count = by_tier.get(tier, 0)
        print(f"  {tier.upper()}: {count} chunks")
    
    print(f"\n{'='*80}")
    print("EJEMPLOS DE CHUNKS CRÍTICOS (Tier: critical)")
    print(f"{'='*80}\n")
    
    # Mostrar 3 chunks críticos como ejemplo
    critical_chunks = [c for c in chunks if c["metadata"]["chunk_tier"] == "critical"]
    
    for i, chunk in enumerate(critical_chunks[:3]):
        print(f"\n{'─'*80}")
        print(f"CHUNK #{i+1}: {chunk['metadata']['chunk_type']} - {chunk['metadata']['chunk_category']}")
        print(f"{'─'*80}")
        print(f"ID: {chunk['id']}")
        print(f"Topics: {', '.join(chunk['metadata']['specific_topics'])}")
        print(f"Tamaño: {len(chunk['content'])} caracteres\n")
        print("CONTENIDO:")
        print(chunk['content'][:600] + "..." if len(chunk['content']) > 600 else chunk['content'])
    
    print(f"\n{'='*80}")
    print("METADATA DE UN CHUNK (ejemplo completo)")
    print(f"{'='*80}\n")
    
    # Mostrar metadata completa de un chunk
    print(json.dumps(chunks[0]['metadata'], indent=2))
    
    print(f"\n{'='*80}")
    print("✅ Chunking completado exitosamente")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()

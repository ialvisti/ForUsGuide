#!/usr/bin/env python3
"""
Script para verificar que un artículo se haya subido correctamente a Pinecone.

Este script busca todos los chunks de un artículo en Pinecone y muestra
estadísticas sobre ellos.

Uso:
    python scripts/verify_article.py <article_id>
    
Ejemplo:
    python scripts/verify_article.py "lt_request_401k_termination_withdrawal_or_rollover"
"""

import sys
import os
import argparse
import logging
from pathlib import Path
from collections import Counter

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_pipeline.pinecone_uploader import PineconeUploader

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)


def verify_article(article_id: str, show_details: bool = False) -> bool:
    """
    Verifica que un artículo esté en Pinecone.
    
    Args:
        article_id: ID del artículo a verificar
        show_details: Si True, muestra detalles de cada chunk
    
    Returns:
        True si el artículo se encontró correctamente
    """
    try:
        # Conectar a Pinecone
        uploader = PineconeUploader()
        
        # Buscar chunks del artículo
        logger.info(f"🔍 Buscando chunks para artículo: {article_id}")
        chunks = uploader.get_article_chunks(article_id)
        
        if not chunks:
            logger.error(f"❌ No se encontraron chunks para el artículo: {article_id}")
            return False
        
        # Analizar chunks
        print("\n" + "="*80)
        print(f"ARTÍCULO: {article_id}")
        print("="*80 + "\n")
        
        # Estadísticas básicas
        print(f"✅ Total de chunks encontrados: {len(chunks)}")
        print()
        
        # Contar por tier
        tiers = [chunk['metadata'].get('chunk_tier', 'unknown') for chunk in chunks]
        tier_counts = Counter(tiers)
        
        print("📊 Distribución por tier:")
        for tier in ['critical', 'high', 'medium', 'low']:
            count = tier_counts.get(tier, 0)
            if count > 0:
                percentage = (count / len(chunks)) * 100
                print(f"   {tier.upper():8s}: {count:3d} chunks ({percentage:5.1f}%)")
        print()
        
        # Contar por tipo
        types = [chunk['metadata'].get('chunk_type', 'unknown') for chunk in chunks]
        type_counts = Counter(types)
        
        print("📋 Distribución por tipo:")
        for chunk_type, count in type_counts.most_common():
            percentage = (count / len(chunks)) * 100
            print(f"   {chunk_type:20s}: {count:3d} chunks ({percentage:5.1f}%)")
        print()
        
        # Metadata común
        if chunks:
            first_chunk = chunks[0]['metadata']
            print("ℹ️  Metadata del artículo:")
            print(f"   Título: {first_chunk.get('article_title', 'N/A')}")
            print(f"   Record keeper: {first_chunk.get('record_keeper', 'N/A')}")
            print(f"   Plan type: {first_chunk.get('plan_type', 'N/A')}")
            print(f"   Topic: {first_chunk.get('topic', 'N/A')}")
            print()
        
        # Verificar scores
        scores = [chunk['score'] for chunk in chunks]
        if scores:
            avg_score = sum(scores) / len(scores)
            max_score = max(scores)
            min_score = min(scores)
            
            print("📈 Scores (similarity con query vacío):")
            print(f"   Promedio: {avg_score:.4f}")
            print(f"   Máximo: {max_score:.4f}")
            print(f"   Mínimo: {min_score:.4f}")
            print()
        
        # Mostrar detalles de chunks si se solicita
        if show_details:
            print("\n" + "="*80)
            print("DETALLES DE CHUNKS")
            print("="*80 + "\n")
            
            for i, chunk in enumerate(chunks, 1):
                print(f"\n--- Chunk {i}/{len(chunks)} ---")
                print(f"ID: {chunk['id']}")
                print(f"Score: {chunk['score']:.4f}")
                print(f"Tier: {chunk['metadata'].get('chunk_tier', 'unknown')}")
                print(f"Type: {chunk['metadata'].get('chunk_type', 'unknown')}")
                print(f"Category: {chunk['metadata'].get('chunk_category', 'N/A')}")
                
                # Mostrar topics específicos si existen
                specific_topics = chunk['metadata'].get('specific_topics', [])
                if specific_topics:
                    print(f"Topics: {', '.join(specific_topics)}")
        
        # Validaciones
        print("\n" + "="*80)
        print("VALIDACIONES")
        print("="*80 + "\n")
        
        # Verificar que todos tengan el mismo article_id
        article_ids = set(chunk['metadata'].get('article_id') for chunk in chunks)
        if len(article_ids) == 1:
            print("✅ Todos los chunks tienen el mismo article_id")
        else:
            print(f"⚠️  Encontrados {len(article_ids)} article_ids diferentes: {article_ids}")
        
        # Verificar que haya chunks CRITICAL
        critical_count = tier_counts.get('critical', 0)
        if critical_count > 0:
            print(f"✅ Chunks CRITICAL encontrados: {critical_count}")
        else:
            print("⚠️  No se encontraron chunks CRITICAL")
        
        # Verificar que haya chunks HIGH
        high_count = tier_counts.get('high', 0)
        if high_count > 0:
            print(f"✅ Chunks HIGH encontrados: {high_count}")
        
        print("\n" + "="*80)
        print("✅ VERIFICACIÓN COMPLETADA")
        print("="*80 + "\n")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Error verificando artículo: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Verificar que un artículo esté en Pinecone"
    )
    
    parser.add_argument(
        "article_id",
        help="ID del artículo a verificar"
    )
    
    parser.add_argument(
        "--details",
        action="store_true",
        help="Mostrar detalles de cada chunk"
    )
    
    args = parser.parse_args()
    
    # Verificar artículo
    success = verify_article(args.article_id, show_details=args.details)
    
    # Exit code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

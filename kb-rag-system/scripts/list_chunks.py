#!/usr/bin/env python3
"""
Script para listar y visualizar todos los chunks en Pinecone.

Este script permite:
- Ver todos los chunks con su contenido completo y metadata
- Filtrar por article_id, tier, tipo, etc.
- Exportar a JSON o ver en terminal

Uso:
    # Ver todos los chunks
    python scripts/list_chunks.py
    
    # Ver chunks de un artículo específico
    python scripts/list_chunks.py --article-id "forusall_401k_hardship_withdrawal_complete_guide"
    
    # Filtrar por tier
    python scripts/list_chunks.py --tier critical
    
    # Filtrar por tipo
    python scripts/list_chunks.py --type business_rules
    
    # Limitar resultados
    python scripts/list_chunks.py --limit 10
    
    # Ver solo metadata (sin contenido)
    python scripts/list_chunks.py --metadata-only
    
    # Exportar a JSON
    python scripts/list_chunks.py --output chunks.json
"""

import sys
import os
import json
import argparse
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_pipeline.pinecone_uploader import PineconeUploader

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)


def list_chunks(
    article_id: Optional[str] = None,
    tier: Optional[str] = None,
    chunk_type: Optional[str] = None,
    limit: Optional[int] = None,
    metadata_only: bool = False,
    output_file: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Lista chunks de Pinecone con filtros opcionales.
    
    Args:
        article_id: Filtrar por article_id específico
        tier: Filtrar por tier (critical, high, medium, low)
        chunk_type: Filtrar por tipo de chunk
        limit: Limitar número de resultados
        metadata_only: Solo mostrar metadata, sin contenido
        output_file: Si se especifica, exporta a JSON
    
    Returns:
        Lista de chunks encontrados
    """
    try:
        # Conectar a Pinecone
        uploader = PineconeUploader()
        
        # Construir filtro
        filter_dict = {}
        
        if article_id:
            filter_dict["article_id"] = {"$eq": article_id}
            logger.info(f"🔍 Filtrando por article_id: {article_id}")
        
        if tier:
            filter_dict["chunk_tier"] = {"$eq": tier}
            logger.info(f"🔍 Filtrando por tier: {tier}")
        
        if chunk_type:
            filter_dict["chunk_type"] = {"$eq": chunk_type}
            logger.info(f"🔍 Filtrando por tipo: {chunk_type}")
        
        # Hacer query
        top_k = limit if limit else 1000
        
        logger.info(f"📊 Consultando Pinecone (top_k={top_k})...")
        
        if filter_dict:
            chunks = uploader.query_chunks(
                query_text="all chunks",
                top_k=top_k,
                filter_dict=filter_dict
            )
        else:
            # Sin filtros, obtener todos
            chunks = uploader.query_chunks(
                query_text="all chunks",
                top_k=top_k
            )
        
        if not chunks:
            logger.warning("No se encontraron chunks")
            return []
        
        logger.info(f"✅ Encontrados {len(chunks)} chunks")
        
        # Si hay output file, exportar a JSON
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(chunks, f, indent=2, ensure_ascii=False)
            logger.info(f"💾 Chunks exportados a: {output_file}")
            return chunks
        
        # Mostrar en terminal
        print("\n" + "="*80)
        print(f"CHUNKS EN PINECONE ({len(chunks)} encontrados)")
        print("="*80 + "\n")
        
        for i, chunk in enumerate(chunks, 1):
            print(f"\n{'='*80}")
            print(f"CHUNK {i}/{len(chunks)}")
            print(f"{'='*80}\n")
            
            # ID y score
            print(f"📌 ID: {chunk['id']}")
            print(f"📊 Score: {chunk['score']:.4f}")
            print()
            
            # Metadata
            metadata = chunk.get('metadata', {})
            
            print("📋 METADATA:")
            print(f"   Article ID: {metadata.get('article_id', 'N/A')}")
            print(f"   Article Title: {metadata.get('article_title', 'N/A')}")
            print(f"   Record Keeper: {metadata.get('record_keeper', 'N/A')}")
            print(f"   Plan Type: {metadata.get('plan_type', 'N/A')}")
            print(f"   Topic: {metadata.get('topic', 'N/A')}")
            print(f"   Chunk Tier: {metadata.get('chunk_tier', 'N/A').upper()}")
            print(f"   Chunk Type: {metadata.get('chunk_type', 'N/A')}")
            print(f"   Chunk Category: {metadata.get('chunk_category', 'N/A')}")
            
            # Specific topics si existen
            specific_topics = metadata.get('specific_topics', [])
            if specific_topics:
                print(f"   Specific Topics: {', '.join(specific_topics)}")
            
            # Tags si existen
            tags = metadata.get('tags', [])
            if tags:
                print(f"   Tags: {', '.join(tags)}")
            
            print()
            
            # Contenido (si no es metadata-only)
            if not metadata_only:
                content = metadata.get('content', 'N/A')
                print("📝 CONTENIDO:")
                print("-" * 80)
                print(content)
                print("-" * 80)
            
            # Separator
            if i < len(chunks):
                print()
        
        print("\n" + "="*80)
        print(f"✅ MOSTRANDO {len(chunks)} CHUNKS")
        print("="*80 + "\n")
        
        return chunks
        
    except Exception as e:
        logger.error(f"❌ Error listando chunks: {e}")
        import traceback
        traceback.print_exc()
        return []


def get_stats(uploader: PineconeUploader):
    """
    Muestra estadísticas del índice.
    """
    try:
        stats = uploader.get_index_stats()
        
        print("\n" + "="*80)
        print("ESTADÍSTICAS DEL ÍNDICE")
        print("="*80 + "\n")
        
        print(f"📊 Total de vectores: {stats.get('total_vectors', 0)}")
        print()
        
        namespaces = stats.get('namespaces', {})
        if namespaces:
            print("📁 Namespaces:")
            for ns_name, ns_stats in namespaces.items():
                count = ns_stats.get('vector_count', 0) if isinstance(ns_stats, dict) else ns_stats
                print(f"   {ns_name}: {count} vectores")
        
        print("\n" + "="*80 + "\n")
        
    except Exception as e:
        logger.error(f"Error obteniendo stats: {e}")


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Listar y visualizar chunks en Pinecone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Ver todos los chunks
  python scripts/list_chunks.py
  
  # Ver chunks de un artículo
  python scripts/list_chunks.py --article-id "article_id_here"
  
  # Filtrar por tier critical
  python scripts/list_chunks.py --tier critical
  
  # Filtrar por tipo y limitar a 5
  python scripts/list_chunks.py --type business_rules --limit 5
  
  # Ver solo metadata
  python scripts/list_chunks.py --metadata-only
  
  # Exportar a JSON
  python scripts/list_chunks.py --output chunks.json
  
  # Ver estadísticas del índice
  python scripts/list_chunks.py --stats
        """
    )
    
    parser.add_argument(
        "--article-id",
        help="Filtrar por article_id específico"
    )
    
    parser.add_argument(
        "--tier",
        choices=["critical", "high", "medium", "low"],
        help="Filtrar por tier"
    )
    
    parser.add_argument(
        "--type",
        help="Filtrar por tipo de chunk"
    )
    
    parser.add_argument(
        "--limit",
        type=int,
        help="Limitar número de resultados"
    )
    
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Solo mostrar metadata, sin contenido"
    )
    
    parser.add_argument(
        "--output",
        help="Exportar resultados a archivo JSON"
    )
    
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Mostrar estadísticas del índice"
    )
    
    args = parser.parse_args()
    
    # Si solo se piden stats
    if args.stats:
        uploader = PineconeUploader()
        get_stats(uploader)
        sys.exit(0)
    
    # Listar chunks
    chunks = list_chunks(
        article_id=args.article_id,
        tier=args.tier,
        chunk_type=args.type,
        limit=args.limit,
        metadata_only=args.metadata_only,
        output_file=args.output
    )
    
    # Exit code
    sys.exit(0 if chunks or args.output else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Script para procesar un solo artículo JSON y subirlo a Pinecone.

Este script:
1. Carga el artículo JSON
2. Genera chunks con metadata
3. Sube los chunks a Pinecone

Uso:
    python scripts/process_single_article.py <path-to-json>
    
    # Con opciones
    python scripts/process_single_article.py <path> --dry-run
    python scripts/process_single_article.py <path> --show-chunks
"""

import sys
import os
import argparse
import logging
from pathlib import Path
from dotenv import load_dotenv

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Cargar variables de entorno desde .env
load_dotenv(Path(__file__).parent.parent / ".env")

from data_pipeline.article_processor import load_article_from_path
from data_pipeline.chunking import generate_chunks_from_article
from data_pipeline.pinecone_uploader import PineconeUploader

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)


def process_article(
    article_path: str,
    dry_run: bool = False,
    show_chunks: bool = False
) -> bool:
    """
    Procesa un artículo y lo sube a Pinecone.
    
    Args:
        article_path: Path al archivo JSON del artículo
        dry_run: Si True, no sube a Pinecone
        show_chunks: Si True, muestra chunks generados
    
    Returns:
        True si fue exitoso
    """
    try:
        # 1. Cargar artículo
        logger.info(f"📄 Cargando artículo: {article_path}")
        article = load_article_from_path(article_path)
        
        logger.info(f"✅ Artículo cargado:")
        logger.info(f"   ID: {article['metadata']['article_id']}")
        logger.info(f"   Título: {article['metadata']['title']}")
        logger.info(f"   Record keeper: {article['metadata']['record_keeper']}")
        logger.info(f"   Plan type: {article['metadata']['plan_type']}")
        
        # 2. Generar chunks
        logger.info(f"🔨 Generando chunks...")
        chunks = generate_chunks_from_article(article)
        
        # Contar chunks por tier
        tier_counts = {}
        for chunk in chunks:
            tier = chunk['metadata'].get('chunk_tier', 'unknown')
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        
        logger.info(f"✅ {len(chunks)} chunks generados")
        logger.info(f"   Por tier:")
        for tier in ['critical', 'high', 'medium', 'low']:
            count = tier_counts.get(tier, 0)
            if count > 0:
                logger.info(f"     {tier.upper()}: {count} chunks")
        
        # Mostrar chunks si se solicita
        if show_chunks:
            print("\n" + "="*80)
            print("CHUNKS GENERADOS")
            print("="*80 + "\n")
            
            for i, chunk in enumerate(chunks, 1):
                print(f"\n--- Chunk {i}/{len(chunks)} ---")
                print(f"ID: {chunk['id']}")
                print(f"Tier: {chunk['metadata']['chunk_tier']}")
                print(f"Type: {chunk['metadata']['chunk_type']}")
                print(f"Content preview: {chunk['content'][:200]}...")
                print()
        
        # 3. Subir a Pinecone (si no es dry-run)
        if dry_run:
            logger.info("🏜️  Dry-run: No se subirán chunks a Pinecone")
            return True
        
        logger.info(f"📤 Subiendo chunks a Pinecone...")
        uploader = PineconeUploader()
        
        result = uploader.upload_chunks(chunks, show_progress=True)
        
        print("\n" + "="*80)
        print("PROCESAMIENTO DE ARTÍCULO")
        print("="*80 + "\n")
        
        if result['failed'] > 0:
            logger.warning(f"⚠️  {result['failed']} chunks fallaron")
            return False
        else:
            logger.info(f"✅ Todos los chunks se subieron exitosamente")
            print("\n" + "="*80)
            print("✅ PROCESAMIENTO COMPLETADO")
            print("="*80 + "\n")
            print("ℹ️  NOTA: Pinecone usa consistencia eventual. Los vectores estarán disponibles")
            print("   para búsquedas en ~10-15 segundos.")
            print()
            print("Próximos pasos:")
            print(f'  Verificar: python kb-rag-system/scripts/verify_article.py "{article["metadata"]["article_id"]}"')
            print("  (El script esperará automáticamente si los vectores aún no están indexados)")
            print()
            return True
        
    except FileNotFoundError:
        logger.error(f"❌ Archivo no encontrado: {article_path}")
        return False
    except Exception as e:
        logger.error(f"❌ Error procesando artículo: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Procesar un artículo JSON y subirlo a Pinecone"
    )
    
    parser.add_argument(
        "article_path",
        help="Path al archivo JSON del artículo"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No subir a Pinecone (solo procesar)"
    )
    
    parser.add_argument(
        "--show-chunks",
        action="store_true",
        help="Mostrar chunks generados"
    )
    
    args = parser.parse_args()
    
    # Procesar artículo
    success = process_article(
        args.article_path,
        dry_run=args.dry_run,
        show_chunks=args.show_chunks
    )
    
    # Exit code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

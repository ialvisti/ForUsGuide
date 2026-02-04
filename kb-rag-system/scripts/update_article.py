#!/usr/bin/env python3
"""
Script para actualizar un artículo en Pinecone.

Este script:
1. Lee el article_id del archivo JSON
2. Busca y borra la versión vieja en Pinecone
3. Procesa el artículo actualizado
4. Sube los nuevos chunks

Uso:
    python scripts/update_article.py <path-to-json>
    
    # Con opciones
    python scripts/update_article.py <path> --dry-run
    python scripts/update_article.py <path> --skip-confirmation
    python scripts/update_article.py <path> --show-chunks

Ejemplo:
    python scripts/update_article.py "Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
"""

import sys
import os
import json
import argparse
import logging
from pathlib import Path

# Agregar parent directory al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_pipeline.article_processor import load_article_from_path
from data_pipeline.chunking import generate_chunks_from_article
from data_pipeline.pinecone_uploader import PineconeUploader

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def confirm_update(article_id: str, old_chunk_count: int) -> bool:
    """
    Pide confirmación al usuario antes de actualizar.
    
    Args:
        article_id: ID del artículo a actualizar
        old_chunk_count: Número de chunks viejos que se borrarán
    
    Returns:
        True si el usuario confirma, False si no
    """
    print(f"\n⚠️  CONFIRMACIÓN DE ACTUALIZACIÓN:")
    print(f"   Article ID: {article_id}")
    print(f"   Chunks viejos a borrar: {old_chunk_count}")
    print(f"   Se procesará y subirá la versión actualizada")
    print(f"\n   Esta acción borrará la versión vieja.\n")
    
    response = input("¿Continuar? (escribe 'si' para confirmar): ").strip().lower()
    return response in ['si', 'sí', 'yes', 'y']


def update_article(
    article_path: str,
    dry_run: bool = False,
    skip_confirmation: bool = False,
    show_chunks: bool = False
) -> bool:
    """
    Actualiza un artículo en Pinecone (borra viejo y sube nuevo).
    
    Args:
        article_path: Path al archivo JSON del artículo actualizado
        dry_run: Si True, no hace cambios en Pinecone
        skip_confirmation: Si True, no pide confirmación
        show_chunks: Si True, muestra chunks generados
    
    Returns:
        True si fue exitoso
    """
    try:
        # ====================================================================
        # PASO 1: Cargar y validar el artículo nuevo
        # ====================================================================
        print("\n" + "=" * 80)
        print("  PASO 1: CARGAR ARTÍCULO ACTUALIZADO")
        print("=" * 80 + "\n")
        
        logger.info(f"📄 Cargando artículo: {article_path}")
        
        # Verificar que el archivo existe
        if not os.path.exists(article_path):
            logger.error(f"❌ Archivo no encontrado: {article_path}")
            return False
        
        # Cargar artículo
        article = load_article_from_path(article_path)
        article_id = article['metadata']['article_id']
        
        print(f"✅ Artículo cargado:")
        print(f"   ID: {article_id}")
        print(f"   Título: {article['metadata']['title']}")
        print(f"   Record keeper: {article['metadata']['record_keeper']}")
        print(f"   Plan type: {article['metadata']['plan_type']}")
        
        # ====================================================================
        # PASO 2: Buscar versión vieja en Pinecone
        # ====================================================================
        print("\n" + "=" * 80)
        print("  PASO 2: BUSCAR VERSIÓN VIEJA EN PINECONE")
        print("=" * 80 + "\n")
        
        logger.info(f"🔍 Buscando versión vieja en Pinecone...")
        uploader = PineconeUploader()
        
        old_chunks = uploader.get_article_chunks(article_id)
        
        if not old_chunks:
            logger.warning(f"⚠️  No se encontró versión vieja del artículo en Pinecone")
            logger.info(f"   El artículo se procesará como nuevo")
            has_old_version = False
        else:
            print(f"✅ Versión vieja encontrada:")
            print(f"   Chunks existentes: {len(old_chunks)}")
            
            # Mostrar info de la versión vieja
            if old_chunks:
                first_chunk = old_chunks[0]
                old_metadata = first_chunk.get('metadata', {})
                print(f"   Título: {old_metadata.get('article_title', 'N/A')}")
            
            has_old_version = True
        
        # ====================================================================
        # PASO 3: Generar nuevos chunks
        # ====================================================================
        print("\n" + "=" * 80)
        print("  PASO 3: GENERAR NUEVOS CHUNKS")
        print("=" * 80 + "\n")
        
        logger.info(f"🔨 Generando chunks desde artículo actualizado...")
        new_chunks = generate_chunks_from_article(article)
        
        # Contar chunks por tier
        tier_counts = {}
        for chunk in new_chunks:
            tier = chunk['metadata'].get('chunk_tier', 'unknown')
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        
        print(f"✅ {len(new_chunks)} chunks generados")
        print(f"   Distribución por tier:")
        for tier in ['critical', 'high', 'medium', 'low']:
            count = tier_counts.get(tier, 0)
            if count > 0:
                print(f"     {tier.upper()}: {count} chunks")
        
        # Mostrar comparación
        if has_old_version:
            print(f"\n📊 Comparación:")
            print(f"   Chunks viejos: {len(old_chunks)}")
            print(f"   Chunks nuevos: {len(new_chunks)}")
            delta = len(new_chunks) - len(old_chunks)
            if delta > 0:
                print(f"   Cambio: +{delta} chunks")
            elif delta < 0:
                print(f"   Cambio: {delta} chunks")
            else:
                print(f"   Cambio: mismo número de chunks")
        
        # Mostrar chunks si se solicita
        if show_chunks:
            print("\n" + "=" * 80)
            print("NUEVOS CHUNKS GENERADOS")
            print("=" * 80 + "\n")
            
            for i, chunk in enumerate(new_chunks, 1):
                print(f"\n--- Chunk {i}/{len(new_chunks)} ---")
                print(f"ID: {chunk['id']}")
                print(f"Tier: {chunk['metadata']['chunk_tier']}")
                print(f"Type: {chunk['metadata']['chunk_type']}")
                print(f"Content preview: {chunk['content'][:150]}...")
        
        # ====================================================================
        # PASO 4: Pedir confirmación (si no se saltó)
        # ====================================================================
        if has_old_version and not skip_confirmation and not dry_run:
            if not confirm_update(article_id, len(old_chunks)):
                logger.info("❌ Operación cancelada por el usuario")
                return False
        
        # ====================================================================
        # PASO 5: Borrar versión vieja (si existe)
        # ====================================================================
        if has_old_version:
            print("\n" + "=" * 80)
            print("  PASO 4: BORRAR VERSIÓN VIEJA")
            print("=" * 80 + "\n")
            
            if dry_run:
                logger.info("🏜️  Dry-run: No se borrará la versión vieja")
            else:
                logger.info(f"🗑️  Borrando {len(old_chunks)} chunks viejos...")
                
                success = uploader.delete_chunks(
                    filter_dict={"article_id": {"$eq": article_id}}
                )
                
                if not success:
                    logger.error(f"❌ Error al borrar versión vieja")
                    return False
                
                # Verificar que se borró
                verification = uploader.get_article_chunks(article_id)
                if len(verification) == 0:
                    logger.info(f"✅ Versión vieja borrada exitosamente")
                else:
                    logger.warning(f"⚠️  Algunos chunks no se borraron ({len(verification)} restantes)")
        
        # ====================================================================
        # PASO 6: Subir nueva versión
        # ====================================================================
        print("\n" + "=" * 80)
        print(f"  PASO {'5' if has_old_version else '4'}: SUBIR NUEVA VERSIÓN")
        print("=" * 80 + "\n")
        
        if dry_run:
            logger.info("🏜️  Dry-run: No se subirán los chunks nuevos")
            print("\n✅ Dry-run completado (no se hicieron cambios en Pinecone)")
            return True
        
        logger.info(f"📤 Subiendo {len(new_chunks)} chunks nuevos a Pinecone...")
        
        result = uploader.upload_chunks(new_chunks, show_progress=True)
        
        if result['failed'] > 0:
            logger.warning(f"⚠️  {result['failed']} chunks fallaron al subir")
            return False
        
        # ====================================================================
        # PASO 7: Verificar y mostrar resumen
        # ====================================================================
        print("\n" + "=" * 80)
        print("  VERIFICACIÓN FINAL")
        print("=" * 80 + "\n")
        
        logger.info(f"🔍 Verificando artículo en Pinecone...")
        final_chunks = uploader.get_article_chunks(article_id)
        
        print(f"✅ Artículo actualizado exitosamente:")
        print(f"   Article ID: {article_id}")
        print(f"   Chunks en Pinecone: {len(final_chunks)}")
        
        # Resumen final
        print("\n" + "=" * 80)
        print("  ✅ ACTUALIZACIÓN COMPLETADA")
        print("=" * 80 + "\n")
        
        if has_old_version:
            print(f"Resumen:")
            print(f"  • Chunks viejos borrados: {len(old_chunks)}")
            print(f"  • Chunks nuevos subidos: {len(new_chunks)}")
            print(f"  • Chunks actuales: {len(final_chunks)}")
        else:
            print(f"Resumen:")
            print(f"  • Artículo procesado como nuevo")
            print(f"  • Chunks subidos: {len(new_chunks)}")
        
        print(f"\nPróximos pasos:")
        print(f'  Verificar: python scripts/verify_article.py "{article_id}"')
        print()
        
        return True
    
    except FileNotFoundError:
        logger.error(f"❌ Archivo no encontrado: {article_path}")
        return False
    except KeyError as e:
        logger.error(f"❌ Campo requerido no encontrado en el JSON: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Error actualizando artículo: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Función principal."""
    parser = argparse.ArgumentParser(
        description="Actualizar un artículo en Pinecone (borra viejo y sube nuevo)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Actualizar un artículo (pedirá confirmación)
  python scripts/update_article.py "Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
  
  # Actualizar sin pedir confirmación
  python scripts/update_article.py <path> --skip-confirmation
  
  # Ver qué haría sin hacer cambios
  python scripts/update_article.py <path> --dry-run
  
  # Ver chunks generados
  python scripts/update_article.py <path> --show-chunks
        """
    )
    
    parser.add_argument(
        "article_path",
        help="Path al archivo JSON del artículo actualizado"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No hacer cambios en Pinecone (solo mostrar qué haría)"
    )
    
    parser.add_argument(
        "--skip-confirmation",
        action="store_true",
        help="No pedir confirmación antes de borrar"
    )
    
    parser.add_argument(
        "--show-chunks",
        action="store_true",
        help="Mostrar chunks generados"
    )
    
    args = parser.parse_args()
    
    # Banner
    print("\n" + "=" * 80)
    print("  ACTUALIZAR ARTÍCULO EN PINECONE")
    print("=" * 80)
    
    # Actualizar artículo
    success = update_article(
        article_path=args.article_path,
        dry_run=args.dry_run,
        skip_confirmation=args.skip_confirmation,
        show_chunks=args.show_chunks
    )
    
    # Exit code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

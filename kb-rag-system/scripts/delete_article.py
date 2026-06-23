#!/usr/bin/env python3
"""
Script para borrar artículos de Pinecone por article_id.

Uso:
    python scripts/delete_article.py <article_id>
    
Ejemplo:
    python scripts/delete_article.py lt_request_401k_termination_withdrawal_or_rollover
"""

import sys
import os
from pathlib import Path

# Agregar el directorio raíz al path
root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))

# Cargar variables de entorno desde .env (PINECONE_API_KEY, INDEX_NAME, etc.)
from dotenv import load_dotenv
load_dotenv(root_dir / ".env")

from data_pipeline.pinecone_uploader import PineconeUploader
import logging

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def confirm_deletion(article_id: str, chunk_count: int) -> bool:
    """
    Pide confirmación al usuario antes de borrar.
    
    Args:
        article_id: ID del artículo a borrar
        chunk_count: Número de chunks que se borrarán
    
    Returns:
        True si el usuario confirma, False si no
    """
    print(f"\n⚠️  ADVERTENCIA: Estás a punto de borrar el artículo:")
    print(f"   Article ID: {article_id}")
    print(f"   Chunks a borrar: {chunk_count}")
    print(f"\n   Esta acción NO se puede deshacer.\n")
    
    response = input("¿Continuar? (escribe 'si' para confirmar): ").strip().lower()
    return response in ['si', 'sí', 'yes', 'y']


def delete_article_by_id(article_id: str, skip_confirmation: bool = False) -> bool:
    """
    Borra un artículo de Pinecone por su article_id.
    
    Args:
        article_id: ID del artículo a borrar
        skip_confirmation: Si True, no pide confirmación (usar con cuidado)
    
    Returns:
        True si se borró exitosamente, False si hubo error
    """
    try:
        # Inicializar uploader
        logger.info(f"Conectando a Pinecone...")
        uploader = PineconeUploader()
        
        # Verificar que el artículo existe
        logger.info(f"Buscando artículo: {article_id}")
        chunks_before = uploader.get_article_chunks(article_id)
        
        if not chunks_before:
            logger.warning(f"❌ No se encontraron chunks para article_id: {article_id}")
            logger.info(f"   Verifica que el article_id sea correcto")
            return False
        
        logger.info(f"✅ Artículo encontrado: {len(chunks_before)} chunks")
        
        # Mostrar información del artículo
        if chunks_before:
            first_chunk = chunks_before[0]
            metadata = first_chunk.get('metadata', {})
            print(f"\n📄 Información del artículo:")
            print(f"   Article ID: {metadata.get('article_id', 'N/A')}")
            print(f"   Título: {metadata.get('article_title', 'N/A')}")
            print(f"   Record Keeper: {metadata.get('record_keeper', 'N/A')}")
            print(f"   Plan Type: {metadata.get('plan_type', 'N/A')}")
            print(f"   Total chunks: {len(chunks_before)}")
        
        # Pedir confirmación (a menos que se haya saltado)
        if not skip_confirmation:
            if not confirm_deletion(article_id, len(chunks_before)):
                logger.info("❌ Operación cancelada por el usuario")
                return False
        
        # Borrar el artículo
        logger.info(f"🗑️  Borrando artículo...")
        success = uploader.delete_chunks(
            filter_dict={"article_id": {"$eq": article_id}}
        )
        
        if not success:
            logger.error(f"❌ Error al borrar el artículo")
            return False
        
        # Verificar que se borró
        logger.info(f"Verificando eliminación...")
        chunks_after = uploader.get_article_chunks(article_id)
        
        if len(chunks_after) == 0:
            logger.info(f"✅ Artículo borrado exitosamente")
            logger.info(f"   Chunks eliminados: {len(chunks_before)}")
            return True
        else:
            logger.warning(f"⚠️  Algunos chunks no se borraron")
            logger.warning(f"   Chunks restantes: {len(chunks_after)}")
            return False
    
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def list_articles(uploader: PineconeUploader, limit: int = 10):
    """
    Lista algunos artículos disponibles en el índice.
    
    Args:
        uploader: Instancia de PineconeUploader
        limit: Número de artículos únicos a mostrar
    """
    try:
        logger.info("Buscando artículos en el índice...")
        
        # Query genérico para obtener algunos chunks
        chunks = uploader.query_chunks(
            query_text="article",
            top_k=100
        )
        
        # Extraer article_ids únicos
        article_ids = set()
        articles_info = {}
        
        for chunk in chunks:
            metadata = chunk.get('metadata', {})
            article_id = metadata.get('article_id')
            
            if article_id and article_id not in article_ids:
                article_ids.add(article_id)
                articles_info[article_id] = {
                    'title': metadata.get('article_title', 'N/A'),
                    'record_keeper': metadata.get('record_keeper', 'N/A'),
                    'plan_type': metadata.get('plan_type', 'N/A')
                }
                
                if len(article_ids) >= limit:
                    break
        
        if not articles_info:
            print("\n❌ No se encontraron artículos en el índice")
            return
        
        print(f"\n📚 Artículos disponibles (mostrando {len(articles_info)}):\n")
        for article_id, info in list(articles_info.items())[:limit]:
            print(f"  • {article_id}")
            print(f"    Título: {info['title']}")
            print(f"    RK: {info['record_keeper']}, Plan: {info['plan_type']}\n")
    
    except Exception as e:
        logger.error(f"Error listando artículos: {e}")


def main():
    """Función principal."""
    print("=" * 70)
    print("  Script para borrar artículos de Pinecone")
    print("=" * 70)
    
    # Verificar argumentos
    if len(sys.argv) < 2:
        print("\n❌ Error: Debes proporcionar un article_id\n")
        print("Uso:")
        print(f"  python {sys.argv[0]} <article_id>")
        print(f"  python {sys.argv[0]} --list    (para ver artículos disponibles)")
        print("\nEjemplo:")
        print(f"  python {sys.argv[0]} lt_request_401k_termination_withdrawal_or_rollover")
        sys.exit(1)
    
    article_id = sys.argv[1]
    
    # Comando especial para listar artículos
    if article_id in ['--list', '-l', 'list']:
        uploader = PineconeUploader()
        list_articles(uploader, limit=10)
        sys.exit(0)
    
    # Borrar artículo
    success = delete_article_by_id(article_id)
    
    if success:
        print("\n" + "=" * 70)
        print("  ✅ Operación completada exitosamente")
        print("=" * 70 + "\n")
        sys.exit(0)
    else:
        print("\n" + "=" * 70)
        print("  ❌ La operación falló")
        print("=" * 70 + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

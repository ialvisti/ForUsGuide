"""
Módulo para subir chunks a Pinecone.

Este módulo maneja la conexión y carga de chunks a Pinecone,
incluyendo retry logic y batch processing.

IMPORTANTE: Este índice usa embeddings integrados (llama-text-embed-v2),
por lo que el formato de upsert es diferente al tradicional.
"""

import os
import time
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from pinecone import Pinecone
from tqdm import tqdm

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cargar variables de entorno
load_dotenv()


class PineconeUploader:
    """
    Clase para manejar la conexión y upload de chunks a Pinecone.
    
    Este uploader está diseñado para índices con embeddings integrados,
    donde Pinecone genera automáticamente los embeddings del contenido.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        index_name: Optional[str] = None,
        namespace: Optional[str] = None,
        batch_size: int = 96,
        max_retries: int = 3,
        retry_delay: int = 2
    ):
        """
        Inicializa el uploader.
        
        Args:
            api_key: API key de Pinecone (lee de .env si no se provee)
            index_name: Nombre del índice (lee de .env si no se provee)
            namespace: Namespace a usar (lee de .env si no se provee)
            batch_size: Tamaño de batch para uploads (default: 96)
            max_retries: Número máximo de reintentos (default: 3)
            retry_delay: Delay entre reintentos en segundos (default: 2)
        """
        self.api_key = api_key or os.getenv("PINECONE_API_KEY")
        self.index_name = index_name or os.getenv("INDEX_NAME", "kb-articles-production")
        self.namespace = namespace or os.getenv("NAMESPACE", "kb_articles")
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        if not self.api_key:
            raise ValueError("PINECONE_API_KEY no está configurada")
        
        # Inicializar cliente Pinecone
        self.pc = Pinecone(api_key=self.api_key)
        
        # Conectar al índice
        try:
            self.index = self.pc.Index(self.index_name)
            logger.info(f"✅ Conectado a índice: {self.index_name}")
            
            # Obtener stats del índice
            stats = self.index.describe_index_stats()
            logger.info(f"   Namespace: {self.namespace}")
            logger.info(f"   Total vectores: {stats.total_vector_count}")
            
        except Exception as e:
            logger.error(f"Error conectando al índice {self.index_name}: {e}")
            raise
    
    def upload_chunks(
        self,
        chunks: List[Dict[str, Any]],
        show_progress: bool = True
    ) -> Dict[str, int]:
        """
        Sube una lista de chunks a Pinecone.
        
        Args:
            chunks: Lista de chunks con formato:
                {
                    "id": str,
                    "content": str,
                    "metadata": dict
                }
            show_progress: Mostrar barra de progreso
        
        Returns:
            Dict con estadísticas: {"success": int, "failed": int}
        """
        if not chunks:
            logger.warning("No hay chunks para subir")
            return {"success": 0, "failed": 0}
        
        logger.info(f"📤 Subiendo {len(chunks)} chunks a Pinecone...")
        logger.info(f"   Batch size: {self.batch_size}")
        logger.info(f"   Namespace: {self.namespace}")
        
        # Dividir en batches
        batches = [
            chunks[i:i + self.batch_size]
            for i in range(0, len(chunks), self.batch_size)
        ]
        
        success_count = 0
        failed_count = 0
        
        # Procesar batches con progress bar
        iterator = tqdm(batches, desc="Uploading") if show_progress else batches
        
        for batch in iterator:
            success = self._upload_batch(batch)
            
            if success:
                success_count += len(batch)
            else:
                failed_count += len(batch)
        
        logger.info(f"✅ Upload completado")
        logger.info(f"   Exitosos: {success_count}/{len(chunks)}")
        if failed_count > 0:
            logger.warning(f"   Fallidos: {failed_count}/{len(chunks)}")
        
        return {
            "success": success_count,
            "failed": failed_count
        }
    
    def _upload_batch(self, batch: List[Dict[str, Any]]) -> bool:
        """
        Sube un batch de chunks con retry logic.
        
        IMPORTANTE: Para índices con embeddings integrados (model + field_map),
        usar index.upsert_records() en lugar de index.upsert().
        
        El formato de records es un diccionario plano con:
        - "_id": ID del record
        - "content": texto que Pinecone embedirá (debe coincidir con field_map)
        - otros campos de metadata (planos, no nested)
        
        Args:
            batch: Lista de chunks a subir
        
        Returns:
            True si el batch se subió exitosamente, False otherwise
        """
        # Preparar records para Pinecone con embeddings integrados
        # Formato: diccionario plano con _id, content y metadata
        records = []
        for chunk in batch:
            # Crear record plano
            record = {
                "_id": chunk["id"],
                "content": chunk["content"],  # Campo que Pinecone embedirá
                **chunk["metadata"]  # Agregar todos los campos de metadata
            }
            records.append(record)
        
        # Intentar upload con retries
        for attempt in range(self.max_retries):
            try:
                # Upsert usando upsert_records (para embeddings integrados)
                self.index.upsert_records(
                    namespace=self.namespace,
                    records=records
                )
                return True
                
            except Exception as e:
                logger.warning(f"Intento {attempt + 1}/{self.max_retries} falló: {e}")
                
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    logger.error(f"Batch falló después de {self.max_retries} intentos")
                    return False
        
        return False
    
    def delete_chunks(
        self,
        chunk_ids: Optional[List[str]] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
        delete_all: bool = False
    ) -> bool:
        """
        Elimina chunks del índice.
        
        Args:
            chunk_ids: Lista de IDs de chunks a eliminar
            filter_dict: Diccionario de filtros (ej: {"article_id": "..."})
            delete_all: Si True, elimina todos los vectores del namespace
        
        Returns:
            True si fue exitoso
        """
        try:
            if delete_all:
                logger.warning(f"⚠️  Eliminando TODOS los vectores del namespace {self.namespace}")
                self.index.delete(delete_all=True, namespace=self.namespace)
                logger.info("✅ Todos los vectores eliminados")
                
            elif chunk_ids:
                logger.info(f"Eliminando {len(chunk_ids)} chunks...")
                self.index.delete(ids=chunk_ids, namespace=self.namespace)
                logger.info("✅ Chunks eliminados")
                
            elif filter_dict:
                logger.info(f"Eliminando chunks con filtro: {filter_dict}")
                self.index.delete(filter=filter_dict, namespace=self.namespace)
                logger.info("✅ Chunks eliminados")
                
            else:
                logger.warning("No se especificó qué eliminar")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error eliminando chunks: {e}")
            return False
    
    def query_chunks(
        self,
        query_text: str,
        top_k: int = 10,
        filter_dict: Optional[Dict[str, Any]] = None,
        include_metadata: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Busca chunks en Pinecone usando embeddings integrados.
        
        Args:
            query_text: Texto a buscar (Pinecone lo embedirá)
            top_k: Número de resultados
            filter_dict: Filtros a aplicar
            include_metadata: Incluir metadata en resultados
        
        Returns:
            Lista de chunks encontrados
        """
        try:
            # Query usando embeddings integrados con el método search()
            query_params = {
                "top_k": top_k,
                "inputs": {
                    "text": query_text  # Pinecone embedirá esto
                }
            }
            
            # Agregar filtro si existe
            if filter_dict:
                query_params["filter"] = filter_dict
            
            results = self.index.search(
                namespace=self.namespace,
                query=query_params
            )
            
            # Para embeddings integrados, la estructura es diferente:
            # results['result']['hits'] contiene los matches
            chunks = []
            
            if not results:
                logger.warning("No se encontraron resultados")
                return []
            
            # Convertir results a dict si es necesario
            if hasattr(results, 'to_dict'):
                results_dict = results.to_dict()
            else:
                results_dict = results
            
            # Obtener hits de la estructura correcta
            hits = results_dict.get('result', {}).get('hits', [])
            
            if not hits:
                logger.warning("No se encontraron hits en los resultados")
                return []
            
            for hit in hits:
                # Cada hit tiene _id, _score, y fields (metadata)
                chunk = {
                    "id": hit.get('_id'),
                    "score": hit.get('_score', 0.0),
                    "metadata": hit.get('fields', {})
                }
                chunks.append(chunk)
            
            logger.info(f"Query encontró {len(chunks)} chunks")
            return chunks
            
        except Exception as e:
            logger.error(f"Error en query: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def get_article_chunks(
        self,
        article_id: str,
        include_metadata: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Obtiene todos los chunks de un artículo específico.
        
        Args:
            article_id: ID del artículo
            include_metadata: Incluir metadata en resultados
        
        Returns:
            Lista de chunks del artículo
        """
        # Hacer una query genérica con filtro por article_id
        # Pinecone con embeddings integrados requiere text no vacío
        return self.query_chunks(
            query_text="article information",  # Query genérico
            top_k=1000,  # Suficiente para cualquier artículo
            filter_dict={"article_id": {"$eq": article_id}},
            include_metadata=include_metadata
        )
    
    def get_index_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas del índice.
        
        Returns:
            Dict con stats del índice
        """
        try:
            stats = self.index.describe_index_stats()
            
            # Convertir namespaces a dict serializable
            namespaces_dict = {}
            if hasattr(stats, 'namespaces') and stats.namespaces:
                for ns_name, ns_obj in stats.namespaces.items():
                    if hasattr(ns_obj, 'vector_count'):
                        namespaces_dict[ns_name] = {"vector_count": ns_obj.vector_count}
                    else:
                        namespaces_dict[ns_name] = dict(ns_obj) if isinstance(ns_obj, dict) else str(ns_obj)
            
            return {
                "total_vectors": stats.total_vector_count,
                "namespaces": namespaces_dict
            }
        except Exception as e:
            logger.error(f"Error obteniendo stats: {e}")
            return {}


def main():
    """Función de prueba."""
    uploader = PineconeUploader()
    
    # Probar con chunk de ejemplo
    test_chunks = [
        {
            "id": "test_chunk_1",
            "content": "This is a test chunk for Pinecone upload",
            "metadata": {
                "article_id": "test_article",
                "chunk_type": "test",
                "chunk_tier": "low"
            }
        }
    ]
    
    # Upload
    result = uploader.upload_chunks(test_chunks)
    print(f"Upload result: {result}")
    
    # Query
    chunks = uploader.query_chunks("test chunk", top_k=5)
    print(f"Found {len(chunks)} chunks")
    
    # Delete
    uploader.delete_chunks(filter_dict={"article_id": {"$eq": "test_article"}})


if __name__ == "__main__":
    main()

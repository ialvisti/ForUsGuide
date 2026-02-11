"""
Article Processor Module

Lee y procesa artículos JSON de la Knowledge Base.
Valida estructura y extrae información relevante.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ArticleProcessor:
    """Procesa artículos JSON de la Knowledge Base."""
    
    def __init__(self):
        """Inicializa el procesador de artículos."""
        self.required_sections = ["metadata", "details"]
    
    def load_article(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Carga un artículo JSON desde archivo.
        
        Args:
            file_path: Ruta al archivo JSON
            
        Returns:
            Dict con el contenido del artículo o None si falla
        """
        try:
            path = Path(file_path)
            
            if not path.exists():
                logger.error(f"Archivo no encontrado: {file_path}")
                return None
            
            with open(path, 'r', encoding='utf-8') as f:
                article = json.load(f)
            
            # Validar estructura básica
            if not self.validate_article(article):
                logger.error(f"Artículo inválido: {file_path}")
                return None
            
            logger.info(f"Artículo cargado: {article['metadata']['title']}")
            return article
            
        except json.JSONDecodeError as e:
            logger.error(f"Error al parsear JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"Error al cargar artículo: {e}")
            return None
    
    def validate_article(self, article: Dict[str, Any]) -> bool:
        """
        Valida que el artículo tenga la estructura esperada.
        
        Args:
            article: Dict con el artículo
            
        Returns:
            True si es válido, False si no
        """
        # Verificar secciones requeridas
        for section in self.required_sections:
            if section not in article:
                logger.error(f"Sección faltante: {section}")
                return False
        
        # Verificar metadata mínima
        metadata = article.get("metadata", {})
        required_metadata = ["article_id", "title", "record_keeper", "plan_type"]
        
        for field in required_metadata:
            if field not in metadata:
                logger.error(f"Campo de metadata faltante: {field}")
                return False
        
        return True
    
    def get_article_info(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extrae información básica del artículo.
        
        Args:
            article: Dict con el artículo
            
        Returns:
            Dict con información básica
        """
        metadata = article.get("metadata", {})
        
        return {
            "article_id": metadata.get("article_id"),
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "record_keeper": metadata.get("record_keeper"),
            "plan_type": metadata.get("plan_type"),
            "scope": metadata.get("scope"),
            "tags": metadata.get("tags", []),
            "topic": metadata.get("topic"),
            "subtopics": metadata.get("subtopics", [])
        }


# Helper function para uso directo
def load_article_from_path(file_path: str) -> Optional[Dict[str, Any]]:
    """
    Helper function para cargar un artículo desde un path.
    
    Args:
        file_path: Ruta al archivo JSON
        
    Returns:
        Dict con el contenido del artículo o None si falla
    """
    processor = ArticleProcessor()
    return processor.load_article(file_path)

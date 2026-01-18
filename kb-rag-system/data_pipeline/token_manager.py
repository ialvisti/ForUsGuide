"""
Token Manager - Manejo de tokens y presupuestos.

Este módulo maneja el conteo de tokens, truncamiento de contexto,
y cálculo de presupuestos dinámicos.
"""

import tiktoken
from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)

# Modelo por defecto para conteo de tokens
DEFAULT_MODEL = "gpt-4"


class TokenManager:
    """Maneja conteo y presupuesto de tokens."""
    
    def __init__(self, model: str = DEFAULT_MODEL):
        """
        Inicializa el token manager.
        
        Args:
            model: Modelo para conteo de tokens (default: gpt-4)
        """
        self.model = model
        try:
            self.encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fallback si el modelo no está disponible
            self.encoding = tiktoken.get_encoding("cl100k_base")
            logger.warning(f"Modelo {model} no encontrado, usando cl100k_base")
    
    def count_tokens(self, text: str) -> int:
        """
        Cuenta tokens en un texto.
        
        Args:
            text: Texto a contar
        
        Returns:
            Número de tokens
        """
        if not text:
            return 0
        return len(self.encoding.encode(text))
    
    def calculate_context_budget(
        self,
        max_response_tokens: int,
        reserve_for_response: float = 0.3
    ) -> int:
        """
        Calcula cuántos tokens podemos usar para el contexto.
        
        Reserva un porcentaje del presupuesto total para la respuesta del LLM.
        
        Args:
            max_response_tokens: Presupuesto total de tokens
            reserve_for_response: % a reservar para respuesta (default: 30%)
        
        Returns:
            Tokens disponibles para contexto
        """
        context_budget = int(max_response_tokens * (1 - reserve_for_response))
        logger.debug(f"Context budget: {context_budget} tokens (de {max_response_tokens} total)")
        return context_budget
    
    def calculate_dynamic_budget(self, total_inquiries: int) -> int:
        """
        Calcula presupuesto dinámico basado en número de inquiries.
        
        DevRev AI tiene límite de ~4000 tokens total por ticket.
        Dividimos equitativamente entre inquiries con mínimo razonable.
        
        Args:
            total_inquiries: Número total de inquiries en el ticket
        
        Returns:
            Tokens máximos por response
        """
        TOTAL_BUDGET = 4000
        MIN_PER_INQUIRY = 800
        
        if total_inquiries <= 0:
            return 3000  # Default para 1 inquiry
        
        budget_per_inquiry = TOTAL_BUDGET // total_inquiries
        
        # Asegurar mínimo razonable
        budget_per_inquiry = max(budget_per_inquiry, MIN_PER_INQUIRY)
        
        logger.info(f"Dynamic budget: {budget_per_inquiry} tokens para {total_inquiries} inquiries")
        return budget_per_inquiry
    
    def truncate_to_budget(
        self,
        chunks: List[str],
        budget: int,
        preserve_order: bool = True
    ) -> List[str]:
        """
        Trunca lista de chunks para respetar presupuesto de tokens.
        
        Args:
            chunks: Lista de chunks (strings)
            budget: Presupuesto máximo de tokens
            preserve_order: Si True, mantiene orden original
        
        Returns:
            Lista de chunks que caben en el presupuesto
        """
        selected = []
        used_tokens = 0
        
        for chunk in chunks:
            chunk_tokens = self.count_tokens(chunk)
            
            if used_tokens + chunk_tokens <= budget:
                selected.append(chunk)
                used_tokens += chunk_tokens
            else:
                # No cabe más
                break
        
        logger.debug(f"Truncated to {len(selected)}/{len(chunks)} chunks ({used_tokens}/{budget} tokens)")
        return selected
    
    def build_context_with_tiers(
        self,
        chunks_by_tier: Dict[str, List[Dict[str, Any]]],
        budget: int,
        tier_priority: List[str] = ['critical', 'high', 'medium', 'low']
    ) -> tuple:
        """
        Construye contexto priorizando por tier hasta llenar presupuesto.
        
        Args:
            chunks_by_tier: Dict con chunks organizados por tier
            budget: Presupuesto de tokens
            tier_priority: Orden de prioridad de tiers
        
        Returns:
            (context_string, chunks_used, tokens_used)
        """
        selected_chunks = []
        tokens_used = 0
        
        # Procesar tiers en orden de prioridad
        for tier in tier_priority:
            tier_chunks = chunks_by_tier.get(tier, [])
            
            for chunk in tier_chunks:
                content = chunk.get('content', '')
                chunk_tokens = self.count_tokens(content)
                
                # Verificar si cabe
                if tokens_used + chunk_tokens <= budget:
                    selected_chunks.append(chunk)
                    tokens_used += chunk_tokens
                else:
                    # Presupuesto lleno, retornar lo que tenemos
                    logger.info(f"Budget reached at tier '{tier}' ({tokens_used}/{budget} tokens)")
                    context = self._format_chunks_as_context(selected_chunks)
                    return context, selected_chunks, tokens_used
        
        # Si llegamos aquí, todos los chunks caben
        context = self._format_chunks_as_context(selected_chunks)
        logger.info(f"All chunks fit ({tokens_used}/{budget} tokens)")
        return context, selected_chunks, tokens_used
    
    def _format_chunks_as_context(self, chunks: List[Dict[str, Any]]) -> str:
        """
        Formatea chunks como contexto legible.
        
        Args:
            chunks: Lista de chunks con metadata
        
        Returns:
            String formateado para usar como contexto
        """
        sections = []
        
        for i, chunk in enumerate(chunks, 1):
            content = chunk.get('content', '')
            metadata = chunk.get('metadata', {})
            chunk_type = metadata.get('chunk_type', 'unknown')
            chunk_tier = metadata.get('chunk_tier', 'unknown')
            
            section = f"--- Section {i} ({chunk_type}, {chunk_tier}) ---\n{content}\n"
            sections.append(section)
        
        return "\n".join(sections)
    
    def estimate_response_tokens(
        self,
        system_prompt: str,
        user_prompt: str,
        max_completion: int
    ) -> int:
        """
        Estima tokens totales de una llamada al LLM.
        
        Args:
            system_prompt: System prompt
            user_prompt: User prompt
            max_completion: Max tokens para completion
        
        Returns:
            Estimación de tokens totales
        """
        prompt_tokens = self.count_tokens(system_prompt) + self.count_tokens(user_prompt)
        total = prompt_tokens + max_completion
        
        logger.debug(f"Estimated tokens: {prompt_tokens} (prompt) + {max_completion} (completion) = {total}")
        return total


def get_token_manager(model: str = DEFAULT_MODEL) -> TokenManager:
    """
    Factory function para obtener token manager.
    
    Args:
        model: Modelo para conteo de tokens
    
    Returns:
        TokenManager instance
    """
    return TokenManager(model)


# Testing
if __name__ == "__main__":
    # Test básico
    manager = TokenManager()
    
    text = "This is a test sentence for token counting."
    tokens = manager.count_tokens(text)
    print(f"Text: '{text}'")
    print(f"Tokens: {tokens}")
    
    # Test dynamic budget
    for inquiries in [1, 2, 3, 4]:
        budget = manager.calculate_dynamic_budget(inquiries)
        context_budget = manager.calculate_context_budget(budget)
        print(f"{inquiries} inquiries → {budget} tokens total, {context_budget} for context")

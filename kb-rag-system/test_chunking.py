#!/usr/bin/env python3
"""
Script de Prueba de Chunking

Prueba la generación de chunks con un artículo de ejemplo.
Muestra estadísticas y permite inspeccionar chunks individuales.
"""

import sys
from pathlib import Path

# Agregar el directorio raíz al path
sys.path.insert(0, str(Path(__file__).parent))

from data_pipeline.article_processor import ArticleProcessor
from data_pipeline.chunking import KBChunker


def print_separator(char="=", length=80):
    """Imprime una línea separadora."""
    print(char * length)


def print_chunk_summary(chunks):
    """Imprime resumen de chunks generados."""
    print_separator()
    print(f"RESUMEN DE CHUNKS GENERADOS")
    print_separator()
    print(f"\nTotal de chunks: {len(chunks)}\n")
    
    # Agrupar por tier
    by_tier = {}
    by_type = {}
    
    for chunk in chunks:
        tier = chunk["metadata"]["chunk_tier"]
        chunk_type = chunk["metadata"]["chunk_type"]
        
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_type[chunk_type] = by_type.get(chunk_type, 0) + 1
    
    print("Por Tier:")
    for tier in ["critical", "high", "medium", "low"]:
        count = by_tier.get(tier, 0)
        if count > 0:
            print(f"  {tier.upper()}: {count} chunks")
    
    print("\nPor Tipo:")
    for chunk_type, count in sorted(by_type.items()):
        print(f"  {chunk_type}: {count} chunks")
    
    print_separator()


def print_chunk_details(chunk, index):
    """Imprime detalles de un chunk."""
    print_separator("-")
    print(f"CHUNK #{index + 1}")
    print_separator("-")
    
    metadata = chunk["metadata"]
    
    print(f"\nID: {chunk['id']}")
    print(f"Tipo: {metadata['chunk_type']}")
    print(f"Categoría: {metadata['chunk_category']}")
    print(f"Tier: {metadata['chunk_tier']}")
    print(f"Topics: {', '.join(metadata['specific_topics'])}")
    print(f"Tamaño: {len(chunk['content'])} caracteres")
    
    # Mostrar metadata del artículo
    print(f"\nArtículo:")
    print(f"  Title: {metadata['article_title']}")
    print(f"  Record Keeper: {metadata['record_keeper']}")
    print(f"  Plan Type: {metadata['plan_type']}")
    print(f"  Topic: {metadata['topic']}")
    
    print(f"\nContenido (primeros 500 caracteres):")
    print_separator("-")
    content_preview = chunk['content'][:500]
    if len(chunk['content']) > 500:
        content_preview += "..."
    print(content_preview)
    print_separator("-")


def main():
    """Función principal."""
    print_separator("=")
    print("PRUEBA DE CHUNKING - KB RAG SYSTEM")
    print_separator("=")
    
    # Ruta al artículo de ejemplo
    article_path = "../Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
    
    print(f"\n📄 Cargando artículo: {article_path}\n")
    
    # Cargar artículo
    processor = ArticleProcessor()
    article = processor.load_article(article_path)
    
    if not article:
        print("❌ Error al cargar el artículo")
        return
    
    # Mostrar info del artículo
    info = processor.get_article_info(article)
    print(f"✅ Artículo cargado:")
    print(f"   Title: {info['title']}")
    print(f"   Record Keeper: {info['record_keeper']}")
    print(f"   Topic: {info['topic']}")
    print(f"   Subtopics: {', '.join(info['subtopics'][:3])}...")
    
    # Generar chunks
    print(f"\n🔨 Generando chunks...")
    chunker = KBChunker()
    chunks = chunker.chunk_article(article)
    
    print(f"✅ Chunks generados: {len(chunks)}")
    
    # Mostrar resumen
    print_chunk_summary(chunks)
    
    # Modo interactivo
    while True:
        print("\n¿Qué quieres hacer?")
        print("  1. Ver chunks por tier")
        print("  2. Ver chunk específico por número")
        print("  3. Ver chunks por tipo")
        print("  4. Salir")
        
        choice = input("\nElige una opción (1-4): ").strip()
        
        if choice == "1":
            tier = input("¿Qué tier? (critical/high/medium/low): ").strip().lower()
            tier_chunks = [c for c in chunks if c["metadata"]["chunk_tier"] == tier]
            
            print(f"\n{len(tier_chunks)} chunks en tier '{tier}':")
            for i, chunk in enumerate(tier_chunks):
                metadata = chunk["metadata"]
                print(f"{i+1}. [{metadata['chunk_type']}] {metadata['chunk_category']} ({len(chunk['content'])} chars)")
            
            if tier_chunks:
                show = input("\n¿Ver alguno? (número o Enter para continuar): ").strip()
                if show.isdigit():
                    idx = int(show) - 1
                    if 0 <= idx < len(tier_chunks):
                        original_idx = chunks.index(tier_chunks[idx])
                        print_chunk_details(tier_chunks[idx], original_idx)
        
        elif choice == "2":
            num = input(f"¿Qué chunk? (1-{len(chunks)}): ").strip()
            if num.isdigit():
                idx = int(num) - 1
                if 0 <= idx < len(chunks):
                    print_chunk_details(chunks[idx], idx)
                else:
                    print(f"❌ Número fuera de rango (1-{len(chunks)})")
            else:
                print("❌ Número inválido")
        
        elif choice == "3":
            chunk_types = sorted(set(c["metadata"]["chunk_type"] for c in chunks))
            print("\nTipos disponibles:")
            for i, ct in enumerate(chunk_types):
                count = len([c for c in chunks if c["metadata"]["chunk_type"] == ct])
                print(f"{i+1}. {ct} ({count} chunks)")
            
            type_choice = input(f"\n¿Qué tipo? (1-{len(chunk_types)}): ").strip()
            if type_choice.isdigit():
                type_idx = int(type_choice) - 1
                if 0 <= type_idx < len(chunk_types):
                    selected_type = chunk_types[type_idx]
                    type_chunks = [c for c in chunks if c["metadata"]["chunk_type"] == selected_type]
                    
                    print(f"\n{len(type_chunks)} chunks de tipo '{selected_type}':")
                    for i, chunk in enumerate(type_chunks):
                        metadata = chunk["metadata"]
                        print(f"{i+1}. {metadata['chunk_category']} - Tier: {metadata['chunk_tier']} ({len(chunk['content'])} chars)")
        
        elif choice == "4":
            print("\n👋 ¡Hasta luego!")
            break
        
        else:
            print("❌ Opción inválida")


if __name__ == "__main__":
    main()

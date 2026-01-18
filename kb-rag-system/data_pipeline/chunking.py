"""
Chunking Module

Implementa la estrategia de chunking para artículos de KB.
Divide artículos en chunks semánticos con metadata enriquecida.

Estrategia:
- Modo A (required_data): Chunks específicos para recolección de datos
- Modo B (generate_response): Chunks priorizados por tier
"""

from typing import Dict, Any, List
import logging
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class KBChunker:
    """Genera chunks semánticos de artículos KB."""
    
    def __init__(self):
        """Inicializa el chunker."""
        self.chunk_counter = 0
    
    def chunk_article(self, article: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Genera todos los chunks de un artículo.
        
        Args:
            article: Dict con el artículo completo
            
        Returns:
            Lista de chunks con contenido y metadata
        """
        chunks = []
        
        # Reset counter para este artículo
        self.chunk_counter = 0
        
        # Extraer metadata base del artículo
        base_metadata = self._extract_base_metadata(article)
        
        # CHUNKS MODO A: Required Data (para /required-data endpoint)
        chunks.extend(self._create_required_data_chunks(article, base_metadata))
        
        # CHUNKS MODO B: Response Generation (para /generate-response endpoint)
        # Tier 1: CRÍTICO
        chunks.extend(self._create_tier1_chunks(article, base_metadata))
        
        # Tier 2: IMPORTANTE
        chunks.extend(self._create_tier2_chunks(article, base_metadata))
        
        # Tier 3: OPCIONAL
        chunks.extend(self._create_tier3_chunks(article, base_metadata))
        
        logger.info(f"Generados {len(chunks)} chunks para {base_metadata['title']}")
        
        return chunks
    
    def _extract_base_metadata(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Extrae metadata base del artículo para todos los chunks."""
        metadata = article.get("metadata", {})
        summary = article.get("summary", {})
        
        return {
            "article_id": metadata.get("article_id"),
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "record_keeper": metadata.get("record_keeper"),
            "plan_type": metadata.get("plan_type"),
            "scope": metadata.get("scope"),
            "tags": metadata.get("tags", []),
            "topic": summary.get("topic"),
            "subtopics": summary.get("subtopics", [])
        }
    
    def _create_chunk(
        self,
        content: str,
        base_metadata: Dict[str, Any],
        chunk_type: str,
        chunk_category: str,
        tier: str,
        topics: List[str] = None
    ) -> Dict[str, Any]:
        """
        Crea un chunk con metadata completa.
        
        Args:
            content: Contenido del chunk
            base_metadata: Metadata base del artículo
            chunk_type: Tipo de chunk (required_data, business_rules, etc.)
            chunk_category: Categoría específica (fees, eligibility, etc.)
            tier: Tier de prioridad (critical, high, medium, low)
            topics: Lista de topics relacionados
            
        Returns:
            Dict con chunk y metadata
        """
        self.chunk_counter += 1
        
        # Generar ID único para el chunk
        chunk_id = f"{base_metadata['article_id']}_chunk_{self.chunk_counter}"
        
        # Crear hash del contenido para deduplicación
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        
        return {
            "id": chunk_id,
            "content": content,
            "metadata": {
                # Metadata del artículo
                "article_id": base_metadata["article_id"],
                "article_title": base_metadata["title"],
                "record_keeper": base_metadata["record_keeper"],
                "plan_type": base_metadata["plan_type"],
                "scope": base_metadata["scope"],
                "tags": base_metadata["tags"],
                
                # Metadata del artículo - topics
                "topic": base_metadata["topic"],
                "subtopics": base_metadata["subtopics"],
                
                # Metadata del chunk
                "chunk_type": chunk_type,
                "chunk_category": chunk_category,
                "chunk_index": self.chunk_counter,
                "chunk_tier": tier,  # critical, high, medium, low
                
                # Para búsqueda avanzada
                "specific_topics": topics or [],
                "content_hash": content_hash
            }
        }
    
    # ============================================================================
    # MODO A: Required Data Chunks (para /required-data endpoint)
    # ============================================================================
    
    def _create_required_data_chunks(
        self,
        article: Dict[str, Any],
        base_metadata: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Crea chunks para el modo required_data."""
        chunks = []
        details = article.get("details", {})
        summary = article.get("summary", {})
        
        # Chunk 1: Required Data Complete
        required_data = details.get("required_data", {})
        if required_data:
            content = self._format_required_data(required_data, summary)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="required_data",
                chunk_category="data_collection",
                tier="critical",
                topics=["data_requirements", "field_collection"]
            ))
        
        # Chunk 2: Eligibility Requirements
        business_rules = details.get("business_rules", [])
        eligibility_rules = [
            rule for rule in business_rules 
            if rule.get("category") == "eligibility"
        ]
        if eligibility_rules:
            content = self._format_business_rules(eligibility_rules, "Eligibility")
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="eligibility",
                chunk_category="requirements",
                tier="critical",
                topics=["eligibility", "requirements"]
            ))
        
        # Chunk 3: Critical Flags
        critical_flags = summary.get("critical_flags", {})
        if critical_flags:
            content = self._format_critical_flags(critical_flags)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="critical_flags",
                chunk_category="validation",
                tier="critical",
                topics=["flags", "validation"]
            ))
        
        return chunks
    
    def _format_required_data(
        self,
        required_data: Dict[str, Any],
        summary: Dict[str, Any]
    ) -> str:
        """Formatea required_data para el chunk."""
        lines = ["# Required Data for This Process\n"]
        
        # Agregar resumen de data requerida
        required_summary = summary.get("required_data_summary", [])
        if required_summary:
            lines.append("## Summary:")
            for item in required_summary:
                lines.append(f"- {item}")
            lines.append("")
        
        # Must have fields
        must_have = required_data.get("must_have", [])
        if must_have:
            lines.append("## Must Have (Required):")
            for field in must_have:
                lines.append(f"\n### {field.get('data_point')}")
                lines.append(f"**Description:** {field.get('meaning')}")
                lines.append(f"**Why needed:** {field.get('why_needed')}")
                lines.append(f"**Data type:** {field.get('source_type', 'participant_data')}")
                if field.get('example_values'):
                    examples = field['example_values']
                    if isinstance(examples, list):
                        lines.append(f"**Examples:** {', '.join(examples)}")
                    else:
                        lines.append(f"**Example:** {examples}")
        
        # Nice to have fields
        nice_to_have = required_data.get("nice_to_have", [])
        if nice_to_have:
            lines.append("\n## Nice to Have (Optional):")
            for field in nice_to_have:
                lines.append(f"\n### {field.get('data_point')}")
                lines.append(f"**Description:** {field.get('meaning')}")
                lines.append(f"**Why needed:** {field.get('why_needed')}")
        
        # If missing instructions
        if_missing = required_data.get("if_missing", [])
        if if_missing:
            lines.append("\n## If Data is Missing:")
            for item in if_missing:
                lines.append(f"\n**Missing:** {item.get('missing_data_point')}")
                lines.append(f"**Ask:** {item.get('ask_participant')}")
        
        return "\n".join(lines)
    
    def _format_critical_flags(self, flags: Dict[str, Any]) -> str:
        """Formatea critical flags."""
        lines = ["# Critical Flags\n"]
        
        for key, value in flags.items():
            lines.append(f"**{key}:** {value}")
        
        return "\n".join(lines)
    
    # ============================================================================
    # MODO B - TIER 1: Chunks Críticos (siempre recuperar)
    # ============================================================================
    
    def _create_tier1_chunks(
        self,
        article: Dict[str, Any],
        base_metadata: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Crea chunks Tier 1 (CRÍTICOS) para generate_response."""
        chunks = []
        details = article.get("details", {})
        summary = article.get("summary", {})
        
        # Chunk: Decision Guide Complete
        decision_guide = details.get("decision_guide", {})
        if decision_guide:
            content = self._format_decision_guide(decision_guide)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="decision_guide",
                chunk_category="decision_making",
                tier="critical",
                topics=["decision", "outcomes", "eligibility"]
            ))
        
        # Chunk: Response Frames
        response_frames = details.get("response_frames", {})
        if response_frames:
            content = self._format_response_frames(response_frames)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="response_frames",
                chunk_category="response_templates",
                tier="critical",
                topics=["response", "communication"]
            ))
        
        # Chunk: Guardrails Complete
        guardrails = details.get("guardrails", {})
        if guardrails:
            content = self._format_guardrails(guardrails, summary)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="guardrails",
                chunk_category="safety",
                tier="critical",
                topics=["guardrails", "safety", "compliance"]
            ))
        
        # Chunks: Business Rules (por categoría)
        business_rules = details.get("business_rules", [])
        for rule_group in business_rules:
            category = rule_group.get("category", "general")
            content = self._format_business_rules([rule_group], category.title())
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="business_rules",
                chunk_category=category,
                tier="critical" if category in ["fees", "eligibility", "tax_withholding"] else "high",
                topics=[category, "rules", "policy"]
            ))
        
        return chunks
    
    def _format_decision_guide(self, decision_guide: Dict[str, Any]) -> str:
        """Formatea decision guide."""
        lines = ["# Decision Guide\n"]
        
        # Supported outcomes
        outcomes = decision_guide.get("supported_outcomes", [])
        if outcomes:
            lines.append("## Supported Outcomes:")
            for outcome in outcomes:
                lines.append(f"- {outcome}")
            lines.append("")
        
        # Eligibility requirements
        eligibility = decision_guide.get("eligibility_requirements", [])
        if eligibility:
            lines.append("## Eligibility Requirements:")
            for req in eligibility:
                lines.append(f"- {req}")
            lines.append("")
        
        # Blocking conditions
        blocking = decision_guide.get("blocking_conditions", [])
        if blocking:
            lines.append("## Blocking Conditions:")
            for condition in blocking:
                lines.append(f"- {condition}")
            lines.append("")
        
        # Missing data conditions
        missing_data = decision_guide.get("missing_data_conditions", [])
        if missing_data:
            lines.append("## Missing Data Scenarios:")
            for item in missing_data:
                lines.append(f"\n**Condition:** {item.get('condition')}")
                lines.append(f"**Missing:** {item.get('missing_data_point')}")
                lines.append(f"**Outcome:** {item.get('resulting_outcome')}")
                lines.append(f"**Ask:** {item.get('ask_participant')}")
        
        # Allowed conclusions
        allowed = decision_guide.get("allowed_conclusions", [])
        if allowed:
            lines.append("\n## Allowed Conclusions:")
            for conclusion in allowed:
                lines.append(f"- {conclusion}")
        
        # Not allowed conclusions
        not_allowed = decision_guide.get("not_allowed_conclusions", [])
        if not_allowed:
            lines.append("\n## Not Allowed Conclusions:")
            for conclusion in not_allowed:
                lines.append(f"- {conclusion}")
        
        return "\n".join(lines)
    
    def _format_response_frames(self, response_frames: Dict[str, Any]) -> str:
        """Formatea response frames."""
        lines = ["# Response Frames by Outcome\n"]
        
        for outcome, frame in response_frames.items():
            lines.append(f"\n## Outcome: {outcome}\n")
            
            # Message components
            components = frame.get("participant_message_components", [])
            if components:
                lines.append("### Message Components:")
                for comp in components:
                    lines.append(f"- {comp}")
                lines.append("")
            
            # Next steps
            next_steps = frame.get("next_steps", [])
            if next_steps:
                lines.append("### Next Steps:")
                for step in next_steps:
                    lines.append(f"- {step}")
                lines.append("")
            
            # Warnings
            warnings = frame.get("warnings", [])
            if warnings:
                lines.append("### Warnings:")
                for warning in warnings:
                    lines.append(f"- {warning}")
                lines.append("")
            
            # Questions to ask
            questions = frame.get("questions_to_ask", [])
            if questions:
                lines.append("### Questions to Ask:")
                for q in questions:
                    lines.append(f"- {q}")
                lines.append("")
            
            # What not to say
            not_say = frame.get("what_not_to_say", [])
            if not_say:
                lines.append("### Do NOT Say:")
                for item in not_say:
                    lines.append(f"- {item}")
        
        return "\n".join(lines)
    
    def _format_guardrails(
        self,
        guardrails: Dict[str, Any],
        summary: Dict[str, Any]
    ) -> str:
        """Formatea guardrails."""
        lines = ["# Guardrails and Safety Rules\n"]
        
        # Plan specific guardrails from summary
        plan_guardrails = summary.get("plan_specific_guardrails", [])
        if plan_guardrails:
            lines.append("## Plan-Specific Guardrails:")
            for item in plan_guardrails:
                lines.append(f"- {item}")
            lines.append("")
        
        # Must not
        must_not = guardrails.get("must_not", [])
        if must_not:
            lines.append("## Must NOT Say:")
            for item in must_not:
                lines.append(f"- {item}")
            lines.append("")
        
        # Must do if unsure
        must_do = guardrails.get("must_do_if_unsure", [])
        if must_do:
            lines.append("## Must Do If Unsure:")
            for item in must_do:
                lines.append(f"- {item}")
        
        return "\n".join(lines)
    
    def _format_business_rules(
        self,
        rule_groups: List[Dict[str, Any]],
        title: str
    ) -> str:
        """Formatea business rules."""
        lines = [f"# Business Rules: {title}\n"]
        
        for group in rule_groups:
            category = group.get("category", "general")
            rules = group.get("rules", [])
            
            if rules:
                lines.append(f"## {category.replace('_', ' ').title()}:")
                for rule in rules:
                    lines.append(f"- {rule}")
                lines.append("")
        
        return "\n".join(lines)
    
    # ============================================================================
    # MODO B - TIER 2: Chunks Importantes
    # ============================================================================
    
    def _create_tier2_chunks(
        self,
        article: Dict[str, Any],
        base_metadata: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Crea chunks Tier 2 (IMPORTANTES) para generate_response."""
        chunks = []
        details = article.get("details", {})
        summary = article.get("summary", {})
        
        # Chunk: Steps (agrupados)
        steps = details.get("steps", [])
        if steps:
            # Agrupar steps en chunks de 3-4 pasos
            step_chunks = self._group_steps(steps)
            for i, step_group in enumerate(step_chunks):
                content = self._format_steps(step_group)
                first_step = step_group[0].get("step_number", i*3+1)
                last_step = step_group[-1].get("step_number", (i+1)*3)
                
                chunks.append(self._create_chunk(
                    content=content,
                    base_metadata=base_metadata,
                    chunk_type="steps",
                    chunk_category=f"steps_{first_step}_to_{last_step}",
                    tier="high",
                    topics=["procedure", "steps", "process"]
                ))
        
        # Chunk: Key Steps Summary
        key_steps_summary = summary.get("key_steps_summary", [])
        if key_steps_summary:
            content = "# Key Steps Summary\n\n" + "\n".join([f"{i+1}. {step}" for i, step in enumerate(key_steps_summary)])
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="steps_summary",
                chunk_category="overview",
                tier="high",
                topics=["summary", "steps", "overview"]
            ))
        
        # Chunks: Common Issues
        common_issues = details.get("common_issues", [])
        if common_issues:
            # Agrupar issues similares
            issue_chunks = self._group_common_issues(common_issues)
            for category, issues in issue_chunks.items():
                content = self._format_common_issues(issues)
                chunks.append(self._create_chunk(
                    content=content,
                    base_metadata=base_metadata,
                    chunk_type="common_issues",
                    chunk_category=category,
                    tier="high",
                    topics=["troubleshooting", "issues", "problems"]
                ))
        
        # Chunks: Examples (1 por scenario)
        examples = details.get("examples", [])
        for i, example in enumerate(examples):
            content = self._format_example(example)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="example",
                chunk_category=f"scenario_{i+1}",
                tier="medium",
                topics=["example", "scenario", "use_case"]
            ))
        
        # Chunk: Fees Details
        fees = details.get("fees", [])
        if fees:
            content = self._format_fees(fees, summary.get("key_business_rules", []))
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="fees_details",
                chunk_category="costs",
                tier="high",
                topics=["fees", "costs", "charges"]
            ))
        
        return chunks
    
    def _group_steps(self, steps: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Agrupa steps en chunks de 3-4 pasos."""
        grouped = []
        chunk_size = 3
        
        for i in range(0, len(steps), chunk_size):
            grouped.append(steps[i:i + chunk_size])
        
        return grouped
    
    def _format_steps(self, steps: List[Dict[str, Any]]) -> str:
        """Formatea steps."""
        lines = ["# Step-by-Step Procedure\n"]
        
        for step in steps:
            step_num = step.get("step_number", "")
            description = step.get("description", "")
            notes = step.get("notes", "")
            
            lines.append(f"## Step {step_num}: {description}")
            if notes:
                lines.append(f"**Note:** {notes}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _group_common_issues(self, issues: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Agrupa common issues por categoría."""
        grouped = {
            "access": [],
            "process": [],
            "technical": []
        }
        
        for issue in issues:
            issue_text = issue.get("issue", "").lower()
            
            if any(word in issue_text for word in ["login", "access", "portal", "password"]):
                grouped["access"].append(issue)
            elif any(word in issue_text for word in ["wire", "check", "delivery", "received"]):
                grouped["process"].append(issue)
            else:
                grouped["technical"].append(issue)
        
        # Filtrar categorías vacías
        return {k: v for k, v in grouped.items() if v}
    
    def _format_common_issues(self, issues: List[Dict[str, Any]]) -> str:
        """Formatea common issues."""
        lines = ["# Common Issues and Resolutions\n"]
        
        for issue in issues:
            issue_desc = issue.get("issue", "")
            resolution = issue.get("resolution", "")
            
            lines.append(f"## Issue: {issue_desc}")
            lines.append(f"**Resolution:** {resolution}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _format_example(self, example: Dict[str, Any]) -> str:
        """Formatea un ejemplo."""
        scenario = example.get("scenario", "")
        outcome = example.get("outcome", "")
        
        return f"# Example Scenario\n\n**Scenario:** {scenario}\n\n**Outcome:** {outcome}"
    
    def _format_fees(self, fees: List[Dict[str, Any]], business_rules: List[str]) -> str:
        """Formatea fees."""
        lines = ["# Fees and Charges\n"]
        
        # Fee rules from business rules
        fee_rules = [rule for rule in business_rules if "fee" in rule.lower()]
        if fee_rules:
            lines.append("## Fee Rules:")
            for rule in fee_rules:
                lines.append(f"- {rule}")
            lines.append("")
        
        # Detailed fees
        lines.append("## Fee Details:")
        for fee in fees:
            service = fee.get("service", "")
            amount = fee.get("fee", "")
            notes = fee.get("notes", "")
            
            lines.append(f"\n**{service}:** {amount}")
            if notes:
                lines.append(f"*{notes}*")
        
        return "\n".join(lines)
    
    # ============================================================================
    # MODO B - TIER 3: Chunks Opcionales
    # ============================================================================
    
    def _create_tier3_chunks(
        self,
        article: Dict[str, Any],
        base_metadata: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Crea chunks Tier 3 (OPCIONALES) para generate_response."""
        chunks = []
        details = article.get("details", {})
        summary = article.get("summary", {})
        
        # Chunks: FAQs (agrupados de 2-3)
        faq_pairs = details.get("faq_pairs", [])
        high_impact_faqs = summary.get("high_impact_faq_pairs", [])
        
        # High impact FAQs primero
        if high_impact_faqs:
            content = self._format_faqs(high_impact_faqs)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="faqs",
                chunk_category="high_impact",
                tier="medium",
                topics=["faq", "questions", "common_questions"]
            ))
        
        # Regular FAQs
        if faq_pairs:
            faq_chunks = self._group_faqs(faq_pairs)
            for i, faq_group in enumerate(faq_chunks):
                content = self._format_faqs(faq_group)
                chunks.append(self._create_chunk(
                    content=content,
                    base_metadata=base_metadata,
                    chunk_type="faqs",
                    chunk_category=f"group_{i+1}",
                    tier="low",
                    topics=["faq", "questions"]
                ))
        
        # Chunk: Definitions
        definitions = details.get("definitions", [])
        if definitions:
            content = self._format_definitions(definitions)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="definitions",
                chunk_category="glossary",
                tier="low",
                topics=["definitions", "terms", "glossary"]
            ))
        
        # Chunk: Additional Notes
        additional_notes = details.get("additional_notes", [])
        if additional_notes:
            for note_group in additional_notes:
                category = note_group.get("category", "general")
                content = self._format_additional_notes(note_group)
                chunks.append(self._create_chunk(
                    content=content,
                    base_metadata=base_metadata,
                    chunk_type="additional_notes",
                    chunk_category=category,
                    tier="low",
                    topics=["notes", "additional_info", category]
                ))
        
        # Chunk: References
        references = details.get("references", {})
        if references:
            content = self._format_references(references)
            chunks.append(self._create_chunk(
                content=content,
                base_metadata=base_metadata,
                chunk_type="references",
                chunk_category="links",
                tier="low",
                topics=["references", "links", "resources"]
            ))
        
        return chunks
    
    def _group_faqs(self, faqs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Agrupa FAQs en chunks de 2-3."""
        grouped = []
        chunk_size = 3
        
        for i in range(0, len(faqs), chunk_size):
            grouped.append(faqs[i:i + chunk_size])
        
        return grouped
    
    def _format_faqs(self, faqs: List[Dict[str, Any]]) -> str:
        """Formatea FAQs."""
        lines = ["# Frequently Asked Questions\n"]
        
        for faq in faqs:
            question = faq.get("question", "")
            answer = faq.get("answer", "")
            
            lines.append(f"## Q: {question}")
            lines.append(f"**A:** {answer}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _format_definitions(self, definitions: List[Dict[str, Any]]) -> str:
        """Formatea definitions."""
        lines = ["# Definitions and Terms\n"]
        
        for definition in definitions:
            term = definition.get("term", "")
            meaning = definition.get("definition", "")
            
            lines.append(f"## {term}")
            lines.append(f"{meaning}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _format_additional_notes(self, note_group: Dict[str, Any]) -> str:
        """Formatea additional notes."""
        category = note_group.get("category", "General").replace("_", " ").title()
        notes = note_group.get("notes", [])
        
        lines = [f"# Additional Information: {category}\n"]
        
        for note in notes:
            lines.append(f"- {note}")
        
        return "\n".join(lines)
    
    def _format_references(self, references: Dict[str, Any]) -> str:
        """Formatea references."""
        lines = ["# References and Resources\n"]
        
        # Portal link
        portal = references.get("participant_portal")
        if portal:
            lines.append(f"**Participant Portal:** {portal}")
            lines.append("")
        
        # Contact info
        contact = references.get("contact", {})
        if contact:
            lines.append("## Contact Information:")
            if contact.get("email"):
                lines.append(f"**Email:** {contact['email']}")
            if contact.get("phone"):
                lines.append(f"**Phone:** {contact['phone']}")
            if contact.get("support_hours"):
                lines.append(f"**Hours:** {contact['support_hours']}")
            lines.append("")
        
        # Internal articles
        internal = references.get("internal_articles", [])
        if internal:
            lines.append("## Related Internal Articles:")
            for article in internal:
                lines.append(f"- {article}")
            lines.append("")
        
        # External links
        external = references.get("external_links", [])
        if external:
            lines.append("## External Resources:")
            for link in external:
                lines.append(f"- {link}")
        
        return "\n".join(lines)


# Helper function para uso directo
def generate_chunks_from_article(article: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Helper function para generar chunks de un artículo.
    
    Args:
        article: Dict con el artículo completo
        
    Returns:
        Lista de chunks con contenido y metadata
    """
    chunker = KBChunker()
    chunks = chunker.chunk_article(article)
    
    logger.info(f"Generados {len(chunks)} chunks para {article['metadata']['title']}")
    
    return chunks

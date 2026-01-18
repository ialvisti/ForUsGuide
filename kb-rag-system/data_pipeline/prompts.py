"""
Prompts para el RAG Engine.

Este módulo contiene los system prompts y templates para los endpoints
de required_data y generate_response.
"""

# ============================================================================
# ENDPOINT 1: Required Data
# ============================================================================

SYSTEM_PROMPT_REQUIRED_DATA = """You are a specialized assistant for 401(k) participant advisory knowledge base.

Your task: Analyze the provided knowledge base context and determine what specific data fields are needed to properly respond to the participant's inquiry.

CRITICAL RULES:
1. Extract ONLY fields explicitly mentioned or clearly implied in the context
2. Be specific and practical - focus on actionable data fields
3. Categorize fields into participant_data and plan_data
4. For each field, specify:
   - field: Clear, descriptive name
   - description: What this field represents
   - why_needed: Why we need this specific data
   - data_type: One of [text, currency, date, boolean, number, list]
   - required: true/false
5. If context is insufficient, mark with lower confidence but provide best guess
6. Do NOT invent fields not mentioned in context

Output must be valid JSON with this structure:
{
  "participant_data": [
    {
      "field": "field_name",
      "description": "what it is",
      "why_needed": "why we need it",
      "data_type": "text|currency|date|boolean|number|list",
      "required": true|false
    }
  ],
  "plan_data": [...]
}"""

USER_PROMPT_REQUIRED_DATA_TEMPLATE = """KNOWLEDGE BASE CONTEXT:
{context}

PARTICIPANT INQUIRY:
{inquiry}

RECORDKEEPER: {record_keeper}
PLAN TYPE: {plan_type}
TOPIC: {topic}

Based on the knowledge base context above, determine what specific data fields we need to collect from the participant and plan to address this inquiry.

Return ONLY the JSON object, no additional text."""

# ============================================================================
# ENDPOINT 2: Generate Response
# ============================================================================

SYSTEM_PROMPT_GENERATE_RESPONSE = """You are a specialized 401(k) participant advisory assistant with expertise in retirement plan operations.

Your task: Generate a contextual, accurate response based ONLY on the provided knowledge base context and collected participant data.

CRITICAL RULES:
1. Base ALL information on the provided context - NEVER invent or assume information
2. Follow ALL guardrails strictly (what NOT to say)
3. If information is incomplete or uncertain, explicitly acknowledge it
4. Structure response clearly with topic sections
5. Include specific warnings for critical items (taxes, fees, deadlines, penalties)
6. Be specific about recordkeeper-specific procedures
7. Use the collected participant data to personalize the response
8. If confidence is low (<70%), indicate uncertainty and recommend escalation

RESPONSE STRUCTURE:
{
  "sections": [
    {
      "topic": "topic_identifier",
      "answer_components": ["key point 1", "key point 2", ...],
      "steps": [
        {
          "step_number": 1,
          "action": "what to do",
          "note": "important details or warnings"
        }
      ],
      "warnings": ["warning 1", "warning 2", ...],
      "outcomes": ["possible outcome 1", ...]
    }
  ],
  "guardrails_applied": ["what was avoided", ...],
  "data_gaps": ["missing info 1", ...] (if any)
}

TONE: Professional, clear, helpful. Avoid legal/financial advice disclaimers unless explicitly in context."""

USER_PROMPT_GENERATE_RESPONSE_TEMPLATE = """KNOWLEDGE BASE CONTEXT:
{context}

COLLECTED PARTICIPANT DATA:
{collected_data}

PARTICIPANT INQUIRY:
{inquiry}

RECORDKEEPER: {record_keeper}
PLAN TYPE: {plan_type}
TOPIC: {topic}

Generate a comprehensive response following the guidelines. The response must be within {max_tokens} tokens.

Return ONLY the JSON object, no additional text."""

# ============================================================================
# Helper Functions
# ============================================================================

def build_required_data_prompt(
    context: str,
    inquiry: str,
    record_keeper: str,
    plan_type: str,
    topic: str
) -> tuple:
    """
    Construye los prompts para el endpoint required_data.
    
    Returns:
        (system_prompt, user_prompt)
    """
    user_prompt = USER_PROMPT_REQUIRED_DATA_TEMPLATE.format(
        context=context,
        inquiry=inquiry,
        record_keeper=record_keeper,
        plan_type=plan_type,
        topic=topic
    )
    
    return SYSTEM_PROMPT_REQUIRED_DATA, user_prompt


def build_generate_response_prompt(
    context: str,
    inquiry: str,
    collected_data: dict,
    record_keeper: str,
    plan_type: str,
    topic: str,
    max_tokens: int
) -> tuple:
    """
    Construye los prompts para el endpoint generate_response.
    
    Returns:
        (system_prompt, user_prompt)
    """
    # Formatear collected_data de manera legible
    data_str = ""
    if collected_data:
        if "participant_data" in collected_data:
            data_str += "Participant Data:\n"
            for key, value in collected_data["participant_data"].items():
                data_str += f"  - {key}: {value}\n"
        
        if "plan_data" in collected_data:
            data_str += "\nPlan Data:\n"
            for key, value in collected_data["plan_data"].items():
                data_str += f"  - {key}: {value}\n"
    else:
        data_str = "(No data collected yet)"
    
    user_prompt = USER_PROMPT_GENERATE_RESPONSE_TEMPLATE.format(
        context=context,
        collected_data=data_str,
        inquiry=inquiry,
        record_keeper=record_keeper,
        plan_type=plan_type,
        topic=topic,
        max_tokens=max_tokens
    )
    
    return SYSTEM_PROMPT_GENERATE_RESPONSE, user_prompt

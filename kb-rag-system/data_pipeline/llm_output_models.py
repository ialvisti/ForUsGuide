"""
Schemas estrictos para outputs de LLM (Task 5 del plan, HT-12).

Cada agente tiene un modelo Pydantic con bounds; el orchestrator valida ANTES
de usar cualquier output. Un item inválido se descarta (degradación explícita
en diagnostics), nunca se "arregla" silenciosamente. Los campos server-owned
(IDs, límites, conteos, collected_data) NO aparecen aquí: aunque el LLM los
emita, el orchestrator los ignora y los fija desde fuentes confiables.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TOPIC_RE = r"^[a-z0-9_]{1,100}$"


class ExtractedInquiryOut(BaseModel):
    """Item del array de ``extract_inquiries``."""

    model_config = ConfigDict(extra="forbid")

    inquiry: str = Field(..., min_length=1, max_length=1000)
    topic: str = Field(..., min_length=2, max_length=100)
    record_keeper: Optional[str] = Field(default=None, max_length=100)
    plan_type: Optional[str] = Field(default=None, max_length=50)
    related_inquiries: List[str] = Field(default_factory=list, max_length=10)

    @field_validator("topic")
    @classmethod
    def _norm_topic(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("related_inquiries")
    @classmethod
    def _bound_related(cls, v: List[str]) -> List[str]:
        return [s[:1000] for s in v]


class KBQuestionSynthesisOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Optional[str] = Field(default=None, max_length=800)
    insufficient_inquiry: bool = False


class FieldMapModuleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=60)
    fields: List[str] = Field(default_factory=list, max_length=40)

    @field_validator("fields")
    @classmethod
    def _bound_fields(cls, v: List[str]) -> List[str]:
        return [s[:120] for s in v]


class FieldMapOut(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    modules: List[FieldMapModuleOut] = Field(default_factory=list, max_length=12)
    unmapped: List[Any] = Field(default_factory=list, alias="_unmapped",
                                max_length=60)


class TicketFieldValueOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None
    evidence: str = Field(..., min_length=1, max_length=600)


class TicketFieldExtractOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extracted: Dict[str, TicketFieldValueOut] = Field(default_factory=dict)
    not_found: List[str] = Field(default_factory=list, max_length=60)


class GRBodyDraftOut(BaseModel):
    """Único rol restante del gr_body_build: REDACCIÓN (inquiry enriquecida,
    topic). Todo lo demás que el modelo emita (collected_data, límites,
    conteos, IDs) se IGNORA: los fija el builder determinístico y el request.
    """

    model_config = ConfigDict(extra="ignore")

    inquiry: Optional[str] = Field(default=None, max_length=2000)
    topic: Optional[str] = Field(default=None, max_length=100)
    plan_type: Optional[str] = Field(default=None, max_length=50)

    @field_validator("topic")
    @classmethod
    def _norm_topic(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().lower() if isinstance(v, str) else v

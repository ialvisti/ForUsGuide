import json
import subprocess
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / "ui" / "tickets" / "assets"


def _normalize_execution_detail(envelope: dict) -> dict:
    script = """
const apiUrl = process.argv[1];
const envelope = JSON.parse(process.argv[2]);
const { normalizeExecutionDetail } = await import(apiUrl);
process.stdout.write(JSON.stringify(normalizeExecutionDetail(envelope)));
"""
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            (ASSETS / "api.js").resolve().as_uri(),
            json.dumps(envelope),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_the_complete_authorized_run_reaches_the_lossless_presenter():
    run = {
        "execution_id": "execution-sentinel",
        "event_digest": "a" * 64,
        "review_id": "b" * 64,
        "devrev_work_id": "work-sentinel",
        "devrev_display_id": "TKT-SENTINEL",
        "ticket_snapshot": {"title": "snapshot-sentinel"},
        "ticket_detail_snapshot": {"body": "detail-snapshot-sentinel"},
        "hydration_status": "succeeded",
        "authorization_status": "authorized",
        "hydration_attempts": 3,
        "hydration_retryable": False,
        "hydration_error_code": None,
        "last_hydration_attempt_at": "2026-08-11T01:02:03.456789+00:00",
        "next_hydration_attempt_at": None,
        "retention_expires_at": "2026-09-11T01:02:03+00:00",
        "legal_hold": True,
        "created_at": "2026-08-11T00:00:00+00:00",
        "updated_at": "2026-08-11T01:02:04+00:00",
        "event": {
            "schema_version": "1.0",
            "execution_id": "execution-sentinel",
            "tenant_id": "tenant-sentinel",
            "ticket_id": "ticket-event-sentinel",
            "answer": "answer-sentinel",
        },
    }

    normalized = _normalize_execution_detail({"execution": run})

    assert normalized["execution"]["recordedRun"] == run
    detail = (ASSETS / "detail.js").read_text(encoding="utf-8")
    assert "renderStructuredData(execution.recordedRun" in detail
    assert 'label: "Complete recorded run"' in detail


def test_source_title_aliases_remain_independently_available_to_the_presenter():
    adapter = (ASSETS / "api.js").read_text(encoding="utf-8")

    assert "articleTitle: value.article_title ?? null" in adapter
    assert "title: value.title ?? null" in adapter
    assert "title: value.article_title ?? value.title" not in adapter


def test_conversation_metadata_interface_labels_have_spanish_translations():
    preferences = (ASSETS / "preferences.js").read_text(encoding="utf-8")

    for english, spanish in (
        ("In reply to entry", "En respuesta a la entrada"),
        ("Upstream entry type:", "Tipo de entrada de origen:"),
        ("Entry", "Entrada"),
        (
            "A change was recorded with no summary.",
            "Se registró un cambio sin resumen.",
        ),
    ):
        assert f'["{english}", "{spanish}"]' in preferences


def test_fixture_diagnostic_labels_are_plain_language_and_bilingual():
    presenter = (ASSETS / "structured.js").read_text(encoding="utf-8")
    preferences = (ASSETS / "preferences.js").read_text(encoding="utf-8")

    labels = (
        ("source_plan", "Source plan", "Plan de origen"),
        ("destination_account", "Destination account", "Cuenta de destino"),
        ("llm_called", "LLM invoked", "LLM invocado"),
        ("field", "Field", "Campo"),
        ("required", "Required", "Obligatorio"),
        ("minimum_score_met", "Minimum score met", "Cumple la puntuación mínima"),
        ("match_count", "Match count", "Cantidad de coincidencias"),
        ("audience", "Audience", "Audiencia"),
        ("fallbacks_used", "Fallbacks used", "Alternativas utilizadas"),
        ("metadata", "Metadata", "Metadatos"),
        ("correlation", "Correlation", "Correlación"),
        ("trace_id", "Trace ID", "ID de trazabilidad"),
    )
    for key, english, spanish in labels:
        assert f'["{key}", "{english}"]' in presenter
        assert f'["{english}", "{spanish}"]' in preferences


def test_conversation_entry_counts_translate_in_both_directions():
    preferences = (ASSETS / "preferences.js").read_text(encoding="utf-8")

    assert "entr(?:y|ies) loaded; more remain" in preferences
    assert "entr(?:y|ies) loaded; that is the whole conversation" in preferences
    assert "entradas? cargadas?; quedan más" in preferences
    assert "entradas? cargadas?; es toda la conversación" in preferences


def test_distinct_answer_channels_are_compared_before_any_field_is_suppressed():
    detail = (ASSETS / "detail.js").read_text(encoding="utf-8")
    planner = (ASSETS / "answer-presentation.js").read_text(encoding="utf-8")

    assert 'from "./answer-presentation.js"' in detail
    assert "planAnswerPresentation(execution)" in detail
    assert "renderAnswerChannelReferences" in detail
    assert "answersEquivalent" in planner
    assert "selectedStructuredAnswerKey" not in detail
    assert "presentedResponseFields" not in detail


def test_evidence_preserves_record_multiplicity_instead_of_deduplicating():
    evidence = (ASSETS / "evidence.js").read_text(encoding="utf-8")

    assert "function recordedRecords" in evidence
    assert "uniqueExactRecords" not in evidence
    assert "exactValueKey" not in evidence


def test_derived_evidence_values_remain_translatable():
    evidence = (ASSETS / "evidence.js").read_text(encoding="utf-8")
    preferences = (ASSETS / "preferences.js").read_text(encoding="utf-8")

    assert "function interfaceRow" in evidence
    for english, spanish in (
        ("Execution log", "Registro de ejecución"),
        ("Ticket execution", "Ejecución del ticket"),
        ("Ticket job", "Trabajo del ticket"),
        ("No correlation", "Sin correlación"),
        ("Suggested, unconfirmed", "Sugerida, sin confirmar"),
        ("Verified by the producing workload", "Verificada por la carga de trabajo productora"),
        ("Confirmed by a reviewer", "Confirmada por un revisor"),
        ("Answered", "Respondió"),
        ("Did not answer", "No respondió"),
    ):
        assert f'["{english}", "{spanish}"]' in preferences


def test_every_allowlisted_evidence_identifier_and_warning_remains_visible():
    evidence = (ASSETS / "evidence.js").read_text(encoding="utf-8")
    detail = (ASSETS / "detail.js").read_text(encoding="utf-8")
    adapter = (ASSETS / "api.js").read_text(encoding="utf-8")

    for field in (
        "lookup_key_version",
        "ingress_key_version",
        "principal_hash",
        "provenance.correlation_status",
        "provenance.correlation_trust",
        "provenance.correlation_source",
        "provenance.missing_provenance",
        "link.review_id",
        "link.link_id",
        "link.source_url",
        "link.linked_by?.subject",
        "link.linked_by?.display_name",
        "evidence.provenance",
        "evidence.unavailable_reason",
        "evidence.warnings",
        "evidence.result_digest",
        "evidence.key_versions_queried",
        "evidence.truncated",
    ):
        assert field in evidence
    assert "candidate.candidate_token" not in evidence
    assert "Math.round(record.duration_ms)" not in evidence
    assert "chunk.score.toFixed" not in evidence
    assert 'dom.evidencePersisted.hidden = false' in detail
    assert "renderExecutionEvidence(dom.runSources, dom.runChunks, current.execution)" in detail
    assert "evidence: value.evidence ?? null" in adapter


def test_new_evidence_sections_and_identifiers_are_bilingual():
    preferences = (ASSETS / "preferences.js").read_text(encoding="utf-8")

    for english, spanish in (
        ("Identifiers", "Identificadores"),
        ("Model and prompt", "Modelo y prompt"),
        ("Index and deployment", "Índice y despliegue"),
        ("Lookup key version", "Versión de clave de búsqueda"),
        ("Ingress key version", "Versión de clave de ingreso"),
        ("Actor principal hash", "Hash del principal actor"),
        ("Review id", "ID de revisión"),
        ("Link id", "ID del enlace"),
        ("Source URL", "URL de origen"),
        ("Recorded evidence warnings", "Advertencias registradas de evidencia"),
        ("Recorded retrieval provenance", "Procedencia de recuperación registrada"),
        ("Reviewer subject", "Identificador de sujeto del revisor"),
        ("Reviewer display name", "Nombre visible del revisor"),
        ("Unavailable reason", "Motivo de indisponibilidad"),
        ("Exact timestamp", "Marca de tiempo exacta"),
    ):
        assert f'["{english}", "{spanish}"]' in preferences


def test_language_changes_rerender_dynamic_audit_counts_from_recorded_values():
    app = (ASSETS / "app.js").read_text(encoding="utf-8")

    assert 'document.addEventListener("preferenceschange"' in app
    assert "detail?.render(store.getState())" in app


def test_ticket_context_protects_only_recorded_values_not_interface_fallbacks():
    detail = (ASSETS / "detail.js").read_text(encoding="utf-8")
    evidence = (ASSETS / "evidence.js").read_text(encoding="utf-8")
    renderer = (ASSETS / "render.js").read_text(encoding="utf-8")
    preferences = (ASSETS / "preferences.js").read_text(encoding="utf-8")

    assert 'node.setAttribute("data-audit-value", "")' in detail
    assert 'node.removeAttribute("data-audit-value")' in detail
    assert 'attrs: { "data-audit-value": "", translate: "no" }' in evidence
    assert 'attrs: { "data-audit-value": "", translate: "no" }' in renderer
    assert '"#detail-meta dd"' not in preferences
    assert '".mono"' not in preferences

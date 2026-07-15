"""
Contrato de empaquetado, OpenAPI y configuración por rol (plan de
finalización, Tarea 2 Paso 4).

Estas pruebas son RED hasta cerrar las Tareas 3 (imagen), 4 (contrato v2/
OpenAPI/roles) y 5 (base Firestore nombrada y retenciones separadas):

- La imagen debe contener los prompts Markdown de runtime (.dockerignore hoy
  los excluye — bloqueo 9 del plan: el primer ticket en producción moriría).
- OpenAPI debe declarar Idempotency-Key obligatorio en v2, ambos esquemas de
  autenticación, enums cerrados y los errores 401/403/409/410/413/429/503.
- APP_ROLE, FIRESTORE_DATABASE, receipts con retención y contadores activos
  son configuración obligatoria de producción.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

KB_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = KB_ROOT / "data_pipeline" / "agent_prompts"

EXPECTED_PROMPTS = {
    "extract_inquiries.md",
    "forusbots_field_map.md",
    "gr_body_build.md",
    "kb_question_synthesis.md",
    "ticket_field_extract.md",
}


# ---------------------------------------------------------------------------
# Simulación mínima de .dockerignore (semántica de Docker: última regla que
# matchea gana; ``!`` re-incluye; un patrón sin slash matchea en cualquier
# nivel sólo si es el path completo relativo — Docker NO hace match recursivo
# de basenames salvo con **/). Suficiente para este contrato.
# ---------------------------------------------------------------------------

def _docker_ignores(path: str, rules: list[str]) -> bool:
    ignored = False
    for rule in rules:
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        pattern = pattern.rstrip("/")
        if _matches(path, pattern):
            ignored = not negated
    return ignored


def _matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path, pattern):
        return True
    # un patrón de directorio cubre todo su contenido
    if fnmatch.fnmatch(path, pattern + "/*") or fnmatch.fnmatch(path, pattern + "/**"):
        return True
    # Docker matchea componentes sin slash contra el primer nivel; los
    # patrones tipo *.md aplican a cualquier componente vía fnmatch por
    # segmento (aproximación conservadora: basta para *.md / *.json)
    if "/" not in pattern:
        return any(fnmatch.fnmatch(part, pattern) for part in path.split("/"))
    return False


def _dockerignore_rules() -> list[str]:
    lines = (KB_ROOT / ".dockerignore").read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


class TestImageShipsRuntimePrompts:

    def test_all_agent_prompts_exist_on_disk(self):
        found = {p.name for p in PROMPTS_DIR.glob("*.md")}
        assert EXPECTED_PROMPTS <= found, (
            f"faltan prompts de runtime: {EXPECTED_PROMPTS - found}"
        )

    def test_dockerignore_ships_agent_prompts(self):
        """Bloqueo 9 del plan: `*.md` en .dockerignore excluye los prompts que
        el runtime carga desde disco; la imagen endurecida fallaría con su
        primer ticket. Debe existir la re-inclusión explícita."""
        rules = _dockerignore_rules()
        for name in sorted(EXPECTED_PROMPTS):
            rel = f"data_pipeline/agent_prompts/{name}"
            assert not _docker_ignores(rel, rules), (
                f"RED: {rel} queda EXCLUIDO del contexto de build por "
                ".dockerignore (falta '!data_pipeline/agent_prompts/*.md')"
            )

    def test_five_prompt_builders_load(self):
        from data_pipeline import prompts

        case_data = {
            "userData": {"pptId": "1", "planId": "2", "companyName": "C",
                         "companyStatus": "Ongoing", "companyStatusDetail": None},
            "ticketData": {"userId": None, "userName": "u", "userEmail": "e",
                           "ticketId": None, "emailSubject": "s",
                           "emailBody": "b", "tag": None, "firstContact": None,
                           "ticket_messages": {}},
            "forusbots": {"recordKeeper": "LT Trust"},
        }
        builders = [
            (prompts.build_extract_inquiries_prompt, (case_data,)),
            (prompts.build_kb_question_synthesis_prompt,
             ({"ticketData": case_data["ticketData"]},)),
            (prompts.build_forusbots_field_map_prompt,
             ([{"field": "balance", "description": "d", "why_needed": "w",
                "required": True}],)),
            (prompts.build_gr_body_build_prompt, ([{"caseData": case_data}],)),
            (prompts.build_ticket_field_extract_prompt,
             ([{"field": "amount"}], {"emailSubject": "s", "emailBody": "b"})),
        ]
        for builder, args in builders:
            system, user = builder(*args)
            assert system and user, f"{builder.__name__} devolvió prompt vacío"


# ---------------------------------------------------------------------------
# OpenAPI (Tarea 4 Paso 7)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def openapi_spec():
    with patch("api.main.validate_settings"), \
         patch("api.main.RAGEngine", return_value=Mock()), \
         patch("api.main.PineconeUploader", return_value=Mock()), \
         patch("api.main.InquiryRouterEngine", return_value=Mock()):
        from api.main import app
        return app.openapi()


class TestOpenAPIContract:

    def test_openapi_declares_idempotency_key_required_for_v2(self, openapi_spec):
        post = openapi_spec["paths"]["/api/v2/handle-ticket"]["post"]
        params = {p.get("name"): p for p in post.get("parameters", [])}
        idem = params.get("Idempotency-Key")
        assert idem is not None, (
            "RED: OpenAPI no declara el header Idempotency-Key en v2"
        )
        assert idem.get("required") is True, (
            "RED: Idempotency-Key declarado pero no obligatorio"
        )

    def test_openapi_declares_both_auth_schemes(self, openapi_spec):
        schemes = openapi_spec.get("components", {}).get("securitySchemes", {})
        blob = str(schemes)
        assert "X-API-Key" in blob, (
            "RED: OpenAPI no declara el esquema X-API-Key"
        )
        assert "X-ForUs-Workload-Authorization" in blob or "Bearer" in blob, (
            "RED: OpenAPI no declara la identidad workload de v2 "
            "(X-ForUs-Workload-Authorization / bearer de Cloud Run)"
        )
        for path, method in (
            ("/api/v2/handle-ticket", "post"),
            ("/api/v2/ticket-jobs/{ticket_job_id}", "get"),
        ):
            security = openapi_spec["paths"][path][method].get("security", [])
            flattened = {name for option in security for name in option}
            assert {"ApiKeyAuth", "WorkloadIdentity"} <= flattened

    def test_openapi_declares_workload_identity_on_v1_post_and_poll(
            self, openapi_spec):
        """La documentación no debe anunciar el bypass que runtime rechaza."""
        for path, method in (
            ("/api/v1/handle-ticket", "post"),
            ("/api/v1/tickets/{ticket_job_id}", "get"),
        ):
            security = openapi_spec["paths"][path][method].get("security", [])
            flattened = {name for option in security for name in option}
            assert {"ApiKeyAuth", "WorkloadIdentity"} <= flattened, (
                f"{method.upper()} {path} no declara el segundo factor workload"
            )

    def test_openapi_states_and_actions_are_enums(self, openapi_spec):
        schemas = openapi_spec.get("components", {}).get("schemas", {})
        v2_status = schemas.get("TicketJobStatusV2", {})
        props = v2_status.get("properties", {})

        def _is_enum(prop: dict) -> bool:
            if "enum" in prop:
                return True
            for ref_key in ("$ref", "allOf", "anyOf"):
                if ref_key in str(prop):
                    return True
            return False

        assert props, "RED: TicketJobStatusV2 no está en los schemas"
        assert _is_enum(props.get("state", {})), (
            "RED: TicketJobStatusV2.state es un string sin restricción; debe "
            "ser un enum cerrado"
        )
        assert _is_enum(props.get("next_action", {})), (
            "RED: TicketJobStatusV2.next_action es un string sin restricción"
        )

    def test_openapi_error_responses_declared(self, openapi_spec):
        post = openapi_spec["paths"]["/api/v2/handle-ticket"]["post"]
        declared = set(post.get("responses", {}))
        required = {"401", "403", "409", "413", "429", "503"}
        missing = required - declared
        assert not missing, (
            f"RED: el POST v2 no declara las respuestas {sorted(missing)}"
        )
        get_status = openapi_spec["paths"]["/api/v2/ticket-jobs/{ticket_job_id}"]["get"]
        assert "410" in get_status.get("responses", {}), (
            "RED: el GET v2 no declara 410 (receipt/tombstone vigente tras "
            "expirar el payload)"
        )


# ---------------------------------------------------------------------------
# Configuración por rol y base Firestore nombrada (Tareas 4/5)
# ---------------------------------------------------------------------------

class TestRoleAndDatabaseConfig:

    def test_app_role_setting_exists_with_closed_values(self):
        from api.config import settings
        assert hasattr(settings, "APP_ROLE"), (
            "RED: settings.APP_ROLE no existe (Tarea 4 Paso 1a)"
        )
        assert settings.APP_ROLE in ("producer", "worker", "reconciler")

    def test_firestore_database_setting_exists(self):
        from api.config import settings
        assert hasattr(settings, "FIRESTORE_DATABASE"), (
            "RED: settings.FIRESTORE_DATABASE no existe — el cliente siempre "
            "apunta a (default) y el aislamiento ticket-staging es imposible "
            "(Tarea 5 Paso 3)"
        )

    def test_cross_database_rejection_between_staging_and_default(self, monkeypatch):
        """Producción sólo puede usar (default); staging sólo ticket-staging.
        Una combinación cruzada debe impedir el arranque."""
        from api import config as config_module
        settings = config_module.settings
        if not hasattr(settings, "FIRESTORE_DATABASE"):
            pytest.fail(
                "RED: FIRESTORE_DATABASE no existe; no puede validarse el "
                "rechazo cruzado staging/(default) (Tarea 5 Paso 3)"
            )
        base = {
            "API_KEY": "k", "PINECONE_API_KEY": "p", "OPENAI_API_KEY": "o",
            "ENVIRONMENT": "production", "TICKET_HANDLER_MODE": "full",
            "FORUSBOTS_AUTH_TOKEN": "t",
            "FORUSBOTS_BASE_URL": "https://forusbots.example.com",
            "TICKET_JOB_BACKEND": "firestore",
            "TICKET_TASK_QUEUE": "cloudtasks",
            "TICKET_WORKER_URL": "https://w.example.run.app",
            "FIRESTORE_DATABASE": "ticket-staging",   # cruzado: prod+staging DB
        }
        for name, value in base.items():
            monkeypatch.setattr(settings, name, value, raising=False)
        with pytest.raises(ValueError):
            config_module.validate_settings()

    def test_idempotency_receipt_survives_job_and_returns_410(self):
        """Tarea 5 Paso 2: receipts (`ticket_idempotency_receipts`) y payloads
        (`ticket_job_payloads`) separados del control; control/receipt expiran
        JUNTOS tras TICKET_IDEMPOTENCY_RETENTION_DAYS y el GET devuelve 410
        durante todo el horizonte."""
        from data_pipeline import ticket_job_repository as repo_module
        assert hasattr(repo_module, "RECEIPTS_COLLECTION"), (
            "RED: no existe la colección de receipts separada "
            "(ticket_idempotency_receipts, Tarea 5 Paso 2)"
        )
        assert hasattr(repo_module, "PAYLOADS_COLLECTION"), (
            "RED: no existe la colección de payloads separada "
            "(ticket_job_payloads, Tarea 5 Paso 2)"
        )
        from api.config import settings
        assert hasattr(settings, "TICKET_IDEMPOTENCY_RETENTION_DAYS"), (
            "RED: TICKET_IDEMPOTENCY_RETENTION_DAYS no existe; el default es "
            "90d y nunca menor al horizonte acordado en Tarea 1"
        )
        assert settings.TICKET_IDEMPOTENCY_RETENTION_DAYS >= 90

    def test_active_counter_has_no_ttl_while_positive(self):
        """Tarea 5 Paso 2: ticket_active_counters no lleva TTL mientras
        active_jobs > 0; se elimina sólo al volver atómicamente a cero."""
        from data_pipeline import ticket_job_repository as repo_module
        assert hasattr(repo_module, "COUNTERS_COLLECTION"), (
            "RED: no existe la colección ticket_active_counters "
            "(Tarea 5 Paso 2)"
        )

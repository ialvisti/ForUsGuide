"""Behavioral i18n contract for the reviewer-facing evidence audit.

The evidence renderer deliberately creates interface copy in English and lets
``preferences.js`` translate only allowlisted UI text.  These tests execute the
shipped translator rather than merely searching its source, so a candidate-only
response (no persisted provenance) cannot silently fall back to a half-English
confirmation form when Spanish is selected.
"""

from __future__ import annotations

import json
import subprocess

from api.tickets_console_main import UI_ASSETS_DIRECTORY


_TRANSLATION_HARNESS = r"""
const request = JSON.parse(process.argv[1]);
const { translateUiText } = await import(request.moduleUrl);
const rows = request.rows.map(([english, spanish]) => ({
  english,
  spanish,
  translated: translateUiText(english, "es"),
  restored: translateUiText(spanish, "en"),
}));
process.stdout.write(JSON.stringify(rows));
"""


def _translate(rows: list[tuple[str, str]]) -> list[dict[str, str]]:
    request = {
        "moduleUrl": (UI_ASSETS_DIRECTORY / "preferences.js").as_uri(),
        "rows": rows,
    }
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _TRANSLATION_HARNESS, json.dumps(request)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _assert_round_trip(rows: list[tuple[str, str]]) -> None:
    results = _translate(rows)
    for result in results:
        assert result["translated"] == result["spanish"], result["english"]
        assert result["restored"] == result["english"], result["spanish"]


def test_spanish_candidate_flow_with_empty_provenance_is_fully_guided() -> None:
    _assert_round_trip(
        [
            ("Suggestion 1", "Sugerencia 1"),
            (
                "These were not produced by the workload that answered the ticket, so "
                "the console will not treat them as evidence until a reviewer says "
                "they belong and why.",
                "Estos registros no fueron producidos por la carga de trabajo que "
                "respondió el ticket, por lo que la consola no los tratará como "
                "evidencia hasta que un revisor indique que corresponden y por qué.",
            ),
            (
                "Why this evidence belongs to this ticket",
                "Por qué esta evidencia corresponde a este ticket",
            ),
            (
                "Required. It is written to the audit ledger and cannot be edited later.",
                "Obligatorio. Se registra en el libro de auditoría y no se puede "
                "editar después.",
            ),
            ("Confirm this link", "Confirmar este enlace"),
            (
                "This suggestion has expired. Reload the ticket to get a current one.",
                "Esta sugerencia venció. Recarga el ticket para obtener una vigente.",
            ),
            ("Why this link is wrong", "Por qué este enlace es incorrecto"),
            (
                "Required. An unexplained unlink is indistinguishable from tampering "
                "when the record is read back years later.",
                "Obligatorio. Un enlace desvinculado sin explicación no se puede "
                "distinguir de una manipulación cuando el registro se consulte años después.",
            ),
            ("Unlink", "Desvincular"),
            ("Loading evidence…", "Cargando evidencia…"),
            (
                "No retrieval or prompt provenance is available for this ticket.",
                "No hay procedencia de recuperación o prompt disponible para este ticket.",
            ),
        ]
    )


def test_numbered_records_explanations_and_dynamic_gaps_round_trip() -> None:
    _assert_round_trip(
        [
            ("Complete recorded run", "Registro completo de la ejecución"),
            ("Lookup result digest", "Resumen del resultado de consulta"),
            ("Key versions queried", "Versiones de clave consultadas"),
            ("Lookup truncated", "Consulta truncada"),
            ("Retrieval record 2", "Registro de recuperación 2"),
            (
                "Unknown — this execution did not record an index version",
                "Desconocida — esta ejecución no registró una versión del índice",
            ),
            (
                "No observed vectors were recorded for this execution. That is a gap "
                "in what was logged, not proof that nothing was retrieved.",
                "No se registraron vectores observados para esta ejecución. Es un "
                "vacío en lo registrado, no una prueba de que no se recuperó nada.",
            ),
            (
                "These identifiers are what the retrieval step returned at the time. "
                "They are not stable across a reindex, so treat them as a trace of that "
                "one query rather than as addresses to look up later.",
                "Estos identificadores son los que devolvió el paso de recuperación en "
                "ese momento. No son estables después de una reindexación; considéralos "
                "una traza de esa consulta, no direcciones para consultar más adelante.",
            ),
            (
                "The rendered prompt hash identifies this one execution's prompt text. "
                "The template id and template hash are what identify the version of the "
                "prompt; the trace hash changes whenever the inputs do.",
                "El hash del prompt renderizado identifica el texto del prompt de esta "
                "ejecución. El ID y el hash de la plantilla identifican la versión del "
                "prompt; el hash de trazabilidad cambia cuando cambian las entradas.",
            ),
            (
                "No source article identifiers were recorded for this execution.",
                "No se registraron identificadores de artículos fuente para esta ejecución.",
            ),
            (
                "Article identifiers only. Article text is not read through this console, "
                "so what is shown here cannot drift from what was indexed.",
                "Solo se muestran identificadores de artículos. El texto de los artículos "
                "no se consulta desde esta consola, así que lo mostrado aquí no puede "
                "diferir de lo indexado.",
            ),
            (
                "Not recorded for this execution: index version, deployed revision, "
                "prompt template, model, observed vectors, response hash, source articles, "
                "a pre-Stage-4 record shape.",
                "No se registró para esta ejecución: versión del índice, revisión "
                "desplegada, plantilla de prompt, modelo, vectores observados, hash de "
                "respuesta, artículos fuente, un formato de registro anterior a la Etapa 4.",
            ),
            (
                "No retrieval or prompt provenance is available. Reported reason: "
                "future_reason_code.",
                "No hay procedencia de recuperación o prompt disponible. Motivo reportado: "
                "future_reason_code.",
            ),
        ]
    )

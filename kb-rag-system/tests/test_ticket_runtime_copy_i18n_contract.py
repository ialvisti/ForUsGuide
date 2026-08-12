"""Round-trip i18n contracts for reviewer-facing runtime guidance.

The console translates only a narrow allowlist of its own copy.  These tests
exercise the shipped translator so generated guidance can switch EN -> ES -> EN
without touching a remote email address or silently leaving English fragments.
"""

from __future__ import annotations

import json
import subprocess

from api.tickets_console_main import UI_ASSETS_DIRECTORY


_TRANSLATION_HARNESS = r"""
import fs from "node:fs";
const request = JSON.parse(fs.readFileSync(0, "utf8"));
const { translateUiText } = await import(request.moduleUrl);
const rows = request.rows.map(([english, spanish]) => {
  const translated = translateUiText(english, "es");
  return {
    english,
    spanish,
    translated,
    restored: translateUiText(translated, "en"),
  };
});
process.stdout.write(JSON.stringify(rows));
"""


def _round_trip(rows: list[tuple[str, str]]) -> list[dict[str, str]]:
    request = {
        "moduleUrl": (UI_ASSETS_DIRECTORY / "preferences.js").as_uri(),
        "rows": rows,
    }
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _TRANSLATION_HARNESS],
        check=True,
        capture_output=True,
        input=json.dumps(request),
        text=True,
    )
    results: list[dict[str, str]] = json.loads(completed.stdout)
    for result in results:
        assert result["translated"] == result["spanish"], result["english"]
        assert result["restored"] == result["english"], result["spanish"]
    return results


def test_static_runtime_guidance_round_trips_without_english_fragments() -> None:
    _round_trip(
        [
            (
                "The server reports these as unavailable in this build: "
                "remediation batches.",
                "El servidor informa que estas funciones no están disponibles "
                "en esta versión: lotes de remediación.",
            ),
            (
                "Machine-checked test evidence is attached by the remediation "
                "agent, not typed here, so a review closed from this screen has "
                "to say in words why the outcome is defensible.",
                "La evidencia de pruebas verificada automáticamente la adjunta "
                "el agente de remediación; no se escribe aquí. Por eso, al cerrar "
                "una revisión desde esta pantalla debes explicar con palabras por "
                "qué el resultado es defendible.",
            ),
            (
                "Historical classification retained for continuity. It is not the "
                "observation type above, and one is never derived from the other.",
                "Clasificación histórica conservada por continuidad. No corresponde "
                "al tipo de observación anterior; ninguno se deriva del otro.",
            ),
            ("Wrong route", "Ruta equivocada"),
            (
                "The root-cause taxonomy this console adds. Use “Wrong route” when the "
                "answer took the wrong path with the information it already had.",
                "Taxonomía de causa raíz que agrega esta consola. Usa “Ruta equivocada” "
                "cuando la respuesta tomó el camino incorrecto con la información que "
                "ya tenía.",
            ),
            (
                "No body is shown here because the upstream body was not plain text.",
                "Aquí no se muestra contenido porque el contenido original no era "
                "texto sin formato.",
            ),
            (
                "The server reports batches as disabled in this deployment, so a "
                "review cannot be added to one from here.",
                "El servidor informa que los lotes están deshabilitados en este "
                "despliegue, por lo que una revisión no se puede agregar a un lote "
                "desde aquí.",
            ),
            (
                "No remediation batch is selected. Choose reviews in the queue and "
                "create one to hand a group of observations to the agent.",
                "No hay ningún lote de remediación seleccionado. Elige revisiones "
                "en la cola y crea uno para entregar al agente un grupo de observaciones.",
            ),
            (
                "Required for every batch decision except “Mark ready”, and recorded "
                "in the append-only audit ledger. For “Start verification” this is "
                "your attestation that you did not author the change.",
                "Se requiere para cada decisión del lote excepto “Marcar como listo” "
                "y se registra en el registro de auditoría inmutable. Para “Iniciar "
                "verificación”, es tu constancia de que no creaste el cambio.",
            ),
            (
                "Shown only when the browser refuses clipboard access. The prompt is "
                "not stored by this console; it is fetched, handed over, and dropped.",
                "Se muestra solo cuando el navegador rechaza el acceso al portapapeles. "
                "Esta consola no almacena el prompt: se obtiene, se entrega y se descarta.",
            ),
        ]
    )


def test_assignment_admin_guidance_preserves_the_exact_remote_email() -> None:
    reviewer_suffix = (
        "You may take an unassigned review or release your own. Reassigning "
        "someone else's is an administrator action."
    )
    spanish_reviewer_suffix = (
        "Puedes tomar una revisión sin asignar o liberar una asignada a ti. "
        "Reasignar la revisión de otra persona requiere un administrador."
    )
    suffix = (
        "As an administrator you may take it or clear it. Handing it to a third "
        "person needs their verified sign-in identity, which no route publishes, "
        "so it is not offered here."
    )
    spanish_suffix = (
        "Como administrador, puedes tomarla o dejarla sin asignar. Entregarla a "
        "una tercera persona requiere su identidad verificada de inicio de sesión, "
        "que ninguna ruta publica, por lo que esa opción no se ofrece aquí."
    )
    remote_email = "remote.reviewer+qa@example.invalid"
    results = _round_trip(
        [
            (
                f"Unassigned. Saving your evaluation assigns it to you. {suffix}",
                "Sin asignar. Al guardar tu evaluación queda asignada a ti. "
                f"{spanish_suffix}",
            ),
            (
                "Unassigned. Saving your evaluation assigns it to you. "
                f"{reviewer_suffix}",
                "Sin asignar. Al guardar tu evaluación queda asignada a ti. "
                f"{spanish_reviewer_suffix}",
            ),
            (f"Assigned to you. {suffix}", f"Asignada a ti. {spanish_suffix}"),
            (
                f"Assigned to {remote_email}. {suffix}",
                f"Asignada a {remote_email}. {spanish_suffix}",
            ),
        ]
    )

    remote_result = results[-1]
    assert remote_result["translated"].count(remote_email) == 1
    assert remote_result["restored"].count(remote_email) == 1


def test_dynamic_body_length_round_trips() -> None:
    _round_trip(
        [
            (
                "The upstream body was 731 characters.",
                "El contenido original tenía 731 caracteres.",
            )
        ]
    )


def test_read_only_chat_copy_round_trips() -> None:
    _round_trip(
        [
            ("Messages", "Mensajes"),
            ("Unclassified", "Sin clasificar"),
            ("Read-only conversation", "Conversación de solo lectura"),
            (
                "Internal · not shown to participant",
                "Interno · no visible para el participante",
            ),
            ("Technical details", "Detalles técnicos"),
            (
                "Replying to an earlier message",
                "En respuesta a un mensaje anterior",
            ),
            (
                "No messages have loaded for this ticket.",
                "No se han cargado mensajes para este ticket.",
            ),
            (
                "18 entries loaded; that is the whole conversation. "
                "5 shown by this filter.",
                "18 entradas cargadas; es toda la conversación. "
                "5 mostradas por este filtro.",
            ),
            (
                "1 entry loaded; more remain. 1 shown by this filter.",
                "1 entrada cargada; quedan más. 1 mostrada por este filtro.",
            ),
        ]
    )


def test_primary_outcome_filter_copy_round_trips() -> None:
    _round_trip(
        [
            ("Outcome", "Resultado"),
            ("Request type", "Tipo de solicitud"),
            ("Any outcome", "Cualquier resultado"),
            ("Knowledge Question", "Pregunta de conocimiento"),
            ("Generate Response", "Generar respuesta"),
            (
                "Tickets ready for evaluation, newest received first.",
                "Tickets listos para evaluar, del más reciente al más antiguo.",
            ),
        ]
    )


def test_every_emitted_closing_requirement_combination_round_trips() -> None:
    english_parts = [
        "an outcome",
        "a verification summary",
        "a verification rationale, since no machine-checked test evidence is attached",
    ]
    spanish_parts = [
        "un resultado",
        "un resumen de verificación",
        "una justificación de verificación, ya que no se adjuntó evidencia de "
        "pruebas verificada automáticamente",
    ]
    rows: list[tuple[str, str]] = []
    for mask in range(1, 1 << len(english_parts)):
        english = ", ".join(
            part for index, part in enumerate(english_parts) if mask & (1 << index)
        )
        spanish = ", ".join(
            part for index, part in enumerate(spanish_parts) if mask & (1 << index)
        )
        rows.append(
            (
                f"Closing this review needs {english}.",
                f"Para cerrar esta revisión se requiere {spanish}.",
            )
        )

    _round_trip(rows)

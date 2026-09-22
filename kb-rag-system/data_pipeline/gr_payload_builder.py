"""
Builder determinístico de ``collected_data`` (Task 5 del plan, HT-03).

Los hechos scrapeados de ForusBots se transforman a la forma que consume
``/generate-response`` mediante los mappings tipados de este módulo — el
mismo spec que documenta ``agent_prompts/gr_body_build.md`` §5, ahora
ejecutado en código. El LLM ya NO copia balances, fechas, estatus ni ningún
otro valor fuente: sólo redacta (inquiry/topic).

Fuentes de collected_data, en orden de precedencia:
1. Módulos scrapeados (participant + plan) — autoritativos.
2. Valores extraídos del ticket con evidencia validada (sólo si el scrape
   no aportó el campo).
3. Metadata del request (company_name/company_status → plan_data).
"""

from __future__ import annotations

import re
import hashlib
from math import isfinite
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Tablas de mapping (gr_body_build.md §5) — Source field → target key
# ---------------------------------------------------------------------------

CENSUS_MAP: Dict[str, str] = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Eligibility Status": "employment_status",
    "Termination Date": "termination_date",
    "Rehire Date": "rehire_date",
    "Hire Date": "hire_date",
    "Birth Date": "birth_date",
    "Primary Email": "email",
    "Home Email": "home_email",
    "Phone": "phone",
    "Partial SSN": "partial_ssn",
    "SSN": "ssn",
    "Address 1": "address_line_1",
    "Address 2": "address_line_2",
    "City": "city",
    "State": "state",
    "Zip Code": "zip_code",
    "Projected Plan Entry Date": "projected_plan_entry_date",
    "Crypto Enrollment": "crypto_enrollment",
}

SAVINGS_MAP: Dict[str, str] = {
    "Account Balance": "account_balance",
    "Account Balance As Of": "account_balance_as_of",
    "Employer Match Vested Balance": "employer_match_vested_balance",
    "Formula": "employer_match_formula",
    "Timing": "employer_match_timing",
    "Employee Deferral Balance": "employee_deferral_balance",
    "Roth Deferral Balance": "roth_deferral_balance",
    "Rollover Balance": "rollover_balance",
    "Employer Match Balance": "employer_match_balance",
    "Loan Balance": "loan_balance",
    "Current Pre-tax Percent": "pretax_deferral_percent",
    "Current Pre-tax Amount": "pretax_deferral_amount",
    "Current Roth Percent": "roth_deferral_percent",
    "Current Roth Amount": "roth_deferral_amount",
    "YTD Employee contributions": "ytd_employee_contributions",
    "YTD Employer contributions": "ytd_employer_contributions",
    "Maxed out": "maxed_out",
    "Auto escalation rate": "auto_escalation_rate",
    "Auto escalation rate limit": "auto_escalation_rate_limit",
    "Auto escalation timing": "auto_escalation_timing",
}

# Campos de savings_rate que son de PLAN, no de participante:
SAVINGS_PLAN_MAP: Dict[str, str] = {
    "Record Keeper": "record_keeper",
    "Record Keeper Site": "record_keeper_site",
    "Plan enrollment type": "enrollment_type",
    "Employer Match Type": "employer_match_type",
}

LOANS_MAP: Dict[str, str] = {
    "Account Balance": "loan_account_balance",
    "Account Balance As Of": "loan_balance_as_of",
    "Loan History": "loan_history",
}
LOANS_OMIT = {"Participant Site"}
LOANS_PLAN_MAP: Dict[str, str] = {"Maximum Number of Loans": "max_loans"}

MFA_MAP: Dict[str, str] = {"MFA Status": "mfa_status"}

PLAN_DETAILS_MAP: Dict[str, str] = {
    "Plan Type": "plan_type",
    "Status": "plan_status",
    "Plan enrollment type": "enrollment_type",
    "Auto Enrollment Rate": "auto_enrollment_rate",
    "Minimum Age": "minimum_age",
    "Service Months": "service_months",
    "Service hours": "service_hours",
    "Plan Entry Frequency": "plan_entry_frequency",
    "Profit Sharing": "profit_sharing",
    "Force-out Limit": "force_out_limit",
    "Maximum Number of Loans": "max_loans",
    "Employer Contribution Type": "employer_contribution_type",
    "Formula": "employer_match_formula",
    "Employer Match Timing": "employer_match_timing",
    "Plan Documents": "plan_documents_url",
    "Participant Site": "participant_site_url",
}

PAYROLL_STATIC_MAP: Dict[str, str] = {
    "Latest Payroll": "latest_payroll",
    "Payroll Frequency": "payroll_frequency",
    "Next Schedule paycheck": "next_scheduled_paycheck",
}
PAYROLL_OMIT = {"Available Years"}
_PAYROLL_YEAR_RE = re.compile(r"^Payroll (\d{4})$")

# planDataModules (plan scrape) → plan_data. Claves ya snake_case.
PLAN_MODULES_RENAMES: Dict[Tuple[str, str], str] = {
    ("plan_design", "enrollment_type"): "enrollment_type",
    ("plan_design", "eligibility_min_age"): "minimum_age",
    ("plan_design", "employer_contribution"): "employer_match_type",
    ("plan_design", "employer_contribution_timing"): "employer_match_timing",
    ("plan_design", "default_savings_rate"): "default_savings_rate",
    ("plan_design", "autoescalate_rate"): "auto_escalation_rate",
    ("plan_design", "alts_crypto"): "crypto_enabled",
    ("plan_design", "max_crypto_percent_balance"): "max_crypto_percent_balance",
    ("basic_info", "ein"): "ein",
    ("basic_info", "effective_date"): "plan_effective_date",
    ("basic_info", "official_plan_name"): "legal_plan_name",
    ("basic_info", "status"): "plan_status",
    ("basic_info", "status_as_of"): "plan_status_as_of",
    ("basic_info", "active"): "plan_active",
}
# Conceptos donde planDataModules es más autoritativo que plan_details:
_PLAN_MODULE_WINS = {"enrollment_type", "plan_status", "minimum_age",
                     "employer_match_type", "employer_match_timing"}


def snake_case(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", str(name).strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


_INTERNAL_HISTORY_STATUSES = frozenset({
    "ok", "empty", "panel_missing", "parse_error", "legacy_notes",
})
_KNOWN_PLAN_STATUSES = frozenset({
    "active", "ongoing", "actively_managed", "terminated", "inactive",
    "implementation", "in_implementation", "frozen", "closed",
    "deconverted", "pending_termination",
})
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _normalize_lifecycle_date(value: Any) -> Optional[str]:
    text = str(value or "").strip().replace("–", "-").replace("—", "-")
    text = re.sub(r"\b(\d{4})/(\d{2})/(\d{2})\b", r"\1-\2-\3", text)
    match = _ISO_DATE_RE.search(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = _US_DATE_RE.search(text)
        if not match:
            return None
        month, day, year = (int(part) for part in match.groups())
    if not (1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _normalize_plan_status(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    canonical = re.sub(r"[\s-]+", "_", text.lower())
    if canonical not in _KNOWN_PLAN_STATUSES:
        return None
    return text[:50]


def _normalize_active(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _append_fact(facts: List[str], fact: Optional[str]) -> None:
    if fact and fact not in facts and len(facts) < 12:
        facts.append(fact[:240])


_UNSAFE_NOTE_RE = re.compile(
    r"\b(?:if|hypothetical|draft|example|not|never|might|could)\b|"
    r"ignore\s+(?:all|previous)|(?:system|developer)\s*(?:prompt|message)|"
    r"(?:tell|instruct)\s+(?:the\s+)?(?:participant|assistant)|"
    r"(?:email|send|reveal|expose)\s+(?:private|secret|password|credential)", re.I,
)
# Closed, source-observed institution vocabulary. This maps an explicit note
# label to a display value; it does not infer a provider from plan/payroll IDs.
_SUCCESSOR_RECORDKEEPERS = {"fidelity": "Fidelity", "adp": "ADP"}


def _operational_note_facts(entry: Mapping[str, Any], *, record_index: int = 0) -> List[Dict[str, Any]]:
    note = entry.get("note")
    if not isinstance(note, str) or _UNSAFE_NOTE_RE.search(note) or entry.get("noteTruncated") is True:
        return []
    text = note[:2000]
    lowered = text.lower()
    recorded_at = _normalize_lifecycle_date(entry.get("recordedAt", entry.get("occurredAt")))
    effective_on = _normalize_lifecycle_date(entry.get("effectiveOn"))
    if effective_on is None:
        match = re.search(r"effective\s+date\s*:\s*([0-9/–—-]{8,10})", text, re.I)
        if match:
            effective_on = _normalize_lifecycle_date(match.group(1))
    source = entry.get("source")
    if source not in {"notes", "timeline"}:
        return []
    facts: List[Dict[str, Any]] = []
    # Snapshot-local correlation only: no note text, author or participant ID
    # contributes to this opaque reference. Separate same-date records differ.
    record_ref = hashlib.sha256(
        f"{source}:{record_index}:{recorded_at}:{effective_on}".encode("utf-8")
    ).hexdigest()[:24]

    def add(kind: str, value: Any) -> None:
        facts.append({"kind": kind, "value": value, "source": f"plan_history.{source}",
                      "recorded_at": recorded_at, "effective_on": effective_on,
                      "record_ref": record_ref})

    # A source-observed past-tense plan assertion identifies a reported servicing
    # successor, not an individual asset transfer. Keep its effective date unknown
    # and do not upgrade it to the stronger completed-transfer evidence below.
    plain_successor = re.search(
        r"\bplan\s+(?:was\s+|has been\s+)?deconverted\s+to\s+(fidelity|adp)\b",
        lowered,
    )
    if plain_successor and not re.search(
        r"\b(?:may|will|pending|planned|incorrect|wrong|supposed|mistake)\b|\?", lowered,
    ) and len(set(re.findall(r"\b(?:fidelity|adp)\b", lowered))) == 1:
        add("servicing_successor_reported", _SUCCESSOR_RECORDKEEPERS[plain_successor.group(1)])
        return facts

    # Bind Status to the labeled event, not to another completed task in
    # the same note. Free narrative cannot confirm a completed transfer.
    event_matches = list(re.finditer(r"\bevent\s*:\s*([^\n.;]+)", lowered))
    # Multiple events in one record require review; never borrow the status or
    # provider from an unrelated event or from instructions in the Notes body.
    if len(event_matches) != 1:
        return []
    event_match = event_matches[0]
    event = event_match.group(1)
    is_transition = bool(re.match(r"(?:(?:401\(k\)|401k)\s+)?(?:plan\s+)?(?:deconversion|termination)\b", event))
    is_deconversion = is_transition and bool(re.search(r"\bdeconversion\b", event))
    header = re.split(r"\bnotes\s*:", lowered[event_match.start():], maxsplit=1)[0]
    status_match = re.search(r"\bstatus\s*:\s*(completed|pending|ongoing|cancelled|canceled)\b", header) if is_transition else None
    transition_status = None
    if is_transition and status_match:
        raw = status_match.group(1)
        transition_status = {"ongoing": "pending", "canceled": "cancelled"}.get(raw, raw)
        if is_deconversion:
            add("custody_transition_status", transition_status)
    # Completed plan termination alone does not establish a custody transfer.
    # Require the deconversion event plus transfer evidence and explicit provider.
    transfer_confirmed = (
        transition_status == "completed" and is_deconversion
        and bool(re.search(r"assets and records (?:were|have been) transferred", lowered))
    )
    if transfer_confirmed:
        for token, name in _SUCCESSOR_RECORDKEEPERS.items():
            if re.search(rf"\b{re.escape(token)} is now the new recordkeeper\b", lowered) or re.search(
                rf"\bnew recordkeeper\s*:\s*{re.escape(token)}(?:[.;\n]|$)", lowered
            ):
                add("successor_recordkeeper", name)
    if is_transition and re.search(
        r"distributions (?:have been placed |are |remain |)?on hold\b", lowered
    ):
        add("distribution_hold", True)
    elif is_transition and re.search(
        r"distribution(?:s)? hold (?:has been |was )?(?:lifted|released)\b", lowered
    ):
        add("distribution_hold", False)
    return facts


def _facts_from_lifecycle_note(note: Any, event_date: Optional[str]) -> List[str]:
    """Extract a tiny closed fact vocabulary; never return the note itself."""
    if not isinstance(note, str) or _UNSAFE_NOTE_RE.search(note):
        return []
    lowered = note.lower()[:2000]
    facts: List[str] = []
    if "deconvert" in lowered or "deconversion" in lowered:
        suffix = f" on {event_date}" if event_date else ""
        facts.append(f"Plan deconversion is recorded{suffix}.")
    payroll_match = re.search(
        r"last\s+payroll(?:\s+we\s+should\s+process)?[^0-9]{0,40}"
        r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})",
        lowered,
    )
    if payroll_match:
        payroll_date = _normalize_lifecycle_date(payroll_match.group(1))
        if payroll_date:
            facts.append(
                f"Last payroll date recorded for deconversion: {payroll_date}."
            )
    return facts


def _build_internal_plan_context(
    plan_modules: Mapping[str, Any],
    plan_meta: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Reduce plan notes/history to safe lifecycle facts for reasoning.

    Arbitrary note text, authors, and unrecognized changes are intentionally
    discarded. Only closed-vocabulary statuses, booleans, dates and two
    operational signals survive with their original source dates.
    """
    history = plan_modules.get("plan_history")
    notes = plan_modules.get("plan_notes")
    basic = plan_modules.get("basic_info")
    if not isinstance(history, Mapping) and not isinstance(notes, list) and not isinstance(basic, Mapping):
        return None

    raw_extraction_status = (
        history.get("extractionStatus", history.get("extraction_status"))
        if isinstance(history, Mapping) else "legacy_notes" if isinstance(notes, list) else "panel_missing"
    )
    extraction_status = (
        raw_extraction_status
        if isinstance(raw_extraction_status, str) and raw_extraction_status in _INTERNAL_HISTORY_STATUSES
        else "parse_error"
    )
    current: Dict[str, Any] = {}
    history_current = history.get("current") if isinstance(history, Mapping) else None
    if not isinstance(history_current, Mapping):
        history_current = {}
    if not isinstance(basic, Mapping):
        basic = {}
    basic_error = _source_diagnostic(plan_meta, "basic_info", "status").get("data_state")
    if basic_error in _ERROR_DATA_STATES | _UNAVAILABLE_DATA_STATES:
        basic = {}
        history_current = {}

    status = _normalize_plan_status(
        history_current.get("status", basic.get("status", basic.get("Status")))
    )
    if status is not None:
        current["status"] = status
    active = _normalize_active(
        history_current.get("active", basic.get("active", basic.get("Active")))
    )
    if active is not None:
        current["active"] = active
    status_as_of = _normalize_lifecycle_date(
        history_current.get(
            "statusAsOf",
            history_current.get(
                "status_as_of", basic.get("status_as_of", basic.get("Status as of"))
            ),
        )
    )
    if status_as_of:
        current["status_as_of"] = status_as_of

    facts: List[str] = []
    operational_facts: List[Dict[str, Any]] = []
    entries = history.get("entries") if isinstance(history, Mapping) else None
    if isinstance(entries, list) and extraction_status == "ok":
        for record_index, entry in enumerate(entries[:50]):
            if not isinstance(entry, Mapping):
                continue
            recorded_date = _normalize_lifecycle_date(
                entry.get("recordedAt", entry.get("occurredAt"))
            )
            effective_date = _normalize_lifecycle_date(entry.get("effectiveOn"))
            operational_facts.extend(_operational_note_facts(entry, record_index=record_index))
            effective_label = "snapshot effective" if entry.get("source") == "timeline" else "effective"
            when = (f"; recorded {recorded_date}" if recorded_date else "") + (
                f"; {effective_label} {effective_date}" if effective_date else ""
            )
            changes = entry.get("changes")
            if isinstance(changes, list):
                for change in changes[:20]:
                    if not isinstance(change, Mapping):
                        continue
                    field_name = str(change.get("field") or "").strip().lower()
                    if field_name == "status":
                        old = _normalize_plan_status(change.get("from"))
                        new = _normalize_plan_status(change.get("to"))
                        if new:
                            detail = f" from {old}" if old else ""
                            _append_fact(
                                facts, f"Plan status changed{detail} to {new}{when}."
                            )
                    elif field_name == "active":
                        old_active = _normalize_active(change.get("from"))
                        new_active = _normalize_active(change.get("to"))
                        if new_active is not None:
                            detail = (
                                f" from {str(old_active).lower()}"
                                if old_active is not None else ""
                            )
                            _append_fact(
                                facts,
                                "Plan active flag changed"
                                f"{detail} to {str(new_active).lower()}{when}.",
                            )
                    elif field_name in {
                        "terminated_status_as_of", "actively_managed_status_as_of",
                        "pending_termination_status_as_of"
                    }:
                        change_effective_date = _normalize_lifecycle_date(change.get("to"))
                        if change_effective_date:
                            _append_fact(
                                facts,
                                f"Plan lifecycle effective date is {change_effective_date}.",
                            )
            if entry.get("noteTruncated") is not True:
                for fact in _facts_from_lifecycle_note(entry.get("note"), recorded_date):
                    _append_fact(facts, fact)

    # Legacy v1 note arrays remain accepted, but only recognized facts survive.
    if isinstance(notes, list) and extraction_status == "legacy_notes":
        for note in notes[:50]:
            for fact in _facts_from_lifecycle_note(note, None):
                _append_fact(facts, fact)

    completeness = {"complete": None, "truncated": None}
    if isinstance(history, Mapping):
        from data_pipeline.forusbots_catalog import _safe_completeness
        completeness = _safe_completeness(history.get("completeness"))
    return {
        "extraction_status": extraction_status,
        "current": current,
        "lifecycle_facts": facts,
        "operational_facts": operational_facts,
        "completeness": completeness,
    }


def _strip_pay_date_url(value: Any) -> Any:
    """Elimina ``Pay Date URL`` de un objeto payroll o de sus rows."""
    if isinstance(value, dict):
        return {
            k: _strip_pay_date_url(v)
            for k, v in value.items()
            if k != "Pay Date URL"
        }
    if isinstance(value, list):
        return [_strip_pay_date_url(item) for item in value]
    return value


def _map_simple(module_data: Mapping[str, Any], table: Mapping[str, str],
                target: Dict[str, Any], *, omit: frozenset = frozenset(),
                collision_prefix: str = "") -> None:
    for field_name, value in module_data.items():
        if field_name in omit:
            continue
        key = table.get(field_name)
        if key is None:
            # Regla genérica 5x: nunca descartar datos scrapeados en silencio.
            key = snake_case(field_name)
            if key in target and collision_prefix:
                key = f"{collision_prefix}_{key}"
        target[key] = value


def _map_payroll(module_data: Mapping[str, Any], target: Dict[str, Any]) -> None:
    years: Dict[str, Any] = {}
    for field_name, value in module_data.items():
        if field_name in PAYROLL_OMIT:
            continue
        year_match = _PAYROLL_YEAR_RE.match(field_name)
        if year_match:
            years[year_match.group(1)] = _strip_pay_date_url(value)
            continue
        key = PAYROLL_STATIC_MAP.get(field_name)
        if key == "latest_payroll":
            target[key] = _strip_pay_date_url(value)
            continue
        if key is None:
            key = snake_case(field_name)
        target[key] = value
    if years:
        target["payroll_years"] = years


def _map_loans(module_data: Mapping[str, Any], participant: Dict[str, Any],
               plan: Dict[str, Any]) -> None:
    for field_name, value in module_data.items():
        if field_name in LOANS_OMIT:
            continue
        if field_name in LOANS_PLAN_MAP:
            plan[LOANS_PLAN_MAP[field_name]] = value
            continue
        key = LOANS_MAP.get(field_name)
        if key == "loan_history":
            if isinstance(value, str):
                value = [] if _EMPTY_LOAN_HISTORY_RE.fullmatch(value.strip()) else None
            elif not isinstance(value, list):
                value = None
        if key is None:
            key = snake_case(field_name)
            if key in participant:
                key = f"loans_{key}"
        participant[key] = value


def _map_mfa(module_data: Mapping[str, Any], target: Dict[str, Any]) -> None:
    for field_name, value in module_data.items():
        key = MFA_MAP.get(field_name) or snake_case(field_name)
        if key == "mfa_status" and isinstance(value, str) and value.islower():
            value = value.title()          # "enrolled" → "Enrolled"
        target[key] = value


def _map_savings(module_data: Mapping[str, Any], participant: Dict[str, Any],
                 plan: Dict[str, Any]) -> None:
    for field_name, value in module_data.items():
        if field_name in SAVINGS_PLAN_MAP:
            plan[SAVINGS_PLAN_MAP[field_name]] = value
            continue
        key = SAVINGS_MAP.get(field_name) or snake_case(field_name)
        participant[key] = value


def _map_plan_modules(plan_modules: Mapping[str, Any],
                      plan: Dict[str, Any]) -> None:
    """planDataModules del plan scrape. Precedencia: gana sobre los campos
    de plan derivados del participant scrape para los conceptos listados."""
    duration_value: Any = None
    duration_unit: Any = None
    for module_name, module_data in plan_modules.items():
        if module_name in {"plan_notes", "plan_history"}:
            continue
        if not isinstance(module_data, Mapping):
            plan[snake_case(module_name)] = module_data
            continue
        for field_name, value in module_data.items():
            if value == "":
                continue
            if (module_name, field_name) == ("plan_design", "eligibility_duration_value"):
                duration_value = value
                continue
            if (module_name, field_name) == ("plan_design", "eligibility_duration_unit"):
                duration_unit = value
                continue
            if (module_name, field_name) == ("plan_design", "record_keeper_id"):
                plan.setdefault("record_keeper", value)
                continue
            key = PLAN_MODULES_RENAMES.get((module_name, field_name))
            if key is None:
                key = snake_case(field_name)
            plan[key] = value
    if duration_value is not None or duration_unit is not None:
        plan["eligibility_duration"] = " ".join(
            str(p) for p in (duration_value, duration_unit) if p is not None
        )


# Conceptos que SÓLO se derivan en código (nunca del LLM ni de la extracción
# de ticket). Defense-in-depth: aunque una clave inventada burle la allowlist
# del extractor, jamás rellena estos campos (P1 del review final).
_CODE_DERIVED_CONCEPTS = frozenset({"first_contribution_posted_status"})

# Columnas de payroll que representan dinero POSTEADO a la cuenta.
_CONTRIBUTION_COLUMNS = ("Pre-tax", "Roth", "After-tax", "Employer Match")


def _money(value: Any) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def derive_first_contribution_posted_status(
    participant: Mapping[str, Any],
) -> Optional[bool]:
    """Resolver del CONCEPTO ``first_contribution_posted_status`` (Task 9).

    Regla del artículo (Managing Your 401(k) Statements and Beneficiaries):
    debe existir una contribución POSITIVA posteada; una entrada de $0.00 no
    es evidencia; un refund/negativo no cuenta. Sin datos de payroll el
    estatus es DESCONOCIDO (None) — nunca False por ausencia de datos.

    Limitación documentada: evalúa los datos scrapeados (Latest Payroll +
    año corriente); contribuciones sólo en años anteriores no scrapeados no
    son visibles para este resolver.
    """
    rows: list = []
    latest = participant.get("latest_payroll")
    if isinstance(latest, Mapping):
        rows.append(latest)
    years = participant.get("payroll_years")
    if isinstance(years, Mapping):
        for year_table in years.values():
            if not isinstance(year_table, Mapping):
                continue
            year_rows = year_table.get("Rows")
            if isinstance(year_rows, list):
                rows.extend(r for r in year_rows if isinstance(r, Mapping))
            else:
                rows.append(year_table)
    if not rows:
        return None
    for row in rows:
        if any(_money(row.get(col)) > 0 for col in _CONTRIBUTION_COLUMNS):
            return True
    return False


_EMPTY_LOAN_HISTORY_RE = re.compile(
    r"There['’]s no Loan History for this Participant\.?", re.I,
)
_ERROR_DATA_STATES = frozenset({"panel_missing", "parse_error", "access_denied"})
_UNAVAILABLE_DATA_STATES = frozenset({"missing", "unavailable"})


def _source_diagnostic(meta: Optional[Mapping[str, Any]], module: str,
                       label: str) -> Dict[str, Any]:
    """Read only closed extraction metadata, including legacy envelope errors."""
    source = meta or {}
    diagnostics = source.get("extraction_diagnostics")
    modules = diagnostics.get("modules") if isinstance(diagnostics, Mapping) else None
    module_data = modules.get(module) if isinstance(modules, Mapping) else None
    result: Dict[str, Any] = {}
    if isinstance(module_data, Mapping):
        module_state = module_data.get("dataState")
        fields = module_data.get("fields")
        field = fields.get(label) if isinstance(fields, Mapping) else None
        field_state = field.get("dataState") if isinstance(field, Mapping) else None
        # A module failure cannot be converted to success by stale field data.
        result["data_state"] = (
            module_state if module_state in _ERROR_DATA_STATES else field_state or module_state
        )
        result["observed_at"] = module_data.get("observedAt")
        if isinstance(field, Mapping):
            result["as_of"] = _normalize_lifecycle_date(field.get("sourceAsOf"))
        completeness = module_data.get("completeness")
        if isinstance(completeness, Mapping):
            result["complete"] = completeness.get("complete")
    legacy_status = source.get("module_status")
    if isinstance(legacy_status, Mapping) and legacy_status.get(module) in {
        "error", "failed", "canceled", "unknown",
    }:
        result["data_state"] = "parse_error"
    return result


def _strict_money(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"-?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        return None
    try:
        amount = Decimal(text.replace("$", "").replace(",", ""))
        numeric = float(amount)
        return numeric if amount.is_finite() and isfinite(numeric) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _typed_source(value: Any, *, module: str, label: str,
                  meta: Optional[Mapping[str, Any]], as_of: Any = None,
                  boolean: bool = False) -> Dict[str, Any]:
    evidence = _source_diagnostic(meta, module, label)
    data_state = evidence.get("data_state")
    typed_value: Any = _normalize_active(value) if boolean else _strict_money(value)
    status = "known" if typed_value is not None else "unknown"
    if data_state in _ERROR_DATA_STATES:
        status, typed_value = "error", None
    elif data_state in _UNAVAILABLE_DATA_STATES:
        status, typed_value = "unknown", None
    return {
        "value": typed_value, "status": status,
        "source": f"participant.{module}.{label}",
        "as_of": evidence.get("as_of") or _normalize_lifecycle_date(as_of),
        "observed_at": evidence.get("observed_at"),
    }


def _unknown_source() -> Dict[str, Any]:
    return {"value": None, "status": "unknown", "source": None,
            "as_of": None, "observed_at": None}


def project_verified_participant_facts(value: Any) -> Dict[str, Any]:
    """Narrow approved disclosure contract, independent of model assertions."""
    from data_pipeline.forusbots_catalog import _safe_diagnostic_timestamp

    if not isinstance(value, Mapping) or value.get("identity_verified") is not True or value.get("identity_resolution_status") != "matched":
        return {}
    raw = value.get("facts")
    if not isinstance(raw, Mapping):
        return {}
    allowed = {"first_name": "participant.census.First Name"}
    allowed.update({SAVINGS_MAP[label]: f"participant.savings_rate.{label}" for label in (
        "Account Balance", "Employee Deferral Balance", "Roth Deferral Balance",
        "Rollover Balance", "Employer Match Balance", "Employer Match Vested Balance",
    )})
    facts: Dict[str, Any] = {}
    for key, source in allowed.items():
        entry = raw.get(key)
        if not isinstance(entry, Mapping) or entry.get("source") != source or entry.get("status") != "known":
            continue
        observed = _safe_diagnostic_timestamp(entry.get("observed_at"))
        as_of = _normalize_lifecycle_date(entry.get("as_of"))
        if not observed and not as_of:
            continue
        item = entry.get("value")
        if key == "first_name":
            valid = isinstance(item, str) and 0 < len(item.strip()) <= 80 and all(c.isalpha() or c in " .'-" for c in item)
        else:
            valid = type(item) in (int, float) and isfinite(item)
        if valid:
            facts[key] = {"value": item, "status": "known", "source": source,
                          "observed_at": observed, "as_of": as_of}
    return {"identity_verified": True, "identity_resolution_status": "matched", "facts": facts} if facts else {}


# Plan-side disclosure. These three identifiers are PLAN attributes read from the
# plan scrape; they are not participant account numbers and never identify a
# receiving account. The routed/internal plan_id only BINDS them — it is never a
# value, and no plan fact may select the plan it supposedly belongs to.
_PLAN_FACT_SOURCES: Dict[str, Tuple[str, str]] = {
    "legal_plan_name": ("basic_info", "official_plan_name"),
    "rk_plan_id": ("plan_design", "rk_plan_id"),
    "record_keeper": ("plan_design", "record_keeper_id"),
}
_PLAN_FACT_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "legal_plan_name": re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&()/-]{0,199}"),
    # Conservative recordkeeper code syntax: no spaces, punctuation or markup.
    "rk_plan_id": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}"),
    "record_keeper": re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&()/-]{0,99}"),
}
_PLAN_FACT_SENTINELS = frozenset({
    "unknown", "n/a", "na", "none", "null", "-", "--", "tbd",
    "not available", "not applicable", "pending",
})
_CANONICAL_PLAN_ID_RE = re.compile(r"[1-9][0-9]{0,31}")


def canonical_plan_id(value: Any) -> Optional[str]:
    """Only a positive canonical numeric plan identifier can bind plan facts."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _CANONICAL_PLAN_ID_RE.fullmatch(text) else None


def project_verified_plan_facts(value: Any) -> Dict[str, Any]:
    """Narrow plan disclosure contract, bound to a separately selected plan."""
    from data_pipeline.forusbots_catalog import _safe_diagnostic_timestamp

    if not isinstance(value, Mapping):
        return {}
    plan_id = canonical_plan_id(value.get("plan_id"))
    if plan_id is None or value.get("identity_verified") is not True \
            or value.get("identity_resolution_status") != "matched":
        return {}
    raw = value.get("facts")
    if not isinstance(raw, Mapping):
        return {}
    facts: Dict[str, Any] = {}
    for key, (module, label) in _PLAN_FACT_SOURCES.items():
        entry = raw.get(key)
        source = f"plan.{module}.{label}"
        if not isinstance(entry, Mapping) or entry.get("source") != source \
                or entry.get("status") != "known":
            continue
        raw_observed = entry.get("observed_at")
        raw_as_of = entry.get("as_of")
        observed = _safe_diagnostic_timestamp(raw_observed)
        as_of = _normalize_lifecycle_date(raw_as_of)
        # A malformed supplied date is not equivalent to an absent optional
        # date and cannot borrow validity from the other chronology field.
        if ("observed_at" in entry and raw_observed is not None and observed is None) \
                or ("as_of" in entry and raw_as_of is not None and as_of is None):
            continue
        if not observed and not as_of:
            continue
        item = entry.get("value")
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text.lower() in _PLAN_FACT_SENTINELS or not _PLAN_FACT_PATTERNS[key].fullmatch(text):
            continue
        facts[key] = {"value": text, "status": "known", "source": source,
                      "observed_at": observed, "as_of": as_of}
    if not facts:
        return {}
    return {"plan_id": plan_id, "identity_verified": True,
            "identity_resolution_status": "matched", "facts": facts}


def _build_plan_disclosure_context(
    plan_modules: Optional[Mapping[str, Any]],
    plan_meta: Optional[Mapping[str, Any]],
    identity: Optional[Mapping[str, Any]],
    selected_plan_id: Optional[str],
) -> Dict[str, Any]:
    """Project plan identifiers from the scrape only, for the selected plan.

    ``selected_plan_id`` is the plan the authenticated caller requested. Ticket
    text, model output and a plan fact's own ``plan_id`` cannot select a plan.
    """
    if not isinstance(identity, Mapping) or identity.get("identity_verified") is not True \
            or identity.get("identity_resolution_status") != "matched":
        return {}
    if canonical_plan_id(selected_plan_id) is None or not isinstance(plan_modules, Mapping):
        return {}
    facts: Dict[str, Any] = {}
    for key, (module, label) in _PLAN_FACT_SOURCES.items():
        module_data = plan_modules.get(module)
        if not isinstance(module_data, Mapping) or label not in module_data:
            continue
        evidence = _source_diagnostic(plan_meta, module, label)
        if evidence.get("data_state") != "ok":
            continue
        facts[key] = {"value": module_data.get(label), "status": "known",
                      "source": f"plan.{module}.{label}",
                      "observed_at": evidence.get("observed_at"),
                      "as_of": evidence.get("as_of")}
    return project_verified_plan_facts({
        "plan_id": selected_plan_id, "identity_verified": True,
        "identity_resolution_status": "matched", "facts": facts,
    })


def _build_disclosure_context(participant: Mapping[str, Any], preflight: Mapping[str, Any],
                              meta: Optional[Mapping[str, Any]], identity: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(identity, Mapping) or identity.get("identity_verified") is not True or identity.get("identity_resolution_status") != "matched":
        return {}
    facts = dict(preflight.get("sources") or {})
    name_evidence = _source_diagnostic(meta, "census", "First Name")
    if name_evidence.get("data_state") == "ok":
        facts["first_name"] = {"value": participant.get("first_name"), "status": "known",
                               "source": "participant.census.First Name", "observed_at": name_evidence.get("observed_at")}
    return project_verified_participant_facts({**identity, "facts": facts})


def _build_preflight_context(participant: Mapping[str, Any],
                             meta: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    sources: Dict[str, Any] = {}
    for label in ("Account Balance", "Employee Deferral Balance", "Roth Deferral Balance",
                  "Rollover Balance", "Employer Match Balance", "Employer Match Vested Balance"):
        key = SAVINGS_MAP[label]
        sources[key] = _typed_source(
            participant.get(key), module="savings_rate", label=label, meta=meta,
            as_of=participant.get("account_balance_as_of") if key == "account_balance" else None,
        )
    # Neither absent after-tax nor total vested can be reconstructed by subtracting
    # asynchronously updated source balances from the account total.
    sources["after_tax_balance"] = _unknown_source()
    sources["vested_balance"] = _unknown_source()

    history = participant.get("loan_history")
    loan_evidence = _source_diagnostic(meta, "loans", "Loan History")
    loan_balance = _typed_source(participant.get("loan_balance"), module="savings_rate",
                                 label="Loan Balance", meta=meta)
    state = loan_evidence.get("data_state")
    loans: Dict[str, Any] = {
        "status": "unknown", "outstanding_status": "unknown",
        "source": "participant.loans.Loan History", "as_of": loan_evidence.get("as_of"),
        "observed_at": loan_evidence.get("observed_at"),
        "complete": loan_evidence.get("complete"),
    }
    if state in _ERROR_DATA_STATES:
        loans["status"] = "error"
    elif state not in _UNAVAILABLE_DATA_STATES and isinstance(history, list):
        loans["status"] = "known" if history else "empty"
        balances = [_strict_money(row.get("Outstanding Balance"))
                    if isinstance(row, Mapping) else None for row in history]
        if any(balance is not None and balance > 0 for balance in balances):
            loans["outstanding_status"] = "positive"
        elif all(balance == 0 for balance in balances) and loans["complete"] is not False:
            loans["outstanding_status"] = "zero"
        dates = {_normalize_lifecycle_date(row.get("Balance as of Date"))
                 for row in history if isinstance(row, Mapping)}
        if len(dates) == 1 and None not in dates and not loans["as_of"]:
            loans["as_of"] = next(iter(dates))
    # Independent current loan balances corroborate positive exposure. A
    # contradiction with an empty history remains unknown and needs review.
    if loan_balance["status"] == "known" and loan_balance["value"] > 0:
        if loans["outstanding_status"] == "zero":
            loans["outstanding_status"] = "unknown"
            loans["conflict"] = True
        elif loans["status"] != "error":
            loans["outstanding_status"] = "positive"
            if loans["status"] == "unknown":
                loans["source"] = loan_balance["source"]
                loans["as_of"] = loan_balance["as_of"]
    return {
        "schema_version": 1, "loans": loans, "sources": sources,
        "crypto": {
            "enrollment": _typed_source(participant.get("crypto_enrollment"), module="census",
                                        label="Crypto Enrollment", meta=meta, boolean=True),
            "holdings": _unknown_source(),
        },
    }


def build_collected_data(
    ppt_modules: Optional[Mapping[str, Any]],
    plan_modules: Optional[Mapping[str, Any]],
    ticket_extracted: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    company_name: Optional[str] = None,
    company_status: Optional[str] = None,
    company_status_detail: Optional[str] = None,
    participant_meta: Optional[Mapping[str, Any]] = None,
    plan_meta: Optional[Mapping[str, Any]] = None,
    identity_context: Optional[Mapping[str, Any]] = None,
    selected_plan_id: Optional[str] = None,
) -> Dict[str, Any]:
    """collected_data determinístico: {participant_data, plan_data}.

    ``ticket_extracted`` (slug → {field, value, evidence}) sólo rellena
    campos que el scrape no aportó — nunca sobrescribe un hecho scrapeado.
    """
    participant: Dict[str, Any] = {}
    plan: Dict[str, Any] = {}
    internal_plan_context: Optional[Dict[str, Any]] = None
    # Read the plan scrape before any participant-reported or request value can
    # land in ``plan``; the disclosure contract must not see merged values.
    internal_plan_disclosure_context = _build_plan_disclosure_context(
        plan_modules, plan_meta, identity_context, selected_plan_id,
    )

    for module_name, module_data in (ppt_modules or {}).items():
        if module_name in {"plan_notes", "plan_history"}:
            continue
        if not isinstance(module_data, Mapping):
            plan_key = snake_case(module_name)
            plan[plan_key] = module_data       # e.g. plan_notes passthrough
            continue
        if module_name == "census":
            _map_simple(module_data, CENSUS_MAP, participant)
        elif module_name == "savings_rate":
            _map_savings(module_data, participant, plan)
        elif module_name == "payroll":
            _map_payroll(module_data, participant)
        elif module_name == "loans":
            _map_loans(module_data, participant, plan)
        elif module_name == "mfa":
            _map_mfa(module_data, participant)
        elif module_name == "plan_details":
            _map_simple(module_data, PLAN_DETAILS_MAP, plan)
        else:
            _map_simple(module_data, {}, participant,
                        collision_prefix=snake_case(module_name))

    if plan_modules:
        internal_plan_context = _build_internal_plan_context(plan_modules, plan_meta)
        usable_plan_modules = {
            key: value for key, value in plan_modules.items()
            if _source_diagnostic(plan_meta, key, "status").get("data_state") not in _ERROR_DATA_STATES | _UNAVAILABLE_DATA_STATES
        }
        _map_plan_modules(usable_plan_modules, plan)

    # Only scraped data can establish preflight facts. Ticket assertions remain
    # participant-reported values and cannot silently satisfy source checks.
    internal_preflight_context = _build_preflight_context(participant, participant_meta)
    internal_disclosure_context = _build_disclosure_context(participant, internal_preflight_context, participant_meta, identity_context)

    # Conceptos derivados en código (nunca por el LLM):
    derived_first_contribution = derive_first_contribution_posted_status(participant)
    if derived_first_contribution is not None:
        participant["first_contribution_posted_status"] = derived_first_contribution

    for slug, entry in (ticket_extracted or {}).items():
        key = snake_case(slug)
        if key in _CODE_DERIVED_CONCEPTS:
            # un concepto derivado ausente (payroll sin datos) queda UNKNOWN;
            # la extracción de ticket no puede convertirlo en True/False.
            continue
        if key not in participant:
            value = entry.get("value")
            if key == "confirmation_of_review_of_hardship_distribution_guidelines_pdf":
                evidence = entry.get("evidence")
                # A literal quote alone could say "not reviewed" or only that
                # the PDF was sent. Require an affirmative completed review.
                confirmed = value is True and isinstance(evidence, str) and re.fullmatch(
                    r"\s*(?:I\s+(?:have\s+)?|(?:the\s+)?participant\s+(?:confirmed\s+(?:they\s+)?(?:have\s+)?)?)"
                    r"(?:read|reviewed)\s+(?:the\s+)?Hardship Distribution Guidelines(?:\s+PDF)?[.!]?\s*",
                    evidence, re.I,
                ) is not None
                value = True if confirmed else None
            participant[key] = value

    if company_name is not None:
        plan.setdefault("company_name", company_name)
    if company_status is not None:
        plan.setdefault("company_status", company_status)
    if company_status_detail is not None:
        plan.setdefault("company_status_detail", company_status_detail)

    collected: Dict[str, Any] = {"participant_data": participant,
                               "internal_preflight_context": internal_preflight_context}
    if internal_disclosure_context:
        collected["internal_disclosure_context"] = internal_disclosure_context
    if internal_plan_disclosure_context:
        collected["internal_plan_disclosure_context"] = internal_plan_disclosure_context
    if plan:
        collected["plan_data"] = plan
    if internal_plan_context is not None:
        collected["internal_plan_context"] = internal_plan_context
    return collected


def build_context(req: Any) -> Dict[str, Any]:
    """Objeto ``context`` determinístico desde el request (gr_body_build.md §6)."""
    t = req.ticket
    return {
        "ticket_id": t.ticket_id,
        "agent_name": t.username,
        "agent_email": t.user_email,
        "email_subject": t.email_subject,
        "first_contact": t.first_contact,
        "devrev_tag": None,
        "participant_id": req.participant_id,
        "plan_id": req.plan_id,
    }

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
    "deconverted",
})
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _normalize_lifecycle_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
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
    return f"{year:04d}-{month:02d}-{day:02d}"


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


def _facts_from_lifecycle_note(note: Any, event_date: Optional[str]) -> List[str]:
    """Extract a tiny closed fact vocabulary; never return the note itself."""
    if not isinstance(note, str):
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
) -> Optional[Dict[str, Any]]:
    """Reduce plan notes/history to safe lifecycle facts for reasoning.

    Arbitrary note text, authors, and unrecognized changes are intentionally
    discarded. Only closed-vocabulary statuses, booleans, dates and two
    operational signals (deconversion / last payroll date) survive.
    """
    history = plan_modules.get("plan_history")
    notes = plan_modules.get("plan_notes")
    basic = plan_modules.get("basic_info")
    if not isinstance(history, Mapping) and not isinstance(notes, list):
        return None

    raw_extraction_status = (
        history.get("extractionStatus", history.get("extraction_status"))
        if isinstance(history, Mapping) else "legacy_notes"
    )
    extraction_status = (
        raw_extraction_status
        if raw_extraction_status in _INTERNAL_HISTORY_STATUSES
        else "parse_error"
    )
    current: Dict[str, Any] = {}
    history_current = history.get("current") if isinstance(history, Mapping) else None
    if not isinstance(history_current, Mapping):
        history_current = {}
    if not isinstance(basic, Mapping):
        basic = {}

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
    entries = history.get("entries") if isinstance(history, Mapping) else None
    if isinstance(entries, list):
        for entry in entries[:50]:
            if not isinstance(entry, Mapping):
                continue
            event_date = _normalize_lifecycle_date(
                entry.get("occurredAt", entry.get("effectiveOn"))
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
                            when = f" on {event_date}" if event_date else ""
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
                            when = f" on {event_date}" if event_date else ""
                            _append_fact(
                                facts,
                                "Plan active flag changed"
                                f"{detail} to {str(new_active).lower()}{when}.",
                            )
                    elif field_name in {
                        "terminated_status_as_of", "actively_managed_status_as_of"
                    }:
                        effective_date = _normalize_lifecycle_date(change.get("to"))
                        if effective_date:
                            _append_fact(
                                facts,
                                f"Plan lifecycle effective date is {effective_date}.",
                            )
            for fact in _facts_from_lifecycle_note(entry.get("note"), event_date):
                _append_fact(facts, fact)

    # Legacy v1 note arrays remain accepted, but only recognized facts survive.
    if isinstance(notes, list):
        for note in notes[:50]:
            for fact in _facts_from_lifecycle_note(note, None):
                _append_fact(facts, fact)

    return {
        "extraction_status": extraction_status,
        "current": current,
        "lifecycle_facts": facts,
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
        if key == "loan_history" and isinstance(value, str):
            value = []          # "There's no Loan History..." → lista vacía
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


def build_collected_data(
    ppt_modules: Optional[Mapping[str, Any]],
    plan_modules: Optional[Mapping[str, Any]],
    ticket_extracted: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    company_name: Optional[str] = None,
    company_status: Optional[str] = None,
) -> Dict[str, Any]:
    """collected_data determinístico: {participant_data, plan_data}.

    ``ticket_extracted`` (slug → {field, value, evidence}) sólo rellena
    campos que el scrape no aportó — nunca sobrescribe un hecho scrapeado.
    """
    participant: Dict[str, Any] = {}
    plan: Dict[str, Any] = {}
    internal_plan_context: Optional[Dict[str, Any]] = None

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
        internal_plan_context = _build_internal_plan_context(plan_modules)
        _map_plan_modules(plan_modules, plan)

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
            participant[key] = entry.get("value")

    if company_name is not None:
        plan.setdefault("company_name", company_name)
    if company_status is not None:
        plan.setdefault("company_status", company_status)

    collected: Dict[str, Any] = {"participant_data": participant}
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

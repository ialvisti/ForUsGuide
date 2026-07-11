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
}
# Conceptos donde planDataModules es más autoritativo que plan_details:
_PLAN_MODULE_WINS = {"enrollment_type", "plan_status", "minimum_age",
                     "employer_match_type", "employer_match_timing"}


def snake_case(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", str(name).strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


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
        if module_name == "plan_notes":
            plan["plan_notes"] = module_data
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

    for module_name, module_data in (ppt_modules or {}).items():
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
        _map_plan_modules(plan_modules, plan)

    for slug, entry in (ticket_extracted or {}).items():
        key = snake_case(slug)
        if key not in participant:
            participant[key] = entry.get("value")

    if company_name is not None:
        plan.setdefault("company_name", company_name)
    if company_status is not None:
        plan.setdefault("company_status", company_status)

    collected: Dict[str, Any] = {"participant_data": participant}
    if plan:
        collected["plan_data"] = plan
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

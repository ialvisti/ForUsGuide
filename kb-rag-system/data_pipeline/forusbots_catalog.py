"""
ForusBots catalog — verified module/field ground truth + deterministic mapping.

Source of truth, verified against the ForusBots source code
(/Users/ivanalvis/Desktop/ForUsBots, extractor ``SUPPORTED_FIELDS`` arrays) AND
against real payloads confirmed in production. Three jobs:

1. **Deterministic first-pass mapping** (``map_slug``): the KB's required-data
   prompt already constrains the LLM to canonical snake_case slugs; those map to
   exact ForusBots (module, field) pairs by table, skipping the LLM mapper
   entirely for known slugs.
2. **Validation gate** (``validate_modules``): everything that goes to ForusBots
   — deterministic or LLM-produced — passes through here. Unknown module keys
   are rejected (a bad module can break a job); unknown FIELDS inside a valid
   module are warn-and-pass, because the deployed service is known to return
   fields the local repo doesn't list (drift), and non-strict mode ignores
   non-existent fields harmlessly.
3. **Result normalization** (``normalize_scrape_result``): the scrape result
   shape varies by source (confirmed real payload = flat module-keyed ``data``;
   local-repo normalizer = ``data.modules[]`` envelope). Sniff and flatten all
   of them to the ``{module: {field: value}}`` shape the gr_body_build agent
   documents.

Request vs response vocabulary: fields are REQUESTED using the extractor
``SUPPORTED_FIELDS`` names below. Responses mostly echo those names, EXCEPT
``basic_info`` which responds with snake_case API keys (company_name, ein, ...)
per its FIELD_MAP — the gr_body_build prompt documents the response side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

# ============================================================================
# Catalogs (request vocabulary)
# ============================================================================

# Participant scrape modules. Verified from
# src/extractors/forusall-participant/modules/*.js SUPPORTED_FIELDS.
# Fields marked "observed" were absent from the local repo's lists but present
# in a real production payload (service-vs-repo drift) — kept so we never
# reject them.
PARTICIPANT_MODULES: Dict[str, Tuple[str, ...]] = {
    "census": (
        "Partial SSN", "First Name", "Last Name", "Eligibility Status",
        "Crypto Enrollment", "Birth Date", "Hire Date", "Rehire Date",
        "Termination Date", "Projected Plan Entry Date", "Address 1",
        "Address 2", "City", "State", "Zip Code", "Primary Email",
        "Home Email", "Phone",
    ),
    "savings_rate": (
        "Current Pre-tax Percent", "Current Pre-tax Amount",
        "Current Roth Percent", "Current Roth Amount", "Record Keeper Site",
        "Employer Match Type", "Record Keeper", "Plan enrollment type",
        "Account Balance", "Account Balance As Of", "Employee Deferral Balance",
        "Roth Deferral Balance", "Rollover Balance", "Employer Match Balance",
        "Employer Match Vested Balance", "Loan Balance",
        "YTD Employee contributions", "YTD Employer contributions",
        "Maxed out", "Auto escalation rate", "Auto escalation rate limit",
        "Auto escalation timing",
        "Formula", "Timing",  # observed in real payload
    ),
    "plan_details": (
        "Plan Documents", "Plan Type", "Status", "Participant Site",
        "Plan enrollment type", "Auto Enrollment Rate", "Minimum Age",
        "Service Months", "Service hours", "Plan Entry Frequency",
        "Profit Sharing", "Force-out Limit", "Maximum Number of Loans",
        "Employer Contribution Type", "Formula", "Employer Match Timing",  # observed
    ),
    "loans": (
        "Participant Site", "Maximum Number of Loans", "Account Balance",
        "Account Balance As Of", "Loan History",
    ),
    "payroll": (
        "Payroll Frequency", "Next Schedule paycheck", "Available Years",
        "Latest Payroll",
        # plus dynamic "Payroll YYYY" and "years:..." tokens — see regexes
    ),
    "mfa": ("MFA Status",),
}

# Plan scrape modules. Verified from src/extractors/forusall-plan/modules/*.js.
PLAN_MODULES: Dict[str, Tuple[str, ...]] = {
    "basic_info": (
        # REQUEST uses these labels; the RESPONSE uses snake_case API keys
        # (company_name, official_plan_name, rm_id, ...) per basic_info FIELD_MAP.
        "plan_id", "version_id", "Short Name", "Sfdc id", "Company Name",
        "Legal Plan Name", "Relationship Manager", "Implementation Manager",
        "Service Type", "Plan Type", "Active", "Status", "Status as of",
        "3(16) ONLY", "EIN", "Effective Date",
    ),
    "plan_design": (
        "record_keeper_id", "rk_plan_id", "external_name", "lt_plan_type",
        "accept_covid19_amendment", "fund_lineup_id", "enrollment_type",
        "eligibility_min_age", "eligibility_duration_value",
        "eligibility_duration_unit", "eligibility_hours_requirement",
        "plan_entry_frequency", "plan_entry_frequency_first_month",
        "plan_entry_frequency_second_month", "employer_contribution",
        "er_contribution_monthly_cap", "employer_contribution_cap",
        "employer_contribution_timing", "employer_contribution_options_qaca",
        "default_savings_rate", "contribution_type", "autoescalate_rate",
        "support_aftertax", "alts_crypto", "alts_waitlist_crypto",
        "max_crypto_percent_balance",
    ),
    "onboarding": (
        "first_deferral_date", "special_participation_date", "enrollment_method",
        "blackout_begins_date", "blackout_ends_date", "website_live_date",
    ),
    "communications": (
        "dave_text", "logo", "spanish_participants", "e_statement",
        "raffle_prize", "raffle_date",
    ),
    "extra_settings": (
        "rk_upload_mode", "plan_year_start", "er_contribution_eligibility",
        "er_match_eligibility_age", "er_match_eligibility_duration_value",
        "er_match_eligibility_duration_unit",
        "er_match_eligibility_hours_requirement",
        "er_match_plan_entry_frequency",
        "er_match_plan_entry_frequency_first_month",
        "er_match_plan_entry_frequency_second_month",
    ),
    "feature_flags": (
        "payroll_xray", "payroll_issue", "simple_upload",
        "crypto_portfolio_alert_blacklist",  # observed in real payload
    ),
}

# "communications" exists in BOTH worlds: forbidden as a participant module
# (no structured extractor → panel_not_found) but a valid PLAN module. The
# validator routes it to plan when its fields match the plan catalog.
FORBIDDEN_PARTICIPANT_MODULES = frozenset({"communications", "documents"})

PARTICIPANT_MODULE_ORDER = ("census", "savings_rate", "plan_details", "loans", "payroll", "mfa")
PLAN_MODULE_ORDER = ("basic_info", "plan_design", "onboarding", "communications",
                     "extra_settings", "feature_flags")
_MODULE_ORDER_INDEX = {k: i for i, k in enumerate(PARTICIPANT_MODULE_ORDER + PLAN_MODULE_ORDER)}

# Mirrors ForusBots payroll.js FIELD_POLICY exactly.
_PAYROLL_TOKEN_RE = re.compile(r"^years:\s*(all|\d{4}(?:\s*,\s*\d{4})*)\s*$", re.I)
_PAYROLL_DYNAMIC_RE = re.compile(r"^payroll\s+(\d{4})$", re.I)

# Case-insensitive field lookup per module (request vocabulary).
_CANON: Dict[str, Dict[str, str]] = {
    key: {f.lower(): f for f in fields}
    for key, fields in {**PARTICIPANT_MODULES, **PLAN_MODULES}.items()
}

_NON_MODULE_DATA_KEYS = frozenset({
    "participantId", "planId", "url", "modulesRequested", "full", "notes",
})


# ============================================================================
# Deterministic slug map (first pass)
# ============================================================================

# slug -> ordered (module, field) pairs. The KB's required-data prompt already
# constrains its LLM to these canonical slugs; anything not here goes to the
# LLM mapper (whose output is still validated). The special payroll sentinel
# "years:CURRENT_YEAR" is resolved at map time.
SLUG_MAP: Dict[str, Tuple[Tuple[str, str], ...]] = {
    # --- census ---
    "first_name": (("census", "First Name"),),
    "last_name": (("census", "Last Name"),),
    "participant_name": (("census", "First Name"), ("census", "Last Name")),
    "full_name": (("census", "First Name"), ("census", "Last Name")),
    "participant_s_name": (("census", "First Name"), ("census", "Last Name")),
    "participants_name": (("census", "First Name"), ("census", "Last Name")),
    "name": (("census", "First Name"), ("census", "Last Name")),
    "participant_status": (("census", "Eligibility Status"),),
    "employment_status": (("census", "Eligibility Status"),),
    "eligibility_status": (("census", "Eligibility Status"),),
    "birth_date": (("census", "Birth Date"),),
    "dob": (("census", "Birth Date"),),
    "hire_date": (("census", "Hire Date"),),
    "rehire_date": (("census", "Rehire Date"),),
    "termination_date": (("census", "Termination Date"),),
    "employment_separation_date": (("census", "Termination Date"),),
    "separation_date": (("census", "Termination Date"),),
    "primary_email": (("census", "Primary Email"),),
    "email": (("census", "Primary Email"),),
    "participant_email": (("census", "Primary Email"),),
    "participants_email": (("census", "Primary Email"),),
    "email_address": (("census", "Primary Email"),),
    "home_email": (("census", "Home Email"),),
    "phone": (("census", "Phone"),),
    # Age questions resolve from Birth Date (the predicate itself — e.g.
    # "is under 59½" — is evaluated downstream by the GR from the date).
    "participant_age": (("census", "Birth Date"),),
    "age": (("census", "Birth Date"),),
    "participant_age_relative_to_595": (("census", "Birth Date"),),
    "participant_age_for_penalty_context": (("census", "Birth Date"),),
    "address": (("census", "Address 1"), ("census", "Address 2"),
                ("census", "City"), ("census", "State"), ("census", "Zip Code")),
    "full_address": (("census", "Address 1"), ("census", "Address 2"),
                     ("census", "City"), ("census", "State"), ("census", "Zip Code")),
    "mailing_address": (("census", "Address 1"), ("census", "Address 2"),
                        ("census", "City"), ("census", "State"), ("census", "Zip Code")),
    "mailing_address_on_file": (("census", "Address 1"), ("census", "Address 2"),
                                ("census", "City"), ("census", "State"), ("census", "Zip Code")),
    "partial_ssn": (("census", "Partial SSN"),),
    "ssn_last4": (("census", "Partial SSN"),),
    # Documented Rule-10 exact predicate aliases (open predicates stay LLM):
    "employment_status_has_ended": (("census", "Termination Date"),
                                    ("census", "Eligibility Status")),
    "employment_has_ended": (("census", "Termination Date"),
                             ("census", "Eligibility Status")),
    # --- savings_rate ---
    "account_balance": (("savings_rate", "Account Balance"),),
    "total_balance": (("savings_rate", "Account Balance"),),
    # Account Balance IS the participant's total vested balance (no separate
    # "Vested Balance" field exists). Disambiguation hook may remap to
    # Employer Match Vested Balance — see map_slug.
    "vested_balance": (("savings_rate", "Account Balance"),),
    "total_vested_balance": (("savings_rate", "Account Balance"),),
    "account_total_vested_balance": (("savings_rate", "Account Balance"),),
    "employer_match_vested_balance": (("savings_rate", "Employer Match Vested Balance"),),
    "roth_deferral_balance": (("savings_rate", "Roth Deferral Balance"),),
    "roth_balance": (("savings_rate", "Roth Deferral Balance"),),
    "rollover_balance": (("savings_rate", "Rollover Balance"),),
    "employer_match_balance": (("savings_rate", "Employer Match Balance"),),
    "record_keeper": (("savings_rate", "Record Keeper"),),
    "plan_enrollment_type": (("savings_rate", "Plan enrollment type"),),
    "enrollment_type": (("savings_rate", "Plan enrollment type"),),
    "ytd_employee_contributions": (("savings_rate", "YTD Employee contributions"),),
    "ytd_employer_contributions": (("savings_rate", "YTD Employer contributions"),),
    "employer_match_type": (("savings_rate", "Employer Match Type"),),
    # --- plan_details (plan-level data, FREE on the participant scrape) ---
    "plan_type": (("plan_details", "Plan Type"),),
    "plan_status": (("plan_details", "Status"),),
    "plan_termination_status": (("plan_details", "Status"),),
    "force_out_limit": (("plan_details", "Force-out Limit"),),
    "maximum_number_of_loans": (("plan_details", "Maximum Number of Loans"),),
    "max_loans": (("plan_details", "Maximum Number of Loans"),),
    "auto_enrollment_rate": (("plan_details", "Auto Enrollment Rate"),),
    "minimum_age": (("plan_details", "Minimum Age"),),
    "eligibility_minimum_age": (("plan_details", "Minimum Age"),),
    "service_months": (("plan_details", "Service Months"),),
    "service_hours": (("plan_details", "Service hours"),),
    "plan_entry_frequency": (("plan_details", "Plan Entry Frequency"),),
    "profit_sharing": (("plan_details", "Profit Sharing"),),
    "employer_match_formula": (("plan_details", "Formula"),
                               ("plan_details", "Employer Contribution Type"),
                               ("plan_details", "Employer Match Timing")),
    # --- loans ---
    "loan_history": (("loans", "Loan History"),),
    "loans": (("loans", "Loan History"),),
    "active_loans": (("loans", "Loan History"),),
    "outstanding_loan_balance": (("loans", "Loan History"),),
    "loan_history_status": (("loans", "Loan History"),),
    "loan_status": (("loans", "Loan History"),),
    "loan_request_status": (("loans", "Loan History"),),
    "outstanding_401k_loan_status": (("loans", "Loan History"),),
    # 12-month-highest / scheduled-payment questions resolve from the Loan
    # History rows (Principal / Outstanding Balance / Repayment Amount).
    "prior_12_month_highest_outstanding_loan_balance_availability": (("loans", "Loan History"),),
    "previous_highest_outstanding_loan_balance_during_the_last_12_months": (("loans", "Loan History"),),
    "highest_outstanding_loan_balance": (("loans", "Loan History"),),
    "original_scheduled_payroll_payment_amount": (("loans", "Loan History"),),
    "loan_account_balance": (("loans", "Account Balance"),),
    "loan_balance": (("loans", "Account Balance"),),
    # --- payroll ---
    "payroll_frequency": (("payroll", "Payroll Frequency"),),
    "last_payroll_date": (("payroll", "Latest Payroll"),),
    "last_paycheck": (("payroll", "Latest Payroll"),),
    "last_paycheck_date": (("payroll", "Latest Payroll"),),
    "latest_payroll": (("payroll", "Latest Payroll"),),
    "last_payroll_record": (("payroll", "Latest Payroll"),),
    "most_recent_payroll": (("payroll", "Latest Payroll"),),
    "payroll_history": (("payroll", "years:all"),),
    "historical_payroll": (("payroll", "years:all"),),
    "all_payroll": (("payroll", "years:all"),),
    "payroll": (("payroll", "years:CURRENT_YEAR"),),
    "payroll_data": (("payroll", "years:CURRENT_YEAR"),),
    # --- mfa ---
    "mfa_status": (("mfa", "MFA Status"),),
    # --- plan scrape (config the participant scrape does NOT expose) ---
    "default_savings_rate": (("plan_design", "default_savings_rate"),),
    "contribution_type": (("plan_design", "contribution_type"),),
    "eligibility_duration": (("plan_design", "eligibility_duration_value"),
                             ("plan_design", "eligibility_duration_unit")),
    "eligibility_hours_requirement": (("plan_design", "eligibility_hours_requirement"),),
    "crypto_support": (("plan_design", "alts_crypto"),),
    "crypto_enabled": (("plan_design", "alts_crypto"),),
    "max_crypto_percent": (("plan_design", "max_crypto_percent_balance"),),
    "max_crypto_percent_balance": (("plan_design", "max_crypto_percent_balance"),),
    "first_deferral_date": (("onboarding", "first_deferral_date"),),
    "enrollment_method": (("onboarding", "enrollment_method"),),
    "blackout_begins_date": (("onboarding", "blackout_begins_date"),),
    "blackout_ends_date": (("onboarding", "blackout_ends_date"),),
    "blackout_dates": (("onboarding", "blackout_begins_date"),
                       ("onboarding", "blackout_ends_date")),
    "website_live_date": (("onboarding", "website_live_date"),),
    "plan_year_start": (("extra_settings", "plan_year_start"),),
    "er_match_eligibility_age": (("extra_settings", "er_match_eligibility_age"),),
    "er_contribution_eligibility": (("extra_settings", "er_contribution_eligibility"),),
    "employer_match_eligibility": (
        ("extra_settings", "er_match_eligibility_age"),
        ("extra_settings", "er_match_eligibility_duration_value"),
        ("extra_settings", "er_match_eligibility_duration_unit"),
        ("extra_settings", "er_match_eligibility_hours_requirement"),
    ),
    "employer_match_eligibility_for_hardship_withdrawal": (
        ("extra_settings", "er_match_eligibility_age"),
        ("extra_settings", "er_match_eligibility_duration_value"),
        ("extra_settings", "er_match_eligibility_duration_unit"),
        ("extra_settings", "er_match_eligibility_hours_requirement"),
    ),
    "ein": (("basic_info", "EIN"),),
    "plan_effective_date": (("basic_info", "Effective Date"),),
    "effective_date": (("basic_info", "Effective Date"),),
    "legal_plan_name": (("basic_info", "Legal Plan Name"),),
    "official_plan_name": (("basic_info", "Legal Plan Name"),),
}

# When the required-data item is categorized plan_data, these slugs prefer the
# PLAN-side target over the participant-side default (only slugs whose meaning
# genuinely differs per side).
SLUG_MAP_PLAN_OVERRIDE: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "auto_escalation_rate": (("plan_design", "autoescalate_rate"),),
    "enrollment_type": (("plan_design", "enrollment_type"),),
    "plan_enrollment_type": (("plan_design", "enrollment_type"),),
}
# Participant-side default for auto_escalation_rate (not in the canonical 32
# but a natural emission):
SLUG_MAP.setdefault("auto_escalation_rate", (("savings_rate", "Auto escalation rate"),))

_VESTED_GROUP = frozenset({"vested_balance", "total_vested_balance", "account_total_vested_balance"})
_PAYROLL_GENERAL_GROUP = frozenset({"payroll", "payroll_data"})
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Fields the ticket-handler request itself already carries (caseData) — they
# need NEITHER a scrape NOR ticket extraction, and must never land in unmapped.
REQUEST_PROVIDED_SLUGS = frozenset({
    "plan_id", "participant_id", "participant_plan",
    "company_name", "company_status", "company_status_detail",
})


def is_request_provided(item: Mapping[str, Any]) -> bool:
    """True when the field is already provided by the handle-ticket request."""
    return _normalize_slug(item.get("field")) in REQUEST_PROVIDED_SLUGS


# ============================================================================
# Mapping (deterministic first pass)
# ============================================================================

def _normalize_slug(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    return re.sub(r"_{2,}", "_", s).strip("_")


def map_slug(item: Mapping[str, Any], *, current_year: int) -> Optional[List[Tuple[str, str]]]:
    """Deterministic first pass for one required-field item.

    Returns the (module, field) pairs, or ``None`` when the slug is not in the
    table (→ caller sends it to the LLM mapper)."""
    slug = _normalize_slug(item.get("field"))
    if not slug:
        return None

    text = " ".join(
        str(item.get(k) or "") for k in ("field", "description", "why_needed")
    ).lower()

    # Hook: vested_balance group → Employer Match Vested Balance when the text
    # is specifically about the employer-match vested portion.
    if slug in _VESTED_GROUP:
        if "employer match" in text or "employer's match" in text or (
            "match" in text and "vested" in text
        ):
            return [("savings_rate", "Employer Match Vested Balance")]
        return [("savings_rate", "Account Balance")]

    # Hook: general payroll → historical / explicit years / current year.
    if slug in _PAYROLL_GENERAL_GROUP:
        if "historical" in text or "all years" in text or "entire history" in text:
            return [("payroll", "years:all")]
        years = sorted(set(_YEAR_RE.findall(text)))
        if years:
            return [("payroll", f"years:{','.join(years)}")]
        return [("payroll", f"years:{current_year}")]

    # Hook: plan_data category prefers the plan-side target when one exists.
    if str(item.get("category") or "") == "plan_data" and slug in SLUG_MAP_PLAN_OVERRIDE:
        return list(SLUG_MAP_PLAN_OVERRIDE[slug])

    entries = SLUG_MAP.get(slug)
    if entries is None:
        return None

    # Resolve the payroll current-year sentinel.
    resolved: List[Tuple[str, str]] = []
    for module, fld in entries:
        if fld == "years:CURRENT_YEAR":
            fld = f"years:{current_year}"
        resolved.append((module, fld))
    return resolved


def build_modules(entries: Iterable[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Group (module, field) pairs into [{"key", "fields"}], deduped, ordered."""
    grouped: Dict[str, List[str]] = {}
    for module, fld in entries:
        bucket = grouped.setdefault(module, [])
        if fld not in bucket:
            bucket.append(fld)
    keys = sorted(grouped, key=lambda k: _MODULE_ORDER_INDEX.get(k, 99))
    return [{"key": k, "fields": grouped[k]} for k in keys]


def merge_module_lists(*lists: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Union of module lists (e.g. deterministic + LLM), field-level dedupe."""
    entries: List[Tuple[str, str]] = []
    for lst in lists:
        for mod in (lst or []):
            if not isinstance(mod, dict):
                continue
            key = str(mod.get("key") or "").strip()
            for fld in (mod.get("fields") or []):
                if key and fld is not None:
                    entries.append((key, str(fld)))
    return build_modules(entries)


# ============================================================================
# Validation gate
# ============================================================================

@dataclass
class ValidationResult:
    modules: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)


def _resolve_module_key(raw_key: str, fields: List[str]) -> Optional[str]:
    """Resolve a module key to its catalog, handling the communications clash:
    valid as a PLAN module when its fields look like the plan catalog;
    forbidden as a participant module."""
    key = raw_key.strip().lower()
    if key == "communications":
        plan_canon = _CANON.get("communications", {})
        if fields and all(str(f).lower() in plan_canon for f in fields):
            return "communications"   # plan-side
        return None                    # participant-side → forbidden
    if key in PARTICIPANT_MODULES or key in PLAN_MODULES:
        return key
    return None


def _validate_payroll_field(fld: str) -> Optional[str]:
    """Normalize a payroll field/token; None when invalid."""
    canon = _CANON["payroll"].get(fld.lower())
    if canon:
        return canon
    m = _PAYROLL_TOKEN_RE.match(fld)
    if m:
        spec = m.group(1).lower()
        if spec == "all":
            return "years:all"
        years = [y.strip() for y in spec.split(",") if y.strip()]
        return "years:" + ",".join(years)
    m = _PAYROLL_DYNAMIC_RE.match(fld)
    if m:
        return f"Payroll {m.group(1)}"
    return None


def validate_modules(modules: Any) -> ValidationResult:
    """Catalog gate — ALWAYS applied before anything is sent to ForusBots.

    Hard rejects: unknown/forbidden module keys, "SSN", malformed entries,
    invalid payroll tokens. Case fix-up to canonical names. Unknown fields in a
    VALID module are warn-and-pass (service drift; non-strict ignores them)."""
    result = ValidationResult()
    if not isinstance(modules, list):
        if modules is not None:
            result.rejected.append({"module": str(modules)[:80], "field": "",
                                    "reason": "malformed_modules_payload"})
        return result

    entries: List[Tuple[str, str]] = []
    for mod in modules:
        if not isinstance(mod, dict):
            result.rejected.append({"module": str(mod)[:80], "field": "",
                                    "reason": "malformed_entry"})
            continue
        raw_key = str(mod.get("key") or "").strip()
        raw_fields = [str(f) for f in (mod.get("fields") or []) if f is not None]
        key = _resolve_module_key(raw_key, raw_fields)
        if key is None:
            reason = ("no_structured_extractor"
                      if raw_key.lower() in FORBIDDEN_PARTICIPANT_MODULES
                      else "unknown_module")
            result.rejected.append({"module": raw_key, "field": "", "reason": reason})
            continue

        canon_map = _CANON[key]
        for fld in raw_fields:
            stripped = fld.strip()
            if stripped.upper() == "SSN":
                result.rejected.append({"module": key, "field": stripped,
                                        "reason": "full_ssn_not_permitted"})
                continue
            if key == "payroll":
                normalized = _validate_payroll_field(stripped)
                if normalized is None:
                    result.rejected.append({"module": key, "field": stripped,
                                            "reason": "invalid_payroll_token"})
                else:
                    entries.append((key, normalized))
                continue
            canon = canon_map.get(stripped.lower())
            if canon:
                entries.append((key, canon))
            else:
                # Warn-and-pass: deployed service has fields the local repo
                # doesn't list; non-strict mode ignores truly unknown ones.
                result.warnings.append({"module": key, "field": stripped,
                                        "reason": "unverified_field"})
                entries.append((key, stripped))

    result.modules = build_modules(entries)
    return result


def split_modules_by_target(
    modules: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a validated module list into (participant_modules, plan_modules).

    Keys are disjoint across the two scrapes except "communications", which
    only survives validation as a plan module."""
    participant: List[Dict[str, Any]] = []
    plan: List[Dict[str, Any]] = []
    for mod in modules:
        key = mod.get("key")
        if key in PARTICIPANT_MODULES and key not in FORBIDDEN_PARTICIPANT_MODULES:
            participant.append(mod)
        elif key in PLAN_MODULES:
            plan.append(mod)
    return participant, plan


# ============================================================================
# Result normalization
# ============================================================================

_KNOWN_MODULE_KEYS = frozenset(PARTICIPANT_MODULES) | frozenset(PLAN_MODULES)


def normalize_scrape_result(result: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Flatten any known scrape-result shape to ``({module: data}, meta)``.

    Shapes handled, in sniffing order:
    1. list wrapper → take the first element
    2. ``{state?, data: {<module>: {...}, notes?, ...}}`` — flat module-keyed
       ``data`` (PRIMARY, confirmed real payload); ``notes`` → ``plan_notes``
    3. ``{ok?, data: {modules: [{key, status, data, ...}]}}`` — local-repo
       normalizer envelope
    4. already-flat ``{census: {...}}`` dict
    5. anything else → ``({}, {"shape": "empty"})``
    """
    meta: Dict[str, Any] = {}

    if isinstance(result, list):
        result = result[0] if result else None

    if not isinstance(result, dict):
        return {}, {"shape": "empty"}

    data = result.get("data")

    # Shape 3: envelope with data.modules list
    if isinstance(data, dict) and isinstance(data.get("modules"), list):
        flat: Dict[str, Any] = {}
        module_status: Dict[str, str] = {}
        module_errors: Dict[str, str] = {}
        unknown_fields: Dict[str, Any] = {}
        extractor_warnings: Dict[str, Any] = {}
        for entry in data["modules"]:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not key:
                continue
            status = entry.get("status") or "ok"
            module_status[key] = status
            if status == "ok":
                flat[key] = entry.get("data") or {}
            else:
                module_errors[key] = str(entry.get("error") or status)
            if entry.get("unknownFields"):
                unknown_fields[key] = entry["unknownFields"]
            if entry.get("extractorWarnings"):
                extractor_warnings[key] = entry["extractorWarnings"]
        if isinstance(data.get("notes"), list) and data["notes"]:
            flat["plan_notes"] = data["notes"]
        meta["shape"] = "envelope"
        if module_status:
            meta["module_status"] = module_status
        if module_errors:
            meta["module_errors"] = module_errors
        if unknown_fields:
            meta["unknown_fields"] = unknown_fields
        if extractor_warnings:
            meta["extractor_warnings"] = extractor_warnings
        if result.get("warnings"):
            meta["warnings"] = result["warnings"]
        if result.get("errors"):
            meta["errors"] = result["errors"]
        return flat, meta

    # Shape 2: flat module-keyed data (confirmed real payload)
    if isinstance(data, dict):
        modules_found = {
            k: v for k, v in data.items()
            if k in _KNOWN_MODULE_KEYS and isinstance(v, dict)
        }
        if modules_found:
            flat = dict(modules_found)
            if isinstance(data.get("notes"), list) and data["notes"]:
                flat["plan_notes"] = data["notes"]
            meta["shape"] = "flat_data"
            if result.get("warnings"):
                meta["warnings"] = result["warnings"]
            if result.get("errors"):
                meta["errors"] = result["errors"]
            return flat, meta

    # Shape 4: already-flat module-keyed dict
    modules_found = {
        k: v for k, v in result.items()
        if k in _KNOWN_MODULE_KEYS and isinstance(v, dict)
    }
    if modules_found:
        flat = dict(modules_found)
        if isinstance(result.get("notes"), list) and result["notes"]:
            flat["plan_notes"] = result["notes"]
        return flat, {"shape": "flat"}

    return {}, {"shape": "empty"}

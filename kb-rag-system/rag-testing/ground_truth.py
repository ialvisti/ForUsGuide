"""
Ground truth factual assertions for stress test validation.

Each test ID maps to must_contain (regex patterns that MUST appear in
the response text) and must_not_contain (patterns that indicate
hallucination if matched).

must_contain misses produce soft-fail warnings.
must_not_contain matches produce hard fails (hallucination detected).
"""

import re

GROUND_TRUTH: dict[str, dict[str, list[str]]] = {
    # Force-out thresholds: $80 fee-out, $1,000 safe harbor boundary, $7,000 cap
    "KQ-01": {
        "must_contain": [
            r"\$80\b.*(?:fee.?out|forfeited|absorbed)",
            r"\$1,?000\b.*(?:safe\s*harbor|cash|IRA)",
            r"\$7,?000\b",
            r"20%.*(?:federal|withholding|tax)",
        ],
        "must_not_contain": [],
    },
    # EACA: 90 calendar days from first auto-deferral deposit
    "KQ-02": {
        "must_contain": [
            r"90\s*(?:calendar\s*)?day",
            r"(?:first|initial)\s*(?:auto[- ]?deferral|automatic.*deferral|payroll\s*deferral)",
        ],
        "must_not_contain": [],
    },
    # Hardship: funeral covers parent/spouse/child/dependent — NOT sibling
    "KQ-06": {
        "must_contain": [
            r"(?:parent|spouse|child|dependent)",
        ],
        "must_not_contain": [
            r"(?:sibling|brother|sister)\s+(?:is|are)\s+(?:eligible|a qualifying|an eligible|permitted|allowed)",
        ],
    },
    # LT Trust fee structure: $75 distribution fee, $35 wire, $35 overnight
    "KQ-07": {
        "must_contain": [
            r"\$75\b.*(?:distribution|processing)\s*fee",
            r"\$35\b.*(?:wire|overnight)",
        ],
        "must_not_contain": [
            r"\$50\b.*(?:wire|overnight)",
        ],
    },
    # Contingent amount = 2x outstanding loan balance
    "KQ-08": {
        "must_contain": [
            r"(?:2|two|twice|double).*(?:outstanding|loan\s*balance|contingent)",
        ],
        "must_not_contain": [],
    },
    # RMD ages: 73 (before 1960), 75 (1960+)
    "KQ-09": {
        "must_contain": [
            r"73\b",
            r"75\b",
            r"(?:5|five)\s*%.*(?:owner|ownership)",
        ],
        "must_not_contain": [],
    },
    # Force-out with 20% withholding, $900 balance
    "GR-02": {
        "must_contain": [
            r"20%.*(?:federal|withholding)",
            r"\$80\b.*(?:under|less|fee.?out|below)",
        ],
        "must_not_contain": [],
    },
    # Fee-out: $60 balance absorbed by fees
    "GR-03": {
        "must_contain": [
            r"(?:fee.?out|absorbed|forfeited|offset|zero)",
        ],
        "must_not_contain": [],
    },
    # Safe harbor IRA rollover tier: $1,000-$7,000
    "GR-04": {
        "must_contain": [
            r"(?:safe\s*harbor|IRA\s*rollover|automatic.*IRA)",
        ],
        "must_not_contain": [],
    },
    # GR-09: Contingent amount blocks hardship
    "GR-09": {
        "must_contain": [
            r"(?:contingent|2.*(?:times|x)|twice|double).*(?:loan|outstanding)",
        ],
        "must_not_contain": [],
    },
    # GR-11: 5%+ owner cannot defer RMD
    "GR-11": {
        "must_contain": [
            r"(?:5|five)\s*%.*(?:owner|ownership)",
            r"(?:cannot|can(?:'| no)t|not\s*(?:eligible|allowed|able)).*(?:defer|delay|postpone)",
        ],
        "must_not_contain": [],
    },
    # GR-13: EACA day 89 — within the 90-day window
    "GR-13": {
        "must_contain": [
            r"90\s*(?:calendar\s*)?day",
            r"(?:within|before|meets?).*(?:deadline|window|eligible)",
        ],
        "must_not_contain": [],
    },
    # GR-14: EACA day 91 — past deadline
    "GR-14": {
        "must_contain": [
            r"90\s*(?:calendar\s*)?day",
            r"(?:past|exceed|after|beyond|miss).*(?:deadline|window)",
        ],
        "must_not_contain": [],
    },
    # GR-16: LT Trust fees — $75 distribution, $35 wire, $35 overnight (NOT $50)
    "GR-16": {
        "must_contain": [
            r"\$75\b.*(?:distribution|processing)\s*fee",
            r"\$35\b.*(?:wire|overnight)",
        ],
        "must_not_contain": [
            r"\$50\b.*(?:overnight|wire)",
        ],
    },
    # GR-22: Funeral hardship — parent qualifies
    "GR-22": {
        "must_contain": [
            r"(?:funeral|burial|memorial).*(?:qualif|eligible|permitted|allowed)",
        ],
        "must_not_contain": [],
    },
    # GR-25: Missed RMD penalty — 25%, reducible to 10%
    "GR-25": {
        "must_contain": [
            r"25\s*%",
            r"10\s*%.*(?:reduc|correct|timely)",
        ],
        "must_not_contain": [
            r"50\s*%.*(?:penalty|excise)",
        ],
    },
    # GR-28: Timing — must mention 7 business day wait, must NOT say proceed now
    "GR-28": {
        "must_contain": [
            r"(?:7|seven)\s*(?:business\s*)?day",
        ],
        "must_not_contain": [
            r"you\s*can\s*(?:request|proceed|submit|initiate).*now",
        ],
    },
    # GR-31: Active participant cannot use separation path yet, but alternatives should be covered
    "GR-31": {
        "must_contain": [
            r"Active",
            r"termination\s+date",
            r"hardship",
            r"\bloan\b",
            r"(?:eviction|foreclosure)",
            r"(?:plan\s+(?:allows|permits)|Support\s+(?:must|can|should)\s+confirm)",
        ],
        "must_not_contain": [
            r"can\s+(?:submit|request|proceed|initiate).*separation\s+(?:distribution|withdrawal).*now",
            r"rented\s+house\s+(?:sale|being\s+sold).*(?:automatically|definitely).*(?:qualif|eligible|allowed)",
            r"loan\s+is\s+guaranteed",
        ],
    },
}


def validate_facts(test_id: str, response_text: str) -> tuple[list[str], list[str]]:
    """
    Check response_text against ground truth for the given test_id.

    Returns (warnings, failures):
      - warnings: must_contain patterns that were NOT found (soft fail)
      - failures: must_not_contain patterns that WERE found (hard fail — hallucination)
    """
    truth = GROUND_TRUTH.get(test_id)
    if not truth:
        return [], []

    warnings: list[str] = []
    failures: list[str] = []

    def is_negated(match: re.Match) -> bool:
        sentence_prefix = re.split(r"[.!?\n]", response_text[:match.start()])[-1].lower()
        negation_markers = (
            "do not",
            "does not",
            "did not",
            "not ",
            "never ",
            "cannot",
            "can't",
        )
        return any(marker in sentence_prefix for marker in negation_markers)

    for pattern in truth.get("must_contain", []):
        if not re.search(pattern, response_text, re.IGNORECASE):
            warnings.append(f"Ground truth miss: pattern {pattern!r} not found in response")

    for pattern in truth.get("must_not_contain", []):
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match and not is_negated(match):
            failures.append(
                f"Hallucination detected: pattern {pattern!r} matched '{match.group()}'"
            )

    return warnings, failures

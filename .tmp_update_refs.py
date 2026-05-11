#!/usr/bin/env python3
"""Update internal_articles in 14 KB JSON files with new cross-reference plan.
Preserves original indent (2 or 4), no trailing newline, no escaped unicode.
"""
import json, glob

# article_id -> new internal_articles list (canonical titles or article_ids as currently used)
PLAN = {
    "401k_force_out_process_involuntary_distribution_balance_thresholds_safe_harbor_ira_rollovers_fee_outs_compliance": [
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "How Do I Cancel or Change a Pending Distribution Request?",
        "Missed 60-Day Indirect Rollover Deadline: Tax Consequences, IRS Exceptions, and When to Refer to a Tax Professional",
        "401(k) Force-Out Process: Sponsor FAQ \u2014 Eligibility, Thresholds, Notice Requirements, and Compliance",
        "401(k) Required Minimum Distributions (RMDs): Rules, Deadlines, Penalties, Exceptions, and Roth Conversion Impact",
    ],
    "401k_force_out_process_sponsor_faq": [
        "401(k) Force-Out Process (Involuntary Distribution): Balance Thresholds, Safe Harbor IRA Rollovers, Fee-Outs, and Compliance",
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "How Do I Cancel or Change a Pending Distribution Request?",
        "401(k) Required Minimum Distributions (RMDs): Rules, Deadlines, Penalties, Exceptions, and Roth Conversion Impact",
        "Missed 60-Day Indirect Rollover Deadline: Tax Consequences, IRS Exceptions, and When to Refer to a Tax Professional",
    ],
    "401k_loan_basics_and_support_guide": [
        "LT: 401(k) Loan Complete Guide \u2014 Submission, Repayment & Support",
        "Vanguard: How to Submit a Loan Request",
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
        "LT: Multi-Factor Authentication (MFA) for Your ForUsAll 401(k) Account",
        "How Do I Cancel or Change a Pending Distribution Request?",
    ],
    "401k_savings_after_leaving_your_job_rollovers_cash_outs_roth_pre_tax": [
        "401k_required_minimum_distributions_rmds_rules_deadlines_penalties_exceptions_and_roth_conversion_impact",
        "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover",
        "LT: Can I Split My 401(k) Rollover Between Multiple Providers?",
        "Missed 60-Day Indirect Rollover Deadline: Tax Consequences, IRS Exceptions, and When to Refer to a Tax Professional",
        "401(k) Force-Out Process (Involuntary Distribution): Balance Thresholds, Safe Harbor IRA Rollovers, Fee-Outs, and Compliance",
        "How Do I Cancel or Change a Pending Distribution Request?",
    ],
    "adp_acp_refund_checks_unexpected_checks_taxes_timing_and_prevention": [
        "EACA Refunds After 401(k) Auto-Enrollment: 90-Day Deadline, Eligibility, Fees, and Tax Reporting",
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "How Do I Cancel or Change a Pending Distribution Request?",
    ],
    "can_i_split_my_401_k_rollover_between_multiple_providers": [
        "LT: Multi-Factor Authentication (MFA) for Your ForUsAll 401(k) Account",
        "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover",
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "Missed 60-Day Indirect Rollover Deadline: Tax Consequences, IRS Exceptions, and When to Refer to a Tax Professional",
        "How Do I Cancel or Change a Pending Distribution Request?",
    ],
    "can_i_take_money_from_my_401k_while_employed_your_options_explained": [
        "ForUsAll 401(k) Hardship Withdrawal \u2014 Complete Guide (Rules, Fees, Steps, Timelines)",
        "How Do I Cancel or Change a Pending Distribution Request?",
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "401k_required_minimum_distributions_rmds_rules_deadlines_penalties_exceptions_and_roth_conversion_impact",
        "401(k) Loan Basics and Support Guide",
    ],
    "eaca_refunds_after_401k_auto_enrollment": [
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
        "ADP/ACP Refund Checks: Unexpected Checks, Taxes, Timing, and Prevention",
        "How Do I Cancel or Change a Pending Distribution Request?",
    ],
    "forusall_401k_hardship_withdrawal_complete_guide": [
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
        "How Do I Cancel or Change a Pending Distribution Request?",
        "401(k) Loan Basics and Support Guide",
    ],
    "how_do_i_cancel_or_change_a_pending_distribution_request": [
        "ForUsAll 401(k) Hardship Withdrawal \u2014 Complete Guide (Rules, Fees, Steps, Timelines)",
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "401k_required_minimum_distributions_rmds_rules_deadlines_penalties_exceptions_and_roth_conversion_impact",
        "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover",
        "LT: Can I Split My 401(k) Rollover Between Multiple Providers?",
        "LT: 401(k) Loan Complete Guide \u2014 Submission, Repayment & Support",
        "Vanguard: How to Submit a Loan Request",
    ],
    "lt_request_401k_termination_withdrawal_or_rollover": [
        "LT: Multi-Factor Authentication (MFA) for Your ForUsAll 401(k) Account",
        "How Do I Cancel or Change a Pending Distribution Request?",
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "LT: Can I Split My 401(k) Rollover Between Multiple Providers?",
        "Missed 60-Day Indirect Rollover Deadline: Tax Consequences, IRS Exceptions, and When to Refer to a Tax Professional",
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
    ],
    "lt_trust_401k_loan_complete_guide_submission_repayment_support": [
        "LT: Multi-Factor Authentication (MFA) for Your ForUsAll 401(k) Account",
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
        "401(k) Loan Basics and Support Guide",
        "Vanguard: How to Submit a Loan Request",
        "How Do I Cancel or Change a Pending Distribution Request?",
    ],
    "missed_60_day_rollover_window": [
        "401(k) Options After Leaving Your Job (Terminated Distribution): Rollovers, Cash Outs, Roth and Pre-Tax",
        "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover",
        "LT: Can I Split My 401(k) Rollover Between Multiple Providers?",
        "401(k) Required Minimum Distributions (RMDs): Rules, Deadlines, Penalties, Exceptions, and Roth Conversion Impact",
        "401(k) Force-Out Process (Involuntary Distribution): Balance Thresholds, Safe Harbor IRA Rollovers, Fee-Outs, and Compliance",
    ],
    "vanguard_how_to_submit_a_loan_request": [
        "LT: 401(k) Loan Complete Guide \u2014 Submission, Repayment & Support",
        "401(k) Loan Basics and Support Guide",
        "Can I Take Money From My 401(k) While Employed? Your Options Explained",
        "How Do I Cancel or Change a Pending Distribution Request?",
    ],
}


def detect_indent(text: str) -> int:
    """Detect indent (in spaces) by looking at the first indented line under root."""
    for line in text.splitlines()[1:]:
        if line.strip().startswith('"') and line[0] == ' ':
            return len(line) - len(line.lstrip(' '))
    return 4


def main():
    # Build path -> article_id map for all 16 articles
    path_by_aid = {}
    for folder in ['PA/Participant Dashboard', 'PA/Loans', 'PA/Distributions']:
        for path in sorted(glob.glob(f'{folder}/*.json')):
            with open(path, 'r', encoding='utf-8') as f:
                a = json.load(f)
            path_by_aid[a['metadata']['article_id']] = path

    affected = []
    for aid, new_refs in PLAN.items():
        if aid not in path_by_aid:
            print(f"  ❌ MISSING article in filesystem: {aid}")
            continue
        path = path_by_aid[aid]
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        indent = detect_indent(text)
        article = json.loads(text)
        old = article['details']['references'].get('internal_articles', [])
        if old == new_refs:
            print(f"  = NO CHANGE: {aid}")
            continue
        article['details']['references']['internal_articles'] = new_refs
        new_text = json.dumps(article, indent=indent, ensure_ascii=False)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_text)
        affected.append((aid, path, len(old), len(new_refs)))
        print(f"  ✔ UPDATED: {aid}  ({len(old)} -> {len(new_refs)} refs, indent={indent})")

    print()
    print(f"Total articles updated: {len(affected)}")
    return affected


if __name__ == "__main__":
    main()

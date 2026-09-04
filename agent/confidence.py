"""
Confidence Engine (plan section 14).

Produces a single 0-100 confidence score per transaction reflecting how
completely the available evidence explains the system's conclusion —
whether that conclusion is a clean MATCH or a well-understood EXCEPTION.

Thresholds (tunable, see evaluation/metrics.py for how they're validated
against ground truth rather than asserted as globally "optimal" —
plan section 14):
    >= 95        -> AUTO_RESOLVE
    70 - 94.99   -> AI_REVIEW
    <  70        -> HUMAN_REVIEW
"""

AUTO_RESOLVE_THRESHOLD = 95.0
AI_REVIEW_THRESHOLD = 70.0


def score(row: dict, investigation: dict | None = None) -> float:
    dtype = row.get("discrepancy_type")

    if dtype == "MATCH":
        return 99.0

    if dtype == "DUPLICATE":
        # unambiguous: identical transaction_id + amount + customer, high-confidence exception
        return 97.0

    if dtype == "MISSING_SETTLEMENT":
        # unambiguous data fact, but could still be "pending" rather than an error
        return 90.0

    if dtype == "FEE_MISMATCH":
        return 96.0

    if dtype == "TAX_MISMATCH":
        return 96.0

    if dtype == "DATE_ANOMALY":
        days = row.get("date_diff_days") or 0
        # more days overdue = more confident this is a genuine anomaly, not noise
        return min(93.0, 70.0 + (days - 5) * 2)

    if dtype == "PARTIAL_SETTLEMENT":
        expected_net = row.get("expected_net") or 0
        actual_net = row.get("net_amount") or 0
        coverage = (actual_net / expected_net) if expected_net else 0
        # confidence here reflects how much IS accounted for, capped below
        # auto-resolve since a genuine remainder always needs a human sign-off
        return round(min(85.0, 40.0 + coverage * 45), 1)

    if dtype == "MISSING_BANK_CREDIT":
        # unambiguous data fact (no bank row at all) but always needs a human
        # to confirm the money is genuinely missing vs. still in transit
        return 88.0

    if dtype == "BANK_AMOUNT_MISMATCH":
        return 95.0

    if dtype == "LEDGER_MISMATCH":
        return 94.0

    if dtype == "LEDGER_RECORD_MISSING":
        return 85.0

    if dtype == "TAX_RECORD_MISSING":
        # deterministic, low-severity compliance gap -- safe to auto-resolve
        # (flag for compliance follow-up) since there's no financial ambiguity
        return 97.0

    if dtype == "UNKNOWN_MISMATCH":
        net_diff = row.get("net_diff") or 0
        amount = row.get("amount") or 1
        if investigation and investigation.get("fully_explained"):
            return 92.0
        unexplained = investigation.get("unexplained_amount", net_diff) if investigation else net_diff
        # confidence falls as the unexplained amount grows relative to the
        # transaction size; investigation that finds NOTHING further caps out low
        ratio_unexplained = min(abs(unexplained) / abs(amount), 1.0) if amount else 1.0
        conf = 60.0 * (1 - ratio_unexplained)
        return round(max(conf, 15.0), 1)

    return 50.0  # unknown discrepancy_type — be conservative

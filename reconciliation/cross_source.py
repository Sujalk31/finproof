"""
Cross-source reconciliation (plan section 9: bank + merchant ledger + tax
records as independent sources of truth).

This runs AFTER discrepancy.py's payment/settlement/fee/tax classification.
It only inspects rows where the settlement itself already reconciles
cleanly (discrepancy_type in {MATCH, DATE_ANOMALY}) -- if the settlement
math is already broken, that is the single root cause we report; we don't
pile a second, possibly-confusing root cause on top of it (plan section 17:
one clear root cause per exception, not a bag of symptoms).

Three independent checks, each deterministic (no LLM, no guessing):
  1. Bank statement  -> did the money actually arrive, and for how much?
  2. Merchant ledger  -> was the correct amount booked in accounting?
  3. Tax record       -> was a compliance-grade tax record filed at all?
"""

from reconciliation.discrepancy import EPSILON

ELIGIBLE_TYPES = {"MATCH", "DATE_ANOMALY"}


def check_row(row: dict) -> dict:
    """Returns {} if nothing to change, otherwise an override dict with the
    same keys discrepancy.classify_row produces."""

    if row.get("discrepancy_type") not in ELIGIBLE_TYPES:
        return {}

    net_amount = row.get("net_amount")
    bank_amount = row.get("bank_amount")
    ledger_credit = row.get("ledger_credit")
    filed_tax_amount = row.get("filed_tax_amount")

    # --- 1. Bank statement checks ---
    if bank_amount is None or _is_nan(bank_amount):
        return {
            "discrepancy_type": "MISSING_BANK_CREDIT",
            "needs_investigation": True,
            "auto_reason": (
                f"Settlement of {net_amount} was booked but no corresponding bank credit "
                f"was found in the bank statement -- funds may not have actually landed."
            ),
        }

    bank_diff = round(bank_amount - net_amount, 2) if net_amount is not None else None
    if bank_diff is not None and abs(bank_diff) > EPSILON:
        return {
            "discrepancy_type": "BANK_AMOUNT_MISMATCH",
            "needs_investigation": True,
            "auto_reason": (
                f"Bank credited {bank_amount} but the settlement recorded a net of "
                f"{net_amount} (difference {bank_diff}); settlement and bank disagree "
                f"on how much money actually moved."
            ),
        }

    # --- 2. Merchant ledger checks ---
    if ledger_credit is None or _is_nan(ledger_credit):
        return {
            "discrepancy_type": "LEDGER_RECORD_MISSING",
            "needs_investigation": True,
            "auto_reason": "No merchant ledger entry was found for this settled transaction.",
        }

    ledger_diff = round(ledger_credit - net_amount, 2) if net_amount is not None else None
    if ledger_diff is not None and abs(ledger_diff) > EPSILON:
        return {
            "discrepancy_type": "LEDGER_MISMATCH",
            "needs_investigation": True,
            "auto_reason": (
                f"Merchant ledger booked {ledger_credit} but the settlement net was "
                f"{net_amount} (difference {ledger_diff}); this looks like an accounting "
                f"posting error rather than a payment-side issue."
            ),
        }

    # --- 3. Tax compliance record ---
    if filed_tax_amount is None or _is_nan(filed_tax_amount):
        return {
            "discrepancy_type": "TAX_RECORD_MISSING",
            "needs_investigation": False,  # low severity, deterministic, doesn't need the LLM
            "auto_reason": (
                "Settlement and bank/ledger reconcile, but no separate compliance tax "
                "record was filed for this transaction."
            ),
        }

    return {}


def _is_nan(x) -> bool:
    try:
        return x != x  # NaN != NaN is True
    except Exception:
        return False


def apply_cross_source_checks(records: list) -> list:
    """records: list of dicts (already classified by discrepancy.classify_row,
    with bank_amount / ledger_credit / filed_tax_amount columns attached).
    Mutates and returns the same list with overrides applied in place."""
    for row in records:
        override = check_row(row)
        if override:
            row.update(override)
    return records

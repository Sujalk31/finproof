"""
Discrepancy detection (plan sections 7-8).

Core idea: the fee_config table is treated as ground truth for how fee/tax
SHOULD be computed. For every payment/settlement pair we independently
recompute the expected fee, tax and net amount from first principles, then
compare that to what was actually settled. The GAP is then classified into
a specific, explainable discrepancy type using simple arithmetic — no LLM
required for this step (plan section 25: LLM should not do arithmetic).

Only cases where the gap can't be explained by any deterministic rule are
marked `needs_investigation=True` and handed to the AI agent.
"""

from datetime import datetime

import pandas as pd

EPSILON = 1.0          # rupees — tolerance for float rounding noise
NORMAL_SETTLEMENT_DAYS = 5  # settlement within this window is normal, beyond it is anomalous
PARTIAL_SETTLEMENT_RATIO = 0.90  # actual_net below this fraction of expected => partial settlement


def _expected_fee_tax_net(amount, fee_pct, fixed_fee, tax_rate):
    fee = round(amount * fee_pct / 100 + fixed_fee, 2)
    tax = round(fee * tax_rate / 100, 2)
    net = round(amount - fee - tax, 2)
    return fee, tax, net


def _date_diff_days(pay_date_str, settle_date_str):
    try:
        d1 = datetime.strptime(pay_date_str[:10], "%Y-%m-%d")
        d2 = datetime.strptime(settle_date_str[:10], "%Y-%m-%d")
        return (d2 - d1).days
    except (ValueError, TypeError):
        return None


def classify_row(row, fee_config_by_merchant: dict) -> dict:
    """Classify a single merged payment/settlement row. Returns a dict of
    computed fields to attach to the row."""
    result = {
        "expected_fee": None,
        "expected_tax": None,
        "expected_net": None,
        "net_diff": None,
        "date_diff_days": None,
        "discrepancy_type": None,
        "needs_investigation": False,
        "auto_reason": None,
    }

    # --- duplicate takes priority, no need to look at settlement math ---
    if row.get("is_duplicate"):
        result["discrepancy_type"] = "DUPLICATE"
        result["auto_reason"] = "Duplicate payment record for the same transaction_id."
        return result

    # --- missing settlement ---
    if pd.isna(row.get("net_amount")):
        result["discrepancy_type"] = "MISSING_SETTLEMENT"
        result["auto_reason"] = "No settlement record found for this payment."
        return result

    cfg = fee_config_by_merchant.get(row["merchant_id"])
    if cfg is None:
        result["discrepancy_type"] = "UNKNOWN_MISMATCH"
        result["needs_investigation"] = True
        result["auto_reason"] = "No fee configuration found for this merchant."
        return result

    exp_fee, exp_tax, exp_net = _expected_fee_tax_net(
        row["amount"], cfg["fee_percentage"], cfg["fixed_fee"], cfg["tax_rate"]
    )
    result["expected_fee"] = exp_fee
    result["expected_tax"] = exp_tax
    result["expected_net"] = exp_net

    net_diff = round(row["net_amount"] - exp_net, 2)
    result["net_diff"] = net_diff

    date_diff = _date_diff_days(row["date_norm_pay"], row["date_norm_settle"])
    result["date_diff_days"] = date_diff

    fee_diff = round(row["fee"] - exp_fee, 2) if not pd.isna(row.get("fee")) else None
    tax_diff = round(row["tax"] - exp_tax, 2) if not pd.isna(row.get("tax")) else None

    # --- net amount reconciles exactly ---
    if abs(net_diff) <= EPSILON:
        if date_diff is not None and date_diff > NORMAL_SETTLEMENT_DAYS:
            result["discrepancy_type"] = "DATE_ANOMALY"
            result["needs_investigation"] = True
            result["auto_reason"] = (
                f"Settlement occurred {date_diff} days after payment, beyond the "
                f"{NORMAL_SETTLEMENT_DAYS}-day normal window, even though amounts reconcile."
            )
        else:
            result["discrepancy_type"] = "MATCH"
            result["auto_reason"] = (
                f"Settlement net ({row['net_amount']}) matches expected net ({exp_net}) "
                f"= amount - fee({exp_fee}) - tax({exp_tax})."
            )
        return result

    # --- net doesn't reconcile: try to explain the gap deterministically ---
    if fee_diff is not None and abs(fee_diff) > EPSILON and (tax_diff is None or abs(tax_diff - fee_diff * cfg["tax_rate"] / 100) <= EPSILON):
        result["discrepancy_type"] = "FEE_MISMATCH"
        result["auto_reason"] = (
            f"Applied fee ({row['fee']}) differs from configured fee ({exp_fee}) by {fee_diff}; "
            f"tax scales consistently with the applied fee, so the fee itself is the root cause."
        )
        return result

    if tax_diff is not None and abs(tax_diff) > EPSILON and (fee_diff is None or abs(fee_diff) <= EPSILON):
        result["discrepancy_type"] = "TAX_MISMATCH"
        result["auto_reason"] = (
            f"Applied GST ({row['tax']}) differs from expected GST ({exp_tax}) by {tax_diff}, "
            f"while the fee itself matches the configured rate."
        )
        return result

    if row["net_amount"] < exp_net * PARTIAL_SETTLEMENT_RATIO:
        result["discrepancy_type"] = "PARTIAL_SETTLEMENT"
        result["needs_investigation"] = True
        result["auto_reason"] = (
            f"Settled net ({row['net_amount']}) is only "
            f"{row['net_amount']/exp_net:.0%} of the expected net ({exp_net}); "
            f"looks like a partial payout rather than a fee/tax issue."
        )
        return result

    # --- unexplained by any deterministic rule -> send to AI agent ---
    result["discrepancy_type"] = "UNKNOWN_MISMATCH"
    result["needs_investigation"] = True
    result["auto_reason"] = (
        f"Net difference of {net_diff} is not explained by fee, tax, date, or partial-settlement rules."
    )
    return result


def run_discrepancy_detection(merged_df: pd.DataFrame, fee_config_df: pd.DataFrame) -> pd.DataFrame:
    fee_config_by_merchant = {
        row["merchant_id"]: row for _, row in fee_config_df.iterrows()
    }
    records = merged_df.to_dict("records")
    classified = [classify_row(r, fee_config_by_merchant) for r in records]
    classified_df = pd.DataFrame(classified)
    out = pd.concat([merged_df.reset_index(drop=True), classified_df.reset_index(drop=True)], axis=1)
    return out

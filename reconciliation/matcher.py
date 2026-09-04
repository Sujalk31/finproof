"""
Matching engine — deterministic first (plan section 7).

Level 1: exact transaction_id join between payments and settlements.
Level 2 (duplicate detection): multiple payment rows sharing a transaction_id.
Level 3 (identity check): fuzzy-compare each merchant's name against a
canonical name built from the majority representation for that merchant_id,
so formatting noise never gets treated as a real discrepancy.

Only genuinely ambiguous financial gaps are handed to discrepancy.py /
the AI agent — matching itself stays 100% rule-based and free of LLM cost.
"""

from collections import Counter

import pandas as pd

from reconciliation.fuzzy_matcher import similarity
from reconciliation.normalizer import normalize_merchant_name


def build_merchant_master(payments_df: pd.DataFrame) -> dict:
    """Canonical merchant name per merchant_id = most frequent normalized name's
    most frequent raw representation."""
    master = {}
    for merchant_id, group in payments_df.groupby("merchant_id"):
        counts = Counter(group["merchant_name"])
        canonical_raw = counts.most_common(1)[0][0]
        master[merchant_id] = {
            "canonical_name": canonical_raw,
            "canonical_name_norm": normalize_merchant_name(canonical_raw),
        }
    return master


def detect_duplicates(payments_df: pd.DataFrame) -> pd.DataFrame:
    """Return payments_df with a `is_duplicate` flag. A duplicate = a
    transaction_id appearing more than once with identical amount/customer
    (true duplicate submission), keeping the first occurrence as canonical."""
    df = payments_df.copy()
    df["is_duplicate"] = False
    dup_mask = df.duplicated(subset=["transaction_id"], keep="first")
    df.loc[dup_mask, "is_duplicate"] = True
    return df


def check_identity(payments_df: pd.DataFrame, master: dict, threshold: float = 0.80) -> pd.DataFrame:
    df = payments_df.copy()

    def _check(row):
        canon = master.get(row["merchant_id"], {}).get("canonical_name_norm", "")
        sim = similarity(row["merchant_name_norm"], canon)
        return pd.Series({"identity_similarity": round(sim, 3), "identity_ok": sim >= threshold})

    df = df.join(df.apply(_check, axis=1))
    return df


def join_payment_settlement(payments_df: pd.DataFrame, settlements_df: pd.DataFrame) -> pd.DataFrame:
    """Left-join payments -> settlements on transaction_id. Payments with no
    matching settlement come through with NaNs (caught downstream as
    MISSING_SETTLEMENT)."""
    merged = payments_df.merge(
        settlements_df,
        on="transaction_id",
        how="left",
        suffixes=("_pay", "_settle"),
    )
    return merged


def join_bank(merged_df: pd.DataFrame, bank_df: pd.DataFrame) -> pd.DataFrame:
    """Left-join settlement_id -> bank credit. Settlements with no matching
    bank row come through with NaN bank_amount (caught as MISSING_BANK_CREDIT)."""
    bank_slim = bank_df[["settlement_id", "amount", "date_norm"]].rename(
        columns={"amount": "bank_amount", "date_norm": "bank_date"}
    )
    return merged_df.merge(bank_slim, on="settlement_id", how="left")


def join_ledger(merged_df: pd.DataFrame, ledger_df: pd.DataFrame) -> pd.DataFrame:
    """Left-join transaction_id -> merchant ledger credit entry."""
    ledger_slim = ledger_df[["transaction_id", "credit", "date_norm"]].rename(
        columns={"credit": "ledger_credit", "date_norm": "ledger_date"}
    )
    return merged_df.merge(ledger_slim, on="transaction_id", how="left")


def join_tax(merged_df: pd.DataFrame, tax_df: pd.DataFrame) -> pd.DataFrame:
    """Left-join transaction_id -> separately filed tax record."""
    tax_slim = tax_df[["transaction_id", "tax_amount"]].rename(
        columns={"tax_amount": "filed_tax_amount"}
    )
    return merged_df.merge(tax_slim, on="transaction_id", how="left")

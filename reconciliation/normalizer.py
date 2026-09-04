"""
Normalization layer.

Different source systems represent the same real-world entity differently
(section 2 of plan). This module makes those representations comparable
BEFORE any matching happens, so matching logic never has to deal with raw
formatting noise.
"""

import re
from datetime import datetime

_LEGAL_SUFFIXES = [
    "pvt ltd", "pvt. ltd.", "p ltd", "private limited", "llp", "l.l.p.",
    "ltd", "limited",
]


def normalize_merchant_name(name: str) -> str:
    """Lowercase, strip punctuation, strip legal suffixes, collapse whitespace."""
    if not name:
        return ""
    n = name.lower().strip()
    n = n.replace(".", "").replace(",", "")
    n = re.sub(r"\s+", " ", n)
    for suffix in _LEGAL_SUFFIXES:
        suffix_clean = suffix.replace(".", "")
        if n.endswith(suffix_clean):
            n = n[: -len(suffix_clean)].strip()
    return n.strip()


def normalize_date(value: str) -> str:
    """Return YYYY-MM-DD regardless of input format (handles date or datetime strings)."""
    if not value:
        return ""
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # last resort: take the date-looking prefix
    return value[:10]


def normalize_amount(value) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, str):
        value = value.replace(",", "").replace("₹", "").strip()
    return round(float(value), 2)


def normalize_transaction_id(value: str) -> str:
    if not value:
        return ""
    return value.strip().upper()


def normalize_payments(df):
    df = df.copy()
    df["transaction_id"] = df["transaction_id"].apply(normalize_transaction_id)
    df["merchant_name_norm"] = df["merchant_name"].apply(normalize_merchant_name)
    df["amount"] = df["amount"].apply(normalize_amount)
    df["date_norm"] = df["timestamp"].apply(normalize_date)
    return df


def normalize_settlements(df):
    df = df.copy()
    df["transaction_id"] = df["transaction_id"].apply(normalize_transaction_id)
    for col in ["gross_amount", "fee", "tax", "net_amount"]:
        df[col] = df[col].apply(normalize_amount)
    df["date_norm"] = df["settlement_date"].apply(normalize_date)
    return df


def normalize_fee_config(df):
    df = df.copy()
    df["fee_percentage"] = df["fee_percentage"].astype(float)
    df["fixed_fee"] = df["fixed_fee"].astype(float)
    df["tax_rate"] = df["tax_rate"].astype(float)
    return df


def normalize_bank(df):
    df = df.copy()
    df["amount"] = df["amount"].apply(normalize_amount)
    df["date_norm"] = df["transaction_date"].apply(normalize_date)
    return df


def normalize_ledger(df):
    df = df.copy()
    df["transaction_id"] = df["transaction_id"].apply(normalize_transaction_id)
    df["credit"] = df["credit"].apply(normalize_amount)
    df["debit"] = df["debit"].apply(normalize_amount)
    df["date_norm"] = df["date"].apply(normalize_date)
    return df


def normalize_tax(df):
    df = df.copy()
    df["transaction_id"] = df["transaction_id"].apply(normalize_transaction_id)
    df["tax_amount"] = df["tax_amount"].apply(normalize_amount)
    return df

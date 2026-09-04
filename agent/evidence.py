"""
Builds a structured evidence bundle for a single transaction -- the exact
payload shown in the plan's "Evidence Graph" (section 13) and used both by
the investigator agent, the chatbot, and the "Why was this resolved /
escalated?" UI feature (section 31).
"""


def build_evidence(row: dict) -> dict:
    return {
        "transaction_id": row.get("transaction_id"),
        "merchant_id": row.get("merchant_id"),
        "merchant_name": row.get("merchant_name"),
        "payment": {
            "amount": row.get("amount"),
            "date": row.get("date_norm_pay") or row.get("date_norm"),
            "method": row.get("payment_method"),
        },
        "settlement": {
            "gross_amount": row.get("gross_amount"),
            "fee": row.get("fee"),
            "tax": row.get("tax"),
            "net_amount": row.get("net_amount"),
            "date": row.get("date_norm_settle"),
        },
        "bank": {
            "amount": row.get("bank_amount"),
            "date": row.get("bank_date"),
        },
        "ledger": {
            "credit": row.get("ledger_credit"),
            "date": row.get("ledger_date"),
        },
        "filed_tax_amount": row.get("filed_tax_amount"),
        "expected": {
            "expected_fee": row.get("expected_fee"),
            "expected_tax": row.get("expected_tax"),
            "expected_net": row.get("expected_net"),
        },
        "net_diff": row.get("net_diff"),
        "date_diff_days": row.get("date_diff_days"),
        "discrepancy_type": row.get("discrepancy_type"),
        "deterministic_reason": row.get("auto_reason"),
    }

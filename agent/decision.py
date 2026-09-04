"""
Decision routing (plan sections 14-15).

Turns a discrepancy_type + confidence score into a final routed decision
and a system-level label (MATCH vs EXCEPTION) that can be compared against
ground truth.
"""

from agent.confidence import AUTO_RESOLVE_THRESHOLD, AI_REVIEW_THRESHOLD

# discrepancy types that represent a genuine problem (vs. a benign match)
EXCEPTION_TYPES = {
    "DUPLICATE", "MISSING_SETTLEMENT", "AMOUNT_MISMATCH", "FEE_MISMATCH",
    "TAX_MISMATCH", "DATE_ANOMALY", "PARTIAL_SETTLEMENT", "UNKNOWN_MISMATCH",
    "MISSING_BANK_CREDIT", "BANK_AMOUNT_MISMATCH", "LEDGER_MISMATCH",
    "LEDGER_RECORD_MISSING", "TAX_RECORD_MISSING",
}


def route(discrepancy_type: str, confidence: float) -> dict:
    system_label = "MATCH" if discrepancy_type == "MATCH" else "EXCEPTION"

    if confidence >= AUTO_RESOLVE_THRESHOLD:
        status = "AUTO_RESOLVED"
    elif confidence >= AI_REVIEW_THRESHOLD:
        status = "AI_REVIEW"
    else:
        status = "HUMAN_REVIEW"

    return {"system_label": system_label, "status": status}

"""
Ground-truth comparison (plan section 10, 19).

The reconciliation engine NEVER sees ground_truth.csv. This module is only
used after the pipeline has produced its own decisions, to objectively
score them — this is what lets FinProof report a real accuracy number
instead of an asserted one.
"""

import pandas as pd


def load_ground_truth(path: str) -> pd.DataFrame:
    return pd.read_csv(path)


def evaluate(final_decisions: pd.DataFrame, ground_truth: pd.DataFrame) -> dict:
    """
    final_decisions: one row per transaction_id with columns
        [transaction_id, system_label, status, confidence, discrepancy_type, amount]
    ground_truth: one row per transaction_id with columns
        [transaction_id, injected_error_type, true_label, true_expected_net, true_explanation]
    """
    merged = final_decisions.merge(ground_truth, on="transaction_id", how="inner")

    total = len(merged)
    correct = (merged["system_label"] == merged["true_label"]).sum()
    accuracy = correct / total if total else 0.0

    # Precision/recall on "EXCEPTION" as the positive class (the harder, higher-stakes class)
    tp = ((merged["system_label"] == "EXCEPTION") & (merged["true_label"] == "EXCEPTION")).sum()
    fp = ((merged["system_label"] == "EXCEPTION") & (merged["true_label"] == "MATCH")).sum()
    fn = ((merged["system_label"] == "MATCH") & (merged["true_label"] == "EXCEPTION")).sum()
    tn = ((merged["system_label"] == "MATCH") & (merged["true_label"] == "MATCH")).sum()

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # False resolution rate: of everything the system AUTO_RESOLVED, how much
    # was actually a mislabel against ground truth? This is the critical
    # safety metric for a finance controller (plan section 19).
    auto_resolved = merged[merged["status"] == "AUTO_RESOLVED"]
    n_auto_resolved = len(auto_resolved)
    n_wrong_auto = (auto_resolved["system_label"] != auto_resolved["true_label"]).sum()
    false_resolution_rate = (n_wrong_auto / n_auto_resolved) if n_auto_resolved else 0.0

    return {
        "total_evaluated": int(total),
        "correct": int(correct),
        "accuracy": round(accuracy, 4),
        "precision_exception": round(precision, 4),
        "recall_exception": round(recall, 4),
        "f1_exception": round(f1, 4),
        "confusion_matrix": {
            "true_positive_exception": int(tp),
            "false_positive_exception": int(fp),
            "false_negative_exception": int(fn),
            "true_negative_match": int(tn),
        },
        "auto_resolved_count": int(n_auto_resolved),
        "auto_resolved_wrong": int(n_wrong_auto),
        "false_resolution_rate": round(false_resolution_rate, 4),
        "merged": merged,
    }

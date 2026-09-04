"""
Batch metrics + finance controller report generation (plan sections 18-22).
"""

import pandas as pd


def record_level_metrics(final_decisions: pd.DataFrame) -> dict:
    total = len(final_decisions)
    matched = int((final_decisions["system_label"] == "MATCH").sum())
    exceptions = int((final_decisions["system_label"] == "EXCEPTION").sum())
    auto_resolved = int((final_decisions["status"] == "AUTO_RESOLVED").sum())
    ai_review = int((final_decisions["status"] == "AI_REVIEW").sum())
    human_review = int((final_decisions["status"] == "HUMAN_REVIEW").sum())

    return {
        "records_processed": total,
        "matched": matched,
        "exceptions": exceptions,
        "auto_resolved": auto_resolved,
        "ai_review": ai_review,
        "human_review": human_review,
        "resolution_rate": round((auto_resolved + ai_review) / total, 4) if total else 0.0,
        "match_rate": round(matched / total, 4) if total else 0.0,
    }


def amount_level_metrics(final_decisions: pd.DataFrame) -> dict:
    total_amount = float(final_decisions["amount"].sum())
    reconciled_amount = float(
        final_decisions.loc[final_decisions["status"].isin(["AUTO_RESOLVED", "AI_REVIEW"]), "amount"].sum()
    )
    unresolved_amount = float(
        final_decisions.loc[final_decisions["status"] == "HUMAN_REVIEW", "amount"].sum()
    )
    return {
        "total_amount_processed": round(total_amount, 2),
        "amount_reconciled": round(reconciled_amount, 2),
        "amount_unresolved": round(unresolved_amount, 2),
        "pct_amount_reconciled": round(reconciled_amount / total_amount, 4) if total_amount else 0.0,
    }


def exception_breakdown(final_decisions: pd.DataFrame) -> pd.DataFrame:
    exc = final_decisions[final_decisions["system_label"] == "EXCEPTION"]
    if exc.empty:
        return pd.DataFrame(columns=["discrepancy_type", "count", "pct_of_exceptions", "total_amount"])
    breakdown = (
        exc.groupby("discrepancy_type")
        .agg(count=("transaction_id", "count"), total_amount=("amount", "sum"))
        .reset_index()
        .sort_values("count", ascending=False)
    )
    breakdown["pct_of_exceptions"] = round(breakdown["count"] / len(exc) * 100, 1)
    breakdown["total_amount"] = breakdown["total_amount"].round(2)
    return breakdown


def merchant_risk_ranking(final_decisions: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    exc = final_decisions[final_decisions["system_label"] == "EXCEPTION"]
    if exc.empty or "merchant_id" not in exc.columns:
        return pd.DataFrame(columns=["merchant_id", "merchant_name", "exception_count", "exception_amount"])
    has_name = "merchant_name" in exc.columns
    agg_kwargs = {"exception_count": ("transaction_id", "count"), "exception_amount": ("amount", "sum")}
    if has_name:
        agg_kwargs["merchant_name"] = ("merchant_name", "first")
    ranking = (
        exc.groupby("merchant_id")
        .agg(**agg_kwargs)
        .reset_index()
        .sort_values("exception_count", ascending=False)
        .head(top_n)
    )
    ranking["exception_amount"] = ranking["exception_amount"].round(2)
    return ranking


def build_report(final_decisions: pd.DataFrame, eval_result: dict, elapsed_seconds: float) -> dict:
    rl = record_level_metrics(final_decisions)
    al = amount_level_metrics(final_decisions)
    breakdown = exception_breakdown(final_decisions)
    risk = merchant_risk_ranking(final_decisions)

    top_exception = breakdown.iloc[0]["discrepancy_type"] if not breakdown.empty else None
    largest_exception_amount = float(
        final_decisions.loc[final_decisions["system_label"] == "EXCEPTION", "amount"].max()
        if rl["exceptions"] else 0.0
    )
    highest_risk_merchant = risk.iloc[0]["merchant_id"] if not risk.empty else None
    highest_risk_merchant_name = (
        risk.iloc[0]["merchant_name"] if not risk.empty and "merchant_name" in risk.columns else None
    )

    report = {
        "record_level": rl,
        "amount_level": al,
        "accuracy": {
            "accuracy": eval_result["accuracy"],
            "precision_exception": eval_result["precision_exception"],
            "recall_exception": eval_result["recall_exception"],
            "f1_exception": eval_result["f1_exception"],
            "false_resolution_rate": eval_result["false_resolution_rate"],
            "confusion_matrix": eval_result["confusion_matrix"],
        },
        "throughput": {
            "elapsed_seconds": round(elapsed_seconds, 3),
            "records_per_second": round(rl["records_processed"] / elapsed_seconds, 1) if elapsed_seconds else None,
        },
        "top_exception_type": top_exception,
        "largest_exception_amount": round(largest_exception_amount, 2),
        "highest_risk_merchant": highest_risk_merchant,
        "highest_risk_merchant_name": highest_risk_merchant_name,
        "exception_breakdown": breakdown.to_dict("records"),
        "merchant_risk_ranking": risk.to_dict("records"),
    }
    return report


def format_text_report(report: dict) -> str:
    rl = report["record_level"]
    al = report["amount_level"]
    acc = report["accuracy"]
    tp = report["throughput"]

    lines = [
        "FINANCE CONTROLLER REPORT",
        "─" * 40,
        f"Records processed:       {rl['records_processed']}",
        "",
        f"Matched:                  {rl['matched']}",
        f"Exceptions:               {rl['exceptions']}",
        f"  Auto-resolved:          {rl['auto_resolved']}",
        f"  AI review:              {rl['ai_review']}",
        f"  Human review:           {rl['human_review']}",
        "",
        f"Resolution rate:          {rl['resolution_rate']:.1%}",
        "",
        f"Amount processed:         Rs. {al['total_amount_processed']:,.2f}",
        f"Amount reconciled:        Rs. {al['amount_reconciled']:,.2f} ({al['pct_amount_reconciled']:.1%})",
        f"Amount unresolved:        Rs. {al['amount_unresolved']:,.2f}",
        "",
        f"Accuracy vs ground truth: {acc['accuracy']:.1%}",
        f"Precision (exception):    {acc['precision_exception']:.1%}",
        f"Recall (exception):       {acc['recall_exception']:.1%}",
        f"False resolution rate:    {acc['false_resolution_rate']:.2%}",
        "",
        f"Top exception type:       {report['top_exception_type']}",
        f"Largest exception amount: Rs. {report['largest_exception_amount']:,.2f}",
        f"Highest-risk merchant:    {report['highest_risk_merchant']}",
        "",
        f"Throughput:               {rl['records_processed']} records in {tp['elapsed_seconds']}s "
        f"({tp['records_per_second']} records/sec)",
    ]
    return "\n".join(lines)
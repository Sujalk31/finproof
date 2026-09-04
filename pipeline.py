"""
FinProof end-to-end pipeline.

    Financial Data (payments, settlements, bank, ledger, fee config, tax)
        -> Normalize -> Match -> Discrepancy Detection -> Cross-Source Checks
        -> AI Investigation -> Confidence -> Auto-Resolve / AI Review / Human Review
        -> Finance Report + Audit Trail + SQLite persistence

Run:
    python3 pipeline.py --data data --out output
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd

from reconciliation import normalizer, matcher, discrepancy, cross_source
from agent import evidence as evidence_mod
from agent import investigator, confidence, decision
from evaluation import ground_truth, metrics
from database import models
from json_safe import json_safe


def load_sources(data_dir: str):
    payments = pd.read_csv(os.path.join(data_dir, "payments.csv"))
    settlements = pd.read_csv(os.path.join(data_dir, "settlements.csv"))
    fee_config = pd.read_csv(os.path.join(data_dir, "fee_config.csv"))
    bank = pd.read_csv(os.path.join(data_dir, "bank.csv"))
    ledger = pd.read_csv(os.path.join(data_dir, "ledger.csv"))
    tax = pd.read_csv(os.path.join(data_dir, "tax.csv"))
    return payments, settlements, fee_config, bank, ledger, tax


def aggregate_to_transaction_level(row_results: pd.DataFrame) -> pd.DataFrame:
    """Collapse possibly-multiple rows per transaction_id (e.g. an original
    payment + its duplicate) into one final controller decision per
    transaction: EXCEPTION wins over MATCH, and within EXCEPTION rows we keep
    the lowest-confidence (most cautious) row as the representative decision.
    """
    df = row_results.copy()
    df["_priority"] = (df["system_label"] != "EXCEPTION").astype(int)
    df = df.sort_values(["transaction_id", "_priority", "confidence"], ascending=[True, True, True])
    reps = df.drop_duplicates(subset=["transaction_id"], keep="first").drop(columns=["_priority"])
    return reps.reset_index(drop=True)


def run_pipeline(data_dir: str = "data", out_dir: str = "output", progress_cb=None):
    """progress_cb(stage: str, pct: float) is called at each stage boundary
    so a caller (e.g. the FastAPI background task) can report live progress."""
    def _tick(stage, pct):
        if progress_cb:
            progress_cb(stage, pct)

    os.makedirs(out_dir, exist_ok=True)
    start = time.perf_counter()

    # ---- Stage 1: Ingest ----
    _tick("ingest", 5)
    payments, settlements, fee_config, bank, ledger, tax = load_sources(data_dir)
    n_raw_rows = len(payments)

    # ---- Stage 2: Normalize ----
    _tick("normalize", 15)
    payments = normalizer.normalize_payments(payments)
    settlements = normalizer.normalize_settlements(settlements)
    fee_config = normalizer.normalize_fee_config(fee_config)
    bank = normalizer.normalize_bank(bank)
    ledger = normalizer.normalize_ledger(ledger)
    tax = normalizer.normalize_tax(tax)

    # ---- Stage 3: Match (deterministic) ----
    _tick("match", 30)
    payments = matcher.detect_duplicates(payments)
    merchant_master = matcher.build_merchant_master(payments)
    payments = matcher.check_identity(payments, merchant_master)
    merged = matcher.join_payment_settlement(payments, settlements)
    merged = matcher.join_bank(merged, bank)
    merged = matcher.join_ledger(merged, ledger)
    merged = matcher.join_tax(merged, tax)

    # ---- Stage 4: Discrepancy Detection (deterministic root-cause rules) ----
    _tick("discrepancy_detection", 45)
    classified = discrepancy.run_discrepancy_detection(merged, fee_config)

    # ---- Stage 4b: Cross-source checks (bank / ledger / tax) ----
    _tick("cross_source_checks", 55)
    classified_records = cross_source.apply_cross_source_checks(classified.to_dict("records"))
    classified = pd.DataFrame(classified_records)

    # ---- Stage 5: AI Investigation (only for needs_investigation==True rows) ----
    # LLM calls are the slow part of the pipeline (one network round-trip per
    # exception, run sequentially). On a large batch that adds up fast, so by
    # default only the first MAX_LLM_INVESTIGATIONS exceptions get the real
    # LLM treatment -- every exception past that cap still gets a full,
    # complete investigation, just via the (much faster) deterministic
    # rule-based path instead of a network call. Nothing is skipped or left
    # uninvestigated; only *which* method investigates it changes.
    _tick("ai_investigation", 65)
    max_llm = int(os.environ.get("FINPROOF_MAX_LLM_INVESTIGATIONS", "50"))
    investigations = {}
    needs_inv = classified[classified["needs_investigation"] == True]  # noqa: E712
    for i, (_, row) in enumerate(needs_inv.iterrows()):
        ev = evidence_mod.build_evidence(row.to_dict())
        use_llm = i < max_llm
        investigations[row["transaction_id"] + "::" + str(row.name)] = investigator.investigate(ev, use_llm=use_llm)
    # ---- Stage 6: Confidence + Decision routing ----
    _tick("confidence_and_routing", 80)
    records = []
    audit_entries = []
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    for idx, row in classified.iterrows():
        row_dict = row.to_dict()
        inv = investigations.get(row["transaction_id"] + "::" + str(idx))
        conf = confidence.score(row_dict, inv)
        routed = decision.route(row["discrepancy_type"], conf)

        reason = row_dict.get("auto_reason")
        if inv:
            reason = inv["explanation"]

        rec = {
            "transaction_id": row["transaction_id"],
            "merchant_id": row.get("merchant_id"),
            "merchant_name": row.get("merchant_name"),
            "amount": row.get("amount"),
            "discrepancy_type": row["discrepancy_type"],
            "system_label": routed["system_label"],
            "status": routed["status"],
            "confidence": conf,
            "reason": reason,
        }
        records.append(rec)

        ev = evidence_mod.build_evidence(row_dict)

        # Split the old single "agent" string (e.g. "FinProof Controller v2 +
        # investigator (Rule-based reasoning (AI investigation unavailable))")
        # into a clean agent name plus a structured investigation
        # method/reason pair, so the UI can show:
        #   FinProof Controller + Evidence Investigator
        #   Investigation method: Deterministic fallback
        #   Reason: AI investigator unavailable
        inv_method = None
        inv_reason = None
        if inv:
            src = inv.get("source", "")
            paren_start, paren_end = src.find("("), src.rfind(")")
            inner = src[paren_start + 1:paren_end] if 0 <= paren_start < paren_end else None
            if src.startswith("AI Investigation"):
                # A real LLM call actually ran and produced this explanation.
                agent_name = "FinProof Controller + AI Investigator"
                inv_method = "AI-based investigation"
                inv_reason = inner or "AI investigator"
            else:
                # Deterministic rule-based fallback ran instead (no AI call,
                # AI call failed, or the per-batch AI cap was reached).
                agent_name = "FinProof Controller + Evidence Investigator"
                inv_method = "Deterministic fallback"
                reason_map = {
                    "AI investigation unavailable": "AI investigator unavailable",
                    "LLM investigation cap reached for this batch": "LLM investigation cap reached for this batch",
                    "no AI provider configured": "No AI provider configured",
                }
                inv_reason = reason_map.get(inner, inner or "Rule-based reasoning")
        else:
            agent_name = "FinProof Controller"

        audit_entries.append({
            "timestamp": now_iso,
            "transaction_id": row["transaction_id"],
            "decision": f"{routed['system_label']} / {routed['status']}",
            "reason": reason,
            "confidence": conf,
            "evidence": ev,
            "agent": agent_name,
            "investigation_method": inv_method,
            "investigation_reason": inv_reason,
        })

    row_results = pd.DataFrame(records)

    # ---- Stage 7: Aggregate to one decision per transaction ----
    final_decisions = aggregate_to_transaction_level(row_results)

    elapsed = time.perf_counter() - start

    # ---- Stage 8: Evaluate against hidden ground truth ----
    _tick("evaluation", 90)
    gt = ground_truth.load_ground_truth(os.path.join(data_dir, "ground_truth.csv"))
    eval_result = ground_truth.evaluate(final_decisions, gt)

    # ---- Stage 9: Report ----
    report = metrics.build_report(final_decisions, eval_result, elapsed)
    text_report = metrics.format_text_report(report)

    # ---- Stage 10: Persist everything ----
    _tick("persist", 97)
    final_decisions.to_csv(os.path.join(out_dir, "reconciled_results.csv"), index=False)
    row_results.to_csv(os.path.join(out_dir, "row_level_results.csv"), index=False)
    metrics.exception_breakdown(final_decisions).to_csv(
        os.path.join(out_dir, "exception_breakdown.csv"), index=False
    )
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(json_safe(report), f, indent=2, default=str)
    with open(os.path.join(out_dir, "report.txt"), "w") as f:
        f.write(text_report)
    with open(os.path.join(out_dir, "audit_trail.json"), "w") as f:
        json.dump(json_safe(audit_entries), f, indent=2, default=str)

    eval_export = {k: v for k, v in eval_result.items() if k != "merged"}
    with open(os.path.join(out_dir, "evaluation.json"), "w") as f:
        json.dump(json_safe(eval_export), f, indent=2, default=str)

    conn = models.get_connection(os.path.join(out_dir, "finproof.db"))
    models.save_decisions(conn, json_safe(final_decisions.to_dict("records")))
    models.save_audit_log(conn, json_safe(audit_entries))
    conn.close()

    _tick("done", 100)
    print(text_report)
    print(f"\nRaw payment rows ingested: {n_raw_rows}")
    print(f"Outputs written to: {os.path.abspath(out_dir)}")

    return report, final_decisions, eval_result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the FinProof reconciliation pipeline")
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--out", type=str, default="output")
    args = parser.parse_args()
    run_pipeline(data_dir=args.data, out_dir=args.out)
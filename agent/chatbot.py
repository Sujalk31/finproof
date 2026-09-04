"""
FinProof Chatbot.

A user-facing Q&A assistant over the *results of a completed pipeline run*
(not a generic finance chatbot -- plan section 28 explicitly warns against
"generic chatbot, ask your finance questions" as a WEAK project on its own).
It answers questions like:

    "Why was TXN100482 escalated?"
    "How many fee mismatches did we have?"
    "Which merchant has the most exceptions?"
    "What's our false resolution rate?"

Grounding strategy (kept deliberately simple and auditable, no vector DB
needed at this scale):
  1. If the message contains something that looks like a transaction ID
     (TXN followed by digits), pull that transaction's exact evidence +
     audit entry and hand it to the LLM verbatim.
  2. Otherwise, hand the LLM the finance-controller report summary
     (record/amount metrics, accuracy, exception breakdown, merchant risk
     ranking) so it can answer aggregate questions.
  3. The LLM is instructed to answer ONLY from the provided context and to
     say so plainly if the answer isn't in it -- same "honest failure"
     principle as the investigator agent (plan section 15).

Falls back to a small deterministic Q&A engine (regex/keyword matching over
the same context) when GROQ_API_KEY isn't configured, so the chatbot
works even fully offline.
"""

import re

from agent import llm_client

TXN_ID_RE = re.compile(r"\bTXN\d+\b", re.IGNORECASE)

SYSTEM_PROMPT = """You are the FinProof assistant, embedded in an AI finance-reconciliation \
controller dashboard. You answer questions about ONE completed reconciliation batch run, \
using only the JSON context you are given below the user's question.

Rules:
- Answer only from the provided context. Never invent transaction IDs, amounts, or figures.
- If the context doesn't contain the answer, say so plainly and suggest what the user could \
look up instead (e.g. a specific transaction ID) rather than guessing.
- Write like a normal chat reply, in plain sentences. Use AT MOST 2 bold terms in your \
entire answer (e.g. one status label and one key number) -- do not bold every noun, amount, \
or heading. No markdown tables. No section headers.
- Keep it to 3-6 sentences (or up to ~5 bullet points) unless the user explicitly asks for \
more detail.
- When discussing money, use the currency symbol/format already present in the context.
- When explaining a transaction decision, mention the discrepancy type, confidence, and the \
key evidence numbers that drove the decision -- in plain sentences, not a table.
"""


def _find_transaction_context(message: str, results_by_txn: dict, audit_by_txn: dict):
    match = TXN_ID_RE.search(message)
    if not match:
        return None
    txn_id = match.group(0).upper()
    row = results_by_txn.get(txn_id)
    audit = audit_by_txn.get(txn_id)
    if not row and not audit:
        return {"transaction_id": txn_id, "found": False}
    return {"transaction_id": txn_id, "found": True, "decision": row, "audit": audit}


def _rule_based_answer(message: str, report: dict, txn_context) -> str:
    msg = message.lower()

    if txn_context is not None:
        if not txn_context.get("found"):
            return f"I couldn't find {txn_context['transaction_id']} in this batch's results."

        txn_id = txn_context["transaction_id"]
        decision = txn_context.get("decision") or {}
        audit = txn_context.get("audit") or {}
        evidence = audit.get("evidence") or {}
        reason = audit.get("reason") or decision.get("reason") or "No reason recorded."

        merchant_id = evidence.get("merchant_id") or decision.get("merchant_id")
        merchant_name = evidence.get("merchant_name")
        merchant_label = f"{merchant_id} ({merchant_name})" if merchant_name else (merchant_id or "unknown")

        payment = evidence.get("payment") or {}
        settlement = evidence.get("settlement") or {}
        bank = evidence.get("bank") or {}
        ledger = evidence.get("ledger") or {}

        # --- "who is the merchant" / "which merchant" -------------------
        if any(k in msg for k in ["who is the merchant", "which merchant", "merchant for",
                                   "merchant is", "who is merchant", "what merchant"]):
            return (
                f"The merchant for {txn_id} is {merchant_label}.\n\n"
                f"Evidence: payment of {payment.get('amount')} via {payment.get('method')} "
                f"on {payment.get('date')}, settled net {settlement.get('net_amount')} "
                f"on {settlement.get('date')}."
            )

        # --- amount-specific question ------------------------------------
        if any(k in msg for k in ["how much", "what amount", "what is the amount", "payment amount"]):
            return (
                f"{txn_id} was paid for {payment.get('amount')} via {payment.get('method')} "
                f"on {payment.get('date')} (merchant {merchant_label}).\n\n"
                f"Evidence: settlement net {settlement.get('net_amount')} on "
                f"{settlement.get('date')}, bank credit {bank.get('amount')} on "
                f"{bank.get('date')}, ledger credit {ledger.get('credit')} on {ledger.get('date')}."
            )

        # --- date-specific question ---------------------------------------
        if any(k in msg for k in ["when was", "what date", "payment date", "settlement date"]):
            return (
                f"{txn_id}: payment on {payment.get('date')}, settlement on "
                f"{settlement.get('date')}, bank credit on {bank.get('date')}, "
                f"ledger credit on {ledger.get('date')} (merchant {merchant_label})."
            )

        # --- why / reason question -----------------------------------------
        if any(k in msg for k in ["why", "reason", "explain"]):
            return f"{txn_id} ({merchant_label}): {reason}"

        # --- default: full summary, now including merchant -----------------
        return (
            f"{txn_id}: merchant {merchant_label}, {decision.get('system_label', '?')} "
            f"({decision.get('status', '?')}), discrepancy type "
            f"{decision.get('discrepancy_type', '?')}, confidence "
            f"{decision.get('confidence', '?')}%.\nReason: {reason}"
        )

    rl = report.get("record_level", {})
    al = report.get("amount_level", {})
    acc = report.get("accuracy", {})
    breakdown = report.get("exception_breakdown", [])
    risk = report.get("merchant_risk_ranking", [])

    if any(k in msg for k in ["false resolution", "false-resolution"]):
        return f"False resolution rate for this batch: {acc.get('false_resolution_rate', 0):.2%}."

    if "accuracy" in msg:
        return f"Accuracy against ground truth: {acc.get('accuracy', 0):.1%}."

    if "resolution rate" in msg:
        return f"Resolution rate: {rl.get('resolution_rate', 0):.1%} (auto-resolved + AI review)."

    if "top exception" in msg or ("biggest" in msg and "exception" in msg) or "most common" in msg:
        return f"Top exception type: {report.get('top_exception_type')}." if breakdown else \
            "No exceptions in this batch."

    if "risk" in msg and "merchant" in msg:
        if not risk:
            return "No merchant risk data available (no exceptions in this batch)."
        top = risk[0]
        merchant_id = top.get("merchant_id")
        merchant_name = top.get("merchant_name")
        merchant_label = f"{merchant_id} ({merchant_name})" if merchant_name else merchant_id
        return (
            f"The highest-risk merchant is {merchant_label}, with "
            f"{top.get('exception_count')} exceptions totaling "
            f"{top.get('exception_amount'):,.2f} in this batch."
        )

    if "reconcil" in msg and ("amount" in msg or "money" in msg or "how much" in msg):
        return (
            f"Amount processed: {al.get('total_amount_processed', 0):,.2f}. "
            f"Reconciled: {al.get('amount_reconciled', 0):,.2f} "
            f"({al.get('pct_amount_reconciled', 0):.1%}). "
            f"Unresolved: {al.get('amount_unresolved', 0):,.2f}."
        )

    if "how many" in msg and "record" in msg:
        return f"{rl.get('records_processed', 0)} records were processed in this batch."

    if "throughput" in msg or "how fast" in msg or "how long" in msg:
        tp = report.get("throughput", {})
        return f"Processed {rl.get('records_processed', 0)} records in {tp.get('elapsed_seconds')}s ({tp.get('records_per_second')} rec/s)."

    # default: give the overall snapshot
    return (
        f"This batch processed {rl.get('records_processed', 0)} records: "
        f"{rl.get('matched', 0)} matched, {rl.get('exceptions', 0)} exceptions "
        f"({rl.get('auto_resolved', 0)} auto-resolved, {rl.get('ai_review', 0)} AI review, "
        f"{rl.get('human_review', 0)} human review). Accuracy vs ground truth: "
        f"{acc.get('accuracy', 0):.1%}. Ask me about a specific TXN id, the top exception "
        f"type, or the highest-risk merchant for more detail."
    )


def _grounding_note(txn_context) -> str:
    """A short, honest footer telling the user exactly what data this answer
    was read from -- so nothing looks like it was invented."""
    if txn_context is not None and txn_context.get("found"):
        return (
            f"\n\n_Grounded in: {txn_context['transaction_id']}'s stored decision + audit "
            f"evidence (payment, settlement, bank, and ledger records) from this batch's "
            f"pipeline output._"
        )
    if txn_context is not None and not txn_context.get("found"):
        return "\n\n_No matching transaction was found in this batch's results -- nothing to ground this in._"
    return (
        "\n\n_Grounded in: this batch's aggregate report (report.json) -- record/amount "
        "metrics, exception breakdown, and merchant risk ranking computed from the full "
        "reconciled results table._"
    )


def answer(message: str, report: dict, results_by_txn: dict, audit_by_txn: dict,
           history: list | None = None) -> dict:
    """Returns {"reply": str, "source": str, "transaction_id": str|None}."""
    txn_context = _find_transaction_context(message, results_by_txn, audit_by_txn)
    grounding = _grounding_note(txn_context)

    if llm_client.is_configured():
        try:
            context = {
                "report_summary": {
                    "record_level": report.get("record_level"),
                    "amount_level": report.get("amount_level"),
                    "accuracy": report.get("accuracy"),
                    "throughput": report.get("throughput"),
                    "top_exception_type": report.get("top_exception_type"),
                    "largest_exception_amount": report.get("largest_exception_amount"),
                    "highest_risk_merchant": report.get("highest_risk_merchant"),
                    "highest_risk_merchant_name": report.get("highest_risk_merchant_name"),
                    "exception_breakdown": report.get("exception_breakdown"),
                    "merchant_risk_ranking": report.get("merchant_risk_ranking"),
                },
                "transaction_lookup": txn_context,
            }
            import json
            user_prompt = f"User question: {message}\n\nContext:\n{json.dumps(context, indent=2, default=str)}"
            history_msgs = (history or []) + [{"role": "user", "content": user_prompt}]
            response = llm_client.chat_multiturn(SYSTEM_PROMPT, history_msgs, temperature=0.2, max_tokens=350)
            return {
                "reply": response.text.strip() + grounding,
                "source": f"AI ({response.label})",
                "transaction_id": txn_context["transaction_id"] if txn_context else None,
            }
        except Exception:
            pass  # fall through to rule-based; the failure itself is uninteresting to chat users

    reply = _rule_based_answer(message, report, txn_context)
    return {
        "reply": reply + grounding,
        "source": "Rule-based reasoning (AI unavailable)" if llm_client.is_configured() else "Rule-based reasoning",
        "transaction_id": txn_context["transaction_id"] if txn_context else None,
    }
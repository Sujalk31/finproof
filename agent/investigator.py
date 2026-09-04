"""
AI Investigation Agent (plan sections 12, 25-27).

Only rows flagged `needs_investigation=True` by the deterministic
discrepancy engine + cross-source checks reach this module -- this keeps
LLM calls, cost and hallucination risk to a minimum (plan section 7
"Level 4").

Design principle (section 25): the LLM is used ONLY to interpret evidence
and produce a natural-language explanation + a residual "unexplained
amount". It is NEVER trusted to do arithmetic -- every number the LLM
receives was already computed deterministically upstream, and the final
unexplained_amount is always recomputed in Python, not parsed from free
text, to prevent silent arithmetic drift.

Uses Groq's free hosted open-model API (agent/llm_client.py) when
GROQ_API_KEY is configured. Otherwise a transparent rule-based fallback
runs so the whole pipeline is fully runnable offline/without an API key --
this fallback follows the exact same "only claim what the evidence
supports" discipline as the LLM prompt.
"""

import json

from agent import llm_client

SYSTEM_PROMPT = """You are a financial reconciliation investigator working inside FinProof, \
an evidence-based AI finance controller. You will be given structured evidence about a \
payment/settlement/bank/ledger discrepancy that could NOT be fully explained by \
deterministic fee/tax/date/bank/ledger rules. Your job is ONLY to interpret the evidence \
and explain, in plain language, what is and is not accounted for.

Rules:
- Never invent numbers. Only use the numbers given to you in the evidence.
- If the evidence does not fully explain the gap, say so explicitly and report the \
remaining unexplained amount honestly -- do not round it away or rationalize it.
- Prefer the simplest explanation directly supported by the evidence over a speculative one.
- Respond ONLY with a JSON object, no prose outside it: \
{"explanation": string, "unexplained_amount": number, "fully_explained": boolean}
"""


def _rule_based_investigation(evidence: dict) -> dict:
    """Deterministic, honest fallback investigator -- no LLM required."""
    net_diff = evidence.get("net_diff")
    dtype = evidence.get("discrepancy_type")
    settlement = evidence.get("settlement", {})
    bank = evidence.get("bank", {})
    ledger = evidence.get("ledger", {})
    expected = evidence.get("expected", {})

    if dtype == "DATE_ANOMALY":
        days = evidence.get("date_diff_days")
        return {
            "explanation": (
                f"Amounts fully reconcile; settlement occurred {days} days after payment, "
                f"well beyond the normal processing window. No fee/tax irregularity found -- "
                f"this looks like an operational delay, not a financial discrepancy."
            ),
            "unexplained_amount": 0.0,
            "fully_explained": True,
        }

    if dtype == "PARTIAL_SETTLEMENT":
        expected_net = expected.get("expected_net")
        actual_net = settlement.get("net_amount")
        remainder = round(expected_net - actual_net, 2) if expected_net and actual_net is not None else net_diff
        return {
            "explanation": (
                f"Settlement of {actual_net} covers only part of the expected net "
                f"{expected_net}. No split-settlement or pending-installment record exists "
                f"in the available sources to account for the remaining {remainder}."
            ),
            "unexplained_amount": remainder,
            "fully_explained": False,
        }

    if dtype == "MISSING_BANK_CREDIT":
        return {
            "explanation": (
                f"The settlement recorded a net payout of {settlement.get('net_amount')}, but "
                f"no bank statement row references this settlement at all. Either the payout "
                f"is still in transit or it failed silently after settlement was booked."
            ),
            "unexplained_amount": settlement.get("net_amount") or net_diff,
            "fully_explained": False,
        }

    if dtype == "BANK_AMOUNT_MISMATCH":
        diff = round((bank.get("amount") or 0) - (settlement.get("net_amount") or 0), 2)
        return {
            "explanation": (
                f"Settlement booked {settlement.get('net_amount')} but the bank statement shows "
                f"{bank.get('amount')} actually credited -- a difference of {diff}. No fee, tax, "
                f"or ledger record explains this gap; the settlement and bank disagree."
            ),
            "unexplained_amount": diff,
            "fully_explained": False,
        }

    if dtype in ("LEDGER_MISMATCH", "LEDGER_RECORD_MISSING"):
        diff = None
        if ledger.get("credit") is not None and settlement.get("net_amount") is not None:
            diff = round(ledger.get("credit") - settlement.get("net_amount"), 2)
        if dtype == "LEDGER_RECORD_MISSING":
            ledger_clause = "has no entry for this transaction"
        else:
            ledger_clause = f"booked {ledger.get('credit')} instead"
        return {
            "explanation": (
                f"Settlement and bank agree on {settlement.get('net_amount')}, but the merchant "
                f"ledger {ledger_clause}. This points to an accounting/posting error rather than "
                f"a payment-side issue."
            ),
            "unexplained_amount": diff if diff is not None else net_diff,
            "fully_explained": False,
        }

    # UNKNOWN_MISMATCH / AMOUNT_MISMATCH: genuinely no deterministic explanation found upstream.
    fee = settlement.get("fee")
    tax = settlement.get("tax")
    exp_fee = expected.get("expected_fee")
    exp_tax = expected.get("expected_tax")
    fee_ok = fee is not None and exp_fee is not None and abs(fee - exp_fee) <= 1.0
    tax_ok = tax is not None and exp_tax is not None and abs(tax - exp_tax) <= 1.0

    if fee_ok and tax_ok:
        return {
            "explanation": (
                f"Fee ({fee}) and GST ({tax}) both match the configured schedule, so the "
                f"{net_diff} gap is not caused by fee or tax. No adjustment, chargeback, or "
                f"correction record is available in the connected sources to explain it. "
                f"Flagging as genuinely unexplained rather than guessing."
            ),
            "unexplained_amount": net_diff,
            "fully_explained": False,
        }

    return {
        "explanation": (
            f"Unable to fully attribute the {net_diff} gap using the available evidence "
            f"(payment, settlement, fee configuration, bank, ledger). No further supporting "
            f"records found."
        ),
        "unexplained_amount": net_diff,
        "fully_explained": False,
    }


def _llm_investigation(evidence: dict) -> dict:
    user_prompt = f"Evidence:\n{json.dumps(evidence, indent=2, default=str)}"
    response = llm_client.chat_completion(SYSTEM_PROMPT, user_prompt, temperature=0.1, max_tokens=400)
    parsed = llm_client.parse_json_response(response.text)

    # Never trust LLM arithmetic -- recompute unexplained_amount in Python
    # as a safety net in case the model's number and net_diff disagree.
    parsed["unexplained_amount"] = evidence.get("net_diff")
    parsed["source"] = f"AI Investigation ({response.label})"
    return parsed


def investigate(evidence: dict, use_llm: bool = True) -> dict:
    """Returns {"explanation", "unexplained_amount", "fully_explained", "source"}.

    `use_llm=False` skips the LLM call entirely (no network call attempted at
    all) and goes straight to the deterministic rule-based path -- used by
    the pipeline to cap how many exceptions get the (slower, per-call)
    LLM treatment on a large batch, e.g. only the first N.

    `source` is always a short, human-readable label safe to show directly
    in the UI/audit trail (e.g. "AI Investigation (NVIDIA NIM ...)" or
    "Rule-based reasoning (AI unavailable)") -- never a raw exception, model
    id, or HTTP error body. If every configured LLM provider fails, the
    underlying technical reason is still recoverable via `source_detail` for
    anyone debugging, but it's kept out of the primary label.
    """
    if use_llm and llm_client.is_configured():
        try:
            return _llm_investigation(evidence)
        except Exception as e:
            result = _rule_based_investigation(evidence)
            reason = e.reason if isinstance(e, llm_client.LLMUnavailable) else llm_client._classify_error(e)
            result["source"] = "Rule-based reasoning (AI investigation unavailable)"
            result["source_detail"] = reason
            return result

    result = _rule_based_investigation(evidence)
    if not use_llm and llm_client.is_configured():
        # Only genuinely "capped" if a provider exists and this row simply
        # didn't get its turn -- otherwise (no provider configured at all)
        # every row lands here regardless of the cap, and that's a different,
        # more basic reason worth stating plainly instead.
        result["source"] = "Rule-based reasoning (LLM investigation cap reached for this batch)"
    else:
        result["source"] = "Rule-based reasoning (no AI provider configured)"
    return result
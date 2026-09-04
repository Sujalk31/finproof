# FinProof — Evidence-Based AI Finance Controller

> **Don't just reconcile. Prove it.**

FinProof is an AI agent that reconciles financial records across six source
systems (payments, settlements, bank statements, merchant ledger, fee
configuration, tax records), investigates every discrepancy it finds using
cross-source evidence, and safely auto-resolves only the cases it can
*prove* — escalating everything else to a human with a clear, honest
explanation of what it does and doesn't know.

This build implements the full **Track 04 — AI Finance Controller** spec:
a FastAPI backend + browser dashboard, a Groq-hosted open-weight LLM for
investigation and chat, a ground-truth-evaluated batch pipeline (1,000+
synthetic transactions), an audit trail, and a chatbot for ad-hoc questions
about the results.

---

## 1. What's actually happening, end to end

```
                          DATA SOURCES (data/*.csv)
        ┌──────────┬──────────────┬────────┬────────┬──────────┐
        ▼          ▼              ▼        ▼        ▼          ▼
    payments   settlements      bank    ledger   fee_config    tax
        └──────────┴──────────────┴────┬───┴────────┴──────────┘
                                        ▼
                              1. NORMALIZE
                 (merchant names, dates, amounts, IDs -> one shape)
                                        ▼
                              2. MATCH (deterministic)
              exact transaction_id join, duplicate detection,
              fuzzy merchant-identity check, bank/ledger/tax joins
                                        ▼
                              3. DISCREPANCY DETECTION
               recompute expected fee/tax/net from fee_config and
               compare against what was actually settled (pure math,
               no LLM — see plan section 25)
                                        ▼
                              4. CROSS-SOURCE CHECKS
              if settlement math is clean, still verify: did the bank
              actually credit the money? does the ledger agree? was a
              tax record filed?
                                        ▼
                         needs_investigation? ──── no ──► confidence + route
                                        │ yes
                                        ▼
                              5. AI INVESTIGATION AGENT
                 Groq-hosted LLM interprets the evidence bundle and
                 explains the gap in plain language — but NEVER does the
                 arithmetic itself (Python already computed every number)
                                        ▼
                              6. CONFIDENCE ENGINE
                     0-100 score per transaction based on how completely
                     the evidence explains the conclusion
                                        ▼
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
              AUTO_RESOLVE          AI_REVIEW           HUMAN_REVIEW
               (≥ 95%)              (70–94%)              (< 70%)
                    └───────────────────┼───────────────────┘
                                        ▼
                          7. EVALUATE vs hidden ground truth
                          8. FINANCE CONTROLLER REPORT + AUDIT TRAIL
                                        ▼
                     FastAPI backend  ◄──────────────►  Dashboard + Chatbot
```

Every one of those eight stages is a real, separate, inspectable step in
[`pipeline.py`](pipeline.py) — nothing is hidden inside a single "call the
LLM and hope" function.

---

## 2. Why six data sources instead of two

Most reconciliation demos only compare a payment to a settlement. FinProof
also checks:

| Source | File | What it proves |
|---|---|---|
| **Payments** | `data/payments.csv` | What the customer paid |
| **Settlements** | `data/settlements.csv` | What the payment processor says it settled (gross, fee, tax, net) |
| **Bank statement** | `data/bank.csv` | Whether the money **actually arrived** in the bank account |
| **Merchant ledger** | `data/ledger.csv` | Whether accounting **booked the correct amount** |
| **Fee configuration** | `data/fee_config.csv` | The ground-truth formula for what the fee/tax *should* be |
| **Tax records** | `data/tax.csv` | Whether a compliance-grade tax record was filed at all |

A settlement can look perfectly reconciled against the payment and still be
wrong — the bank might never have received the money, or the ledger might
have booked a different figure. FinProof catches both, and reports *which
source* disagrees, not just that "something doesn't match."

---

## 3. Discrepancy types the engine detects

| Type | Detected by | Meaning |
|---|---|---|
| `MATCH` | discrepancy.py | Everything reconciles |
| `AMOUNT_MISMATCH` | discrepancy.py | Net settlement doesn't match amount − fee − tax, and no deterministic cause found |
| `MISSING_SETTLEMENT` | discrepancy.py | Payment exists, settlement never happened |
| `DUPLICATE` | matcher.py | Same transaction_id submitted twice |
| `FEE_MISMATCH` | discrepancy.py | Applied fee ≠ configured fee schedule |
| `TAX_MISMATCH` | discrepancy.py | Applied GST ≠ expected GST |
| `DATE_ANOMALY` | discrepancy.py | Settlement delayed beyond the normal window (amounts still reconcile) |
| `PARTIAL_SETTLEMENT` | discrepancy.py | Only part of the expected net was paid out |
| `MISSING_BANK_CREDIT` | cross_source.py | Settlement booked, but the bank statement shows nothing |
| `BANK_AMOUNT_MISMATCH` | cross_source.py | Bank credited a different amount than what was settled |
| `LEDGER_MISMATCH` | cross_source.py | Merchant ledger booked a different amount than the settlement |
| `LEDGER_RECORD_MISSING` | cross_source.py | No ledger entry exists for a settled transaction |
| `TAX_RECORD_MISSING` | cross_source.py | Settlement/bank/ledger all agree, but no tax record was filed (compliance gap) |
| `UNKNOWN_MISMATCH` | discrepancy.py | Net difference not explained by any rule above → sent to the AI investigator |

Legitimate noise that the engine correctly does **not** flag as an
exception:
- **Merchant identity noise** — "ABC Technologies Pvt Ltd" vs "ABC Tech
  Pvt. Ltd." vs "ABC TECHNOLOGIES" are recognized as the same entity via
  normalized fuzzy matching (`reconciliation/fuzzy_matcher.py`), so this
  never becomes a false exception.
- **Normal settlement delay** — a settlement landing 2–4 days after payment
  is within the configured normal window and stays `MATCH`.

---

## 4. Confidence & routing

Every decision gets a 0–100 confidence score (`agent/confidence.py`)
reflecting how completely the evidence explains the conclusion, then gets
routed (`agent/decision.py`):

| Confidence | Route | Meaning |
|---|---|---|
| ≥ 95% | `AUTO_RESOLVED` | Safe to resolve without a human |
| 70–94% | `AI_REVIEW` | AI has an explanation, but a human should skim it |
| < 70% | `HUMAN_REVIEW` | Evidence is genuinely insufficient — **the system says "I don't know" rather than guessing** |

This honest-failure behavior (plan section 15) is the whole point of the
project: a wrong auto-resolution in finance is far more expensive than an
escalation.

---

## 5. The AI Investigation Agent — and where the LLM is (and isn't) used

Only rows flagged `needs_investigation=True` reach the LLM — everything
that can be explained by deterministic math never touches it, which keeps
cost, latency, and hallucination risk down (plan section 7 "Level 4" /
section 25).

**The LLM is used for:** interpreting an evidence bundle it cannot fully
explain and writing a plain-language explanation, and for the chatbot.

**The LLM is never used for:** arithmetic, tax calculation, final totals,
or exact-ID matching. Every number the LLM sees was already computed in
Python by `reconciliation/discrepancy.py` / `reconciliation/cross_source.py`,
and `agent/investigator.py` **recomputes `unexplained_amount` in Python
after the LLM call** rather than trusting whatever number the model wrote —
so a hallucinated figure can never leak into the report.

### Groq integration

FinProof calls Groq's free hosted inference API
([console.groq.com](https://console.groq.com)), which exposes an
OpenAI-compatible `/openai/v1/chat/completions` endpoint. Default model:

```
llama-3.3-70b-versatile
```

Groq's free tier needs no credit card and works the moment you generate a
key — no separate account-activation step. (An earlier version of this
project used NVIDIA's build.nvidia.com NIM API instead; it was dropped
after hitting a currently-unresolved NVIDIA platform bug where valid keys
404 with `"Function ... Not found for account"` until NVIDIA support
manually enables a permission — see the NVIDIA Developer Forums, category
NVIDIA NIM > Access/Accounts, for many open reports of the same issue.)
You can swap models via the `GROQ_MODEL` env var (see `.env.example`) —
e.g. `llama-3.1-8b-instant` for a much higher free daily request cap at
slightly lower quality.

The client lives in `agent/llm_client.py` and is shared by the
investigator agent and the chatbot. **If `GROQ_API_KEY` is not set (or
a call fails for any reason, including hitting the free-tier daily/rate
limit), everything falls back to a transparent, deterministic rule-based
responder** — the whole system, including the chatbot, works fully offline
with zero API keys. This is by design: a finance controller must never go
down just because an LLM provider is unreachable.

---

## 6. The chatbot

A floating chat widget on the dashboard lets you ask about the *current
batch's results* — not a generic "ask anything" bot (the plan explicitly
calls that out as a weak, undifferentiated project on its own — section
28).

Examples:
- `"Why was TXN100482 escalated?"` → looks up that exact transaction's
  evidence + audit entry and explains the decision
- `"What's our top exception type?"` / `"highest risk merchant?"` /
  `"false resolution rate?"` → answered from the finance controller report
- Anything else → the model gets the full report summary as grounded
  context and is instructed to say plainly if the answer isn't in it,
  rather than guess

Implementation: `agent/chatbot.py`. It regex-detects a `TXN\d+` pattern in
the message to decide whether to ground the answer in one transaction's
evidence or in the aggregate report, then either calls the Groq LLM with
that context or, if no API key is configured, answers with a small
keyword-matching rule engine over the same JSON (`_rule_based_answer`) —
so the chatbot is genuinely usable with zero external dependencies.

---

## 7. Project structure

```
finproof/
├── backend/
│   └── main.py               # FastAPI app: all REST endpoints + serves the dashboard
├── frontend/
│   ├── index.html            # Dashboard shell
│   └── static/
│       ├── style.css
│       └── app.js            # Charts, transaction table, "Why?" modal, chat widget
├── generator/
│   └── generate_data.py      # Synthetic multi-source dataset + hidden ground truth
├── reconciliation/
│   ├── normalizer.py         # Merchant name / date / amount / ID normalization
│   ├── matcher.py            # Deterministic joins + duplicate + identity checks
│   ├── fuzzy_matcher.py      # difflib-based string similarity
│   ├── discrepancy.py        # Fee/tax/date/amount root-cause classification
│   └── cross_source.py       # Bank / ledger / tax cross-checks
├── agent/
│   ├── llm_client.py         # Groq client (OpenAI-compatible), shared
│   ├── investigator.py       # AI investigation agent (LLM + rule-based fallback)
│   ├── evidence.py           # Builds the evidence bundle used everywhere ("Why?")
│   ├── confidence.py         # 0–100 confidence scoring per discrepancy type
│   ├── decision.py           # Confidence -> AUTO_RESOLVED / AI_REVIEW / HUMAN_REVIEW
│   └── chatbot.py            # Grounded Q&A over the batch's results
├── evaluation/
│   ├── ground_truth.py       # Compares system decisions to hidden ground truth
│   └── metrics.py            # Record-level, amount-level metrics + report builder
├── database/
│   └── models.py             # SQLite persistence (decisions + audit log)
├── data/                      # Generated CSV sources (+ hidden ground_truth.csv)
├── output/                    # Pipeline outputs (report, audit trail, DB, CSVs)
├── pipeline.py                 # Orchestrates all 8 stages end-to-end
├── requirements.txt
├── .env.example
└── README.md                   # This file
```

---

## 8. Setup

### 8.1 Install

```bash
cd finproof
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 8.2 Configure the LLM (optional but recommended)

1. Go to [build.nvidia.com](https://build.nvidia.com), open any model page,
   click **Get API Key**.
2. Copy `.env.example` to `.env` and paste your key:

```bash
cp .env.example .env
# edit .env and set:
# GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxx
```

Without a key, everything still runs — the investigator and chatbot use
their deterministic rule-based fallbacks instead of the LLM.

### 8.3 Generate data + run the pipeline once from the CLI

```bash
python3 generator/generate_data.py --n 1000 --out data
python3 pipeline.py --data data --out output
```

This prints the full finance controller report to the terminal and writes
everything to `output/` (see section 10).

### 8.4 Start the backend + dashboard

```bash
uvicorn backend.main:app --reload --port 8000
```

Open **http://localhost:8000**. If you already ran the pipeline via the
CLI, the dashboard loads immediately. Otherwise click **Run pipeline** in
the top bar (it regenerates 1,000 transactions and reconciles them, with a
live progress indicator via `/api/pipeline/status`).

---

## 9. API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check + whether the Groq LLM is configured |
| POST | `/api/pipeline/run` | Kick off a pipeline run (`{"regenerate": bool, "n_records": int}`) |
| GET | `/api/pipeline/status` | Poll progress of the running job |
| GET | `/api/report` | Full finance controller report (metrics, accuracy, breakdowns) |
| GET | `/api/transactions` | Paginated/filterable/searchable transaction list |
| GET | `/api/transactions/{transaction_id}` | Full evidence bundle + audit entry for one transaction |
| GET | `/api/exceptions/breakdown` | Root-cause exception breakdown |
| GET | `/api/merchants/risk` | Highest-risk merchants |
| POST | `/api/chat` | Chatbot Q&A (`{"message": str, "history": [...]}`) |
| GET | `/` | Dashboard UI |

Interactive OpenAPI docs are auto-served at `/docs`.

---

## 10. Metrics explained (what `output/report.json` contains)

- **Record-level**: records processed, matched, exceptions, and the
  auto-resolved / AI-review / human-review split, plus `resolution_rate`
  (auto-resolved + AI-review) and `match_rate`.
- **Amount-level**: total, reconciled, and unresolved ₹ amounts —
  because a finance controller has to reason about money, not just row
  counts (plan section 20).
- **Accuracy vs ground truth**: accuracy, precision/recall/F1 on the
  `EXCEPTION` class, and a confusion matrix — computed by comparing final
  decisions to `data/ground_truth.csv`, a file the reconciliation engine
  itself never reads (plan section 10).
- **False resolution rate**: of everything the system `AUTO_RESOLVED`, what
  fraction was actually wrong against ground truth. This is the single
  most important safety metric for an autonomous finance controller (plan
  section 19) — a real run of this project measures **0.0%** at the
  default confidence thresholds.
- **Throughput**: measured wall-clock time and records/sec for the batch
  (never invented — plan section 21).
- **Exception breakdown & merchant risk ranking**: root-cause clustering
  (plan section 17) — e.g. "fee mismatches account for 11% of exceptions"
  or "merchant M003 accounts for the most exceptions."

All of this is also written to `output/report.txt` (human-readable),
`output/reconciled_results.csv`, `output/row_level_results.csv`,
`output/audit_trail.json` (every decision with its evidence, plan section
22), and `output/finproof.db` (SQLite).

### A real measured run (1,000 transactions, rule-based fallback, no API key)

```
Records processed:       1000
Matched:                  629      Exceptions: 371
  Auto-resolved: 779   AI review: 139   Human review: 82
Resolution rate:          91.8%

Amount processed:         Rs. 29,140,563.80
Amount reconciled:        Rs. 26,586,308.36 (91.2%)
Amount unresolved:        Rs. 2,554,255.44

Accuracy vs ground truth: 98.9%
Precision (exception):    97.0%
Recall (exception):       100.0%
False resolution rate:    0.00%

Throughput: 1000 records in 0.4s (~2,500 records/sec)
```

(Numbers are regenerated fresh every run — this is a real run of the code
in this repo, not an asserted figure.)

---

## 11. Tuning the confidence thresholds

`agent/confidence.py` defines:

```python
AUTO_RESOLVE_THRESHOLD = 95.0
AI_REVIEW_THRESHOLD = 70.0
```

These are starting points, not claimed-optimal values (plan section 14
explicitly warns against asserting thresholds without validation). To tune
them: run the pipeline, inspect `output/evaluation.json`'s
`false_resolution_rate` and `confusion_matrix`, adjust the thresholds, and
re-run — the ground-truth evaluation gives you an objective signal instead
of guessing.

---

## 12. Design principles this project follows

1. **Deterministic first, LLM last.** Exact ID matching → composite
   matching → fuzzy matching → LLM, in that order (plan section 7). The
   LLM only ever sees the minority of rows that survive all deterministic
   checks.
2. **The LLM never does arithmetic.** Every number it's shown was computed
   in Python; every number it outputs is discarded in favor of the
   Python-computed figure.
3. **Honest failure over confident guessing.** Below the confidence
   threshold, the system says "human review required" instead of forcing a
   decision (plan section 15).
4. **Everything is measured, nothing is claimed.** Accuracy, false
   resolution rate, and throughput are all computed from a real batch run
   against real ground truth, not asserted.
5. **Full audit trail.** Every decision, its confidence, its evidence, and
   which agent (rule-based or LLM) made it are logged (plan section 22).
6. **Works with zero API keys.** The rule-based fallback for both the
   investigator and the chatbot means the whole system is demoable
   offline; the Groq LLM makes the explanations richer, not the system
   dependent.

---

## 13. Extending this project

- Swap `database/models.py`'s SQLite for Postgres for multi-user/production use.
- Add a real adjustments/chargebacks source table so `UNKNOWN_MISMATCH`
  cases have one more deterministic layer to check before reaching the LLM.
- Add authentication + per-user batches to the FastAPI backend if this
  moves beyond a single-batch demo.
- Swap the vanilla-JS frontend for a React app if you want richer
  interactivity — the API is already fully decoupled from the UI.

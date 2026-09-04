# FinProof — Evidence-Based AI Finance Controller

> **Don't just reconcile. Prove it.**

FinProof is an AI agent that reconciles financial records across six source
systems (payments, settlements, bank statements, merchant ledger, fee
configuration, tax records), investigates every discrepancy it finds using
cross-source evidence, and safely auto-resolves only the cases it can
*prove* — escalating everything else to a human with a clear, honest
explanation of what it does and doesn't know.

This build implements the full **Track 04 — AI Finance Controller** spec:
a FastAPI backend + browser dashboard, an LLM-powered investigation and
chat agent, a ground-truth-evaluated batch pipeline (1,000+ synthetic
transactions), a full audit trail, and a chatbot for ad-hoc questions about
the results.

---

## Table of contents

1. [What's actually happening, end to end](#1-whats-actually-happening-end-to-end)
2. [The six data sources](#2-the-six-data-sources)
3. [Discrepancy types the engine detects](#3-discrepancy-types-the-engine-detects)
4. [Confidence & routing](#4-confidence--routing)
5. [The AI Investigation Agent — where the LLM is (and isn't) used](#5-the-ai-investigation-agent--where-the-llm-is-and-isnt-used)
6. [The chatbot](#6-the-chatbot)
7. [Project structure](#7-project-structure)
8. [Getting it from GitHub and running it](#8-getting-it-from-github-and-running-it)
9. [Configuring the LLM](#9-configuring-the-llm)
10. [API reference](#10-api-reference)
11. [Metrics explained](#11-metrics-explained-what-outputreportjson-contains)
12. [Tuning the confidence thresholds](#12-tuning-the-confidence-thresholds)
13. [Design principles this project follows](#13-design-principles-this-project-follows)
14. [Extending this project](#14-extending-this-project)

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
               no LLM involved)
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
                 An LLM interprets the evidence bundle and explains the
                 gap in plain language — but NEVER does the arithmetic
                 itself (Python already computed every number)
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

## 2. The six data sources

FinProof reconciles across six independent sources, so it can point to
*which* source disagrees rather than just reporting "something doesn't
match":

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
have booked a different figure. FinProof catches both, and reports *which*
source disagrees.

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

This honest-failure behavior is the whole point of the project: a wrong
auto-resolution in finance is far more expensive than an escalation.

---

## 5. The AI Investigation Agent — where the LLM is (and isn't) used

Only rows flagged `needs_investigation=True` reach the LLM — everything
that can be explained by deterministic math never touches it, which keeps
cost, latency, and hallucination risk down.

**The LLM is used for:** interpreting an evidence bundle it cannot fully
explain and writing a plain-language explanation, and for the chatbot.

**The LLM is never used for:** arithmetic, tax calculation, final totals,
or exact-ID matching. Every number the LLM sees was already computed in
Python by `reconciliation/discrepancy.py` / `reconciliation/cross_source.py`,
and `agent/investigator.py` **recomputes `unexplained_amount` in Python
after the LLM call** rather than trusting whatever number the model wrote —
so a hallucinated figure can never leak into the report.

### How the LLM client works (`agent/llm_client.py`)

FinProof calls out to a small, ordered chain of **OpenAI-compatible**
hosted-inference providers, and automatically falls through to the next
one if a call fails for any reason (bad model name, account not
provisioned, rate limit, network error, etc.):

1. **NVIDIA NIM** ([build.nvidia.com](https://build.nvidia.com)) — primary provider
2. **Groq** ([console.groq.com](https://console.groq.com)) — secondary fallback provider
3. **Deterministic rule-based logic** — final fallback, handled by the
   caller (`agent/investigator.py` / `agent/chatbot.py`), not by the LLM
   client itself

You can configure either provider alone, both, or neither. If **no**
provider is configured (or every configured provider fails, including
hitting a free-tier rate limit), everything falls back to a transparent,
rule-based responder — **the whole system, including the chatbot, works
fully offline with zero API keys.** This is by design: a finance
controller must never go down just because an LLM provider is
unreachable.

Raw provider errors (HTTP codes, internal model IDs, JSON error bodies)
are never surfaced directly to the user — `llm_client.py` classifies them
into a short, stable, human-readable reason (e.g. "rate limited (too many
requests)", "authentication failed (check the API key)") that's safe to
show in the UI and log to the audit trail.

See [section 9](#9-configuring-the-llm) for exactly how to set this up.

---

## 6. The chatbot

A floating chat widget on the dashboard lets you ask about the *current
batch's results* — not a generic "ask anything" bot.

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
evidence or in the aggregate report, then either calls the LLM (via
`agent/llm_client.py`) with that context or, if no provider is configured,
answers with a small keyword-matching rule engine over the same JSON
(`_rule_based_answer`) — so the chatbot is genuinely usable with zero
external dependencies.

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
│   ├── llm_client.py         # Multi-provider LLM client (NVIDIA -> Groq -> caller fallback)
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

## 8. Getting it from GitHub and running it

### 8.1 Clone the repo

```bash
git clone https://github.com/<your-username>/finproof.git
cd finproof
```

(Replace `<your-username>/finproof` with the actual GitHub path you
uploaded this project to.)

### 8.2 Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 8.3 (Optional but recommended) Configure the LLM

FinProof runs perfectly well with **zero configuration** — see
[section 9](#9-configuring-the-llm) if you want richer, LLM-generated
explanations instead of the deterministic rule-based fallback.

### 8.4 Generate data + run the pipeline once from the CLI

```bash
python3 generator/generate_data.py --n 1000 --out data
python3 pipeline.py --data data --out output
```

This prints the full finance controller report to the terminal and writes
everything to `output/` (see [section 11](#11-metrics-explained-what-outputreportjson-contains)).

### 8.5 Start the backend + dashboard

```bash
uvicorn backend.main:app --reload --port 8000
```

Open **http://localhost:8000** in your browser. If you already ran the
pipeline via the CLI, the dashboard loads immediately. Otherwise click
**Run pipeline** in the top bar (it regenerates 1,000 transactions and
reconciles them, with a live progress indicator via
`/api/pipeline/status`).

---

## 9. Configuring the LLM

This step is optional — without it, the investigator and chatbot simply
use their deterministic rule-based fallbacks instead of an LLM, and the
rest of the system behaves identically.

### 9.1 Copy the example environment file

```bash
cp .env.example .env
```

### 9.2 Pick a provider (or both)

FinProof tries **NVIDIA NIM first, then Groq**, then falls back to
rule-based logic. You only need to fill in the provider(s) you actually
want to use.

**Option A — Groq (fastest to set up, free, no credit card required)**

1. Go to [console.groq.com/keys](https://console.groq.com/keys) and
   generate a free API key.
2. Add it to your `.env`:

```bash
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxx
GROQ_MODEL=openai/gpt-oss-120b
GROQ_BASE_URL=https://api.groq.com/openai/v1
```

`GROQ_MODEL` defaults to `openai/gpt-oss-120b` if you leave it unset —
that's the value already shown above and in `.env.example`. You can swap
in any other chat model available on your Groq account (e.g.
`llama-3.1-8b-instant` for a higher free daily request cap at slightly
lower quality).

**Option B — NVIDIA NIM (tried first if configured)**

1. Go to [build.nvidia.com](https://build.nvidia.com), open any model
   page, and click **Get API Key**.
2. Add it to your `.env`:

```bash
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxx
NVIDIA_MODEL=nvidia/llama-3.1-nemotron-70b-instruct
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
```

**Using both:** if both `NVIDIA_API_KEY` and `GROQ_API_KEY` are set,
every LLM call tries NVIDIA first and automatically falls through to Groq
if the NVIDIA call fails for any reason (bad model name, account not yet
provisioned, rate limit, network error, etc.) — you get an extra layer of
resilience for free.

**Using neither:** leave both keys blank (or delete `.env` entirely) and
the investigator/chatbot use their rule-based fallback paths. Nothing else
about the system changes — the pipeline, dashboard, and API all work the
same way.

### 9.3 Other environment variables

```bash
# Where the FastAPI backend looks for data / writes output (defaults shown)
FINPROOF_DATA_DIR=data
FINPROOF_OUT_DIR=output
FINPROOF_MAX_LLM_INVESTIGATIONS=50
```

`FINPROOF_MAX_LLM_INVESTIGATIONS` caps how many rows in a single batch are
allowed to call the LLM, so a single run can never blow past a free-tier
rate limit or run away in cost — everything past the cap is handled by the
rule-based fallback instead.

---

## 10. API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check + whether an LLM provider is configured |
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

## 11. Metrics explained (what `output/report.json` contains)

- **Record-level**: records processed, matched, exceptions, and the
  auto-resolved / AI-review / human-review split, plus `resolution_rate`
  (auto-resolved + AI-review) and `match_rate`.
- **Amount-level**: total, reconciled, and unresolved ₹ amounts —
  because a finance controller has to reason about money, not just row
  counts.
- **Accuracy vs ground truth**: accuracy, precision/recall/F1 on the
  `EXCEPTION` class, and a confusion matrix — computed by comparing final
  decisions to `data/ground_truth.csv`, a file the reconciliation engine
  itself never reads.
- **False resolution rate**: of everything the system `AUTO_RESOLVED`, what
  fraction was actually wrong against ground truth. This is the single
  most important safety metric for an autonomous finance controller — a
  real run of this project measures **0.0%** at the default confidence
  thresholds.
- **Throughput**: measured wall-clock time and records/sec for the batch
  (never invented).
- **Exception breakdown & merchant risk ranking**: root-cause clustering —
  e.g. "fee mismatches account for 11% of exceptions" or "merchant M003
  accounts for the most exceptions."

All of this is also written to `output/report.txt` (human-readable),
`output/reconciled_results.csv`, `output/row_level_results.csv`,
`output/audit_trail.json` (every decision with its evidence), and
`output/finproof.db` (SQLite).

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

## 12. Tuning the confidence thresholds

`agent/confidence.py` defines:

```python
AUTO_RESOLVE_THRESHOLD = 95.0
AI_REVIEW_THRESHOLD = 70.0
```

These are starting points, not claimed-optimal values. To tune them: run
the pipeline, inspect `output/evaluation.json`'s `false_resolution_rate`
and `confusion_matrix`, adjust the thresholds, and re-run — the
ground-truth evaluation gives you an objective signal instead of guessing.

---

## 13. Design principles this project follows

1. **Deterministic first, LLM last.** Exact ID matching → composite
   matching → fuzzy matching → LLM, in that order. The LLM only ever sees
   the minority of rows that survive all deterministic checks.
2. **The LLM never does arithmetic.** Every number it's shown was computed
   in Python; every number it outputs is discarded in favor of the
   Python-computed figure.
3. **Honest failure over confident guessing.** Below the confidence
   threshold, the system says "human review required" instead of forcing a
   decision.
4. **Everything is measured, nothing is claimed.** Accuracy, false
   resolution rate, and throughput are all computed from a real batch run
   against real ground truth, not asserted.
5. **Full audit trail.** Every decision, its confidence, its evidence, and
   which agent (rule-based or LLM) made it are logged.
6. **Works with zero API keys.** The rule-based fallback for both the
   investigator and the chatbot means the whole system is demoable
   offline; an LLM provider makes the explanations richer, not the system
   dependent on it.

---

## 14. Extending this project

- Swap `database/models.py`'s SQLite for Postgres for multi-user/production use.
- Add a real adjustments/chargebacks source table so `UNKNOWN_MISMATCH`
  cases have one more deterministic layer to check before reaching the LLM.
- Add authentication + per-user batches to the FastAPI backend if this
  moves beyond a single-batch demo.
- Swap the vanilla-JS frontend for a React app if you want richer
  interactivity — the API is already fully decoupled from the UI.
"""
FinProof FastAPI backend.

Run:
    uvicorn backend.main:app --reload --port 8000

Then open http://localhost:8000 for the dashboard.

Endpoints
---------
GET  /api/health                       - liveness + whether the Groq LLM is configured
POST /api/pipeline/run                 - (re)generate data (optional) + run the pipeline
GET  /api/pipeline/status              - progress of the currently running pipeline job
GET  /api/report                       - full finance controller report (metrics, accuracy, breakdowns)
GET  /api/transactions                 - paginated, filterable, searchable transaction list
GET  /api/transactions/{transaction_id}- full evidence bundle + audit entry for one transaction ("Why?")
GET  /api/exceptions/breakdown         - root-cause exception breakdown
GET  /api/merchants/risk               - highest-risk merchants
POST /api/chat                         - chatbot Q&A over the current batch's results
GET  /                                 - dashboard UI (static frontend)
"""

import json
import os
import sys
import threading
import time
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)  # so `import pipeline`, `agent`, etc. resolve when run from anywhere

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
except ImportError:
    pass

import pipeline as pipeline_mod  # noqa: E402
from agent import chatbot, llm_client  # noqa: E402
from generator import generate_data  # noqa: E402
from json_safe import json_safe  # noqa: E402

DATA_DIR = os.path.join(ROOT_DIR, os.environ.get("FINPROOF_DATA_DIR", "data"))
OUT_DIR = os.path.join(ROOT_DIR, os.environ.get("FINPROOF_OUT_DIR", "output"))
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")

app = FastAPI(title="FinProof — Evidence-Based AI Finance Controller", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------------
# In-memory state (single-batch demo scope -- swap for a real DB-backed job
# queue if you extend this to multiple concurrent users/batches)
# ----------------------------------------------------------------------------
_state_lock = threading.Lock()
_pipeline_status = {"running": False, "stage": None, "pct": 0, "error": None}
_cache = {"report": None, "results_df": None, "audit_by_txn": None, "loaded_at": None}


def _progress_cb(stage, pct):
    with _state_lock:
        _pipeline_status["stage"] = stage
        _pipeline_status["pct"] = pct


def _load_outputs_into_cache():
    report_path = os.path.join(OUT_DIR, "report.json")
    results_path = os.path.join(OUT_DIR, "reconciled_results.csv")
    audit_path = os.path.join(OUT_DIR, "audit_trail.json")

    if not (os.path.exists(report_path) and os.path.exists(results_path) and os.path.exists(audit_path)):
        return False

    with open(report_path) as f:
        report = json.load(f)
    results_df = pd.read_csv(results_path)
    with open(audit_path) as f:
        audit_entries = json.load(f)
    audit_by_txn = {a["transaction_id"]: a for a in audit_entries}

    # Defensive: sanitize even if report.json/audit_trail.json on disk were
    # written by an older version of the pipeline before this fix (NaN from
    # missing settlement/bank/tax rows is otherwise invalid JSON for the browser).
    _cache["report"] = json_safe(report)
    _cache["results_df"] = results_df
    _cache["audit_by_txn"] = json_safe(audit_by_txn)
    _cache["loaded_at"] = time.time()
    return True


def _ensure_loaded():
    if _cache["report"] is None:
        if not _load_outputs_into_cache():
            raise HTTPException(
                status_code=404,
                detail="No pipeline output found yet. POST /api/pipeline/run first.",
            )


def _run_pipeline_job(n_records: Optional[int], regenerate: bool):
    # `running` is already set to True by the /api/pipeline/run handler itself
    # (synchronously, before this thread even starts) to avoid a first-poll race.
    try:
        if regenerate:
            generate_data.generate(n_records=n_records or 1000, out_dir=DATA_DIR)
        pipeline_mod.run_pipeline(data_dir=DATA_DIR, out_dir=OUT_DIR, progress_cb=_progress_cb)
        _load_outputs_into_cache()
    except Exception as e:
        with _state_lock:
            _pipeline_status["error"] = str(e)
    finally:
        with _state_lock:
            _pipeline_status["running"] = False


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------
class PipelineRunRequest(BaseModel):
    regenerate: bool = False
    n_records: int = 1000


class ChatRequest(BaseModel):
    message: str
    history: Optional[list] = None


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "llm_configured": llm_client.is_configured(),
        "llm_model": llm_client.DEFAULT_MODEL if llm_client.is_configured() else None,
        "output_available": os.path.exists(os.path.join(OUT_DIR, "report.json")),
    }


@app.post("/api/pipeline/run")
def run_pipeline(req: PipelineRunRequest):
    with _state_lock:
        if _pipeline_status["running"]:
            raise HTTPException(status_code=409, detail="A pipeline run is already in progress.")
        # Flip this HERE, synchronously, before the response is sent — not inside
        # the background thread. Otherwise the frontend's very first status poll
        # can land before the new thread gets scheduled, still see the stale
        # `running: false` from the last run (or the initial default), and give
        # up waiting immediately even though a run just started.
        _pipeline_status.update({"running": True, "stage": "starting", "pct": 0, "error": None})
    thread = threading.Thread(target=_run_pipeline_job, args=(req.n_records, req.regenerate), daemon=True)
    thread.start()
    return {"started": True}


@app.get("/api/pipeline/status")
def pipeline_status():
    with _state_lock:
        return dict(_pipeline_status)


@app.get("/api/report")
def get_report():
    _ensure_loaded()
    return _cache["report"]


@app.get("/api/transactions")
def list_transactions(
    status: Optional[str] = None,
    discrepancy_type: Optional[str] = None,
    system_label: Optional[str] = None,
    merchant_id: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
):
    _ensure_loaded()
    df = _cache["results_df"].copy()

    if status:
        df = df[df["status"] == status]
    if discrepancy_type:
        df = df[df["discrepancy_type"] == discrepancy_type]
    if system_label:
        df = df[df["system_label"] == system_label]
    if merchant_id:
        df = df[df["merchant_id"] == merchant_id]
    if search:
        df = df[df["transaction_id"].str.contains(search, case=False, na=False)]

    total = len(df)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    start = (page - 1) * page_size
    page_df = df.iloc[start:start + page_size]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": json_safe(page_df.to_dict("records")),
    }


@app.get("/api/transactions/{transaction_id}")
def get_transaction(transaction_id: str):
    _ensure_loaded()
    df = _cache["results_df"]
    row = df[df["transaction_id"].str.upper() == transaction_id.upper()]
    if row.empty:
        raise HTTPException(status_code=404, detail=f"{transaction_id} not found in this batch.")

    audit = _cache["audit_by_txn"].get(transaction_id.upper())
    if not audit:
        # audit is keyed by exact-case transaction_id; fall back to a scan
        audit = next(
            (a for a in _cache["audit_by_txn"].values()
             if a["transaction_id"].upper() == transaction_id.upper()),
            None,
        )

    return {"decision": json_safe(row.iloc[0].to_dict()), "audit": json_safe(audit)}


@app.get("/api/exceptions/breakdown")
def exceptions_breakdown():
    _ensure_loaded()
    return _cache["report"].get("exception_breakdown", [])


@app.get("/api/merchants/risk")
def merchants_risk():
    _ensure_loaded()
    return _cache["report"].get("merchant_risk_ranking", [])


@app.post("/api/chat")
def chat(req: ChatRequest):
    _ensure_loaded()
    results_by_txn = {
        row["transaction_id"]: row for row in json_safe(_cache["results_df"].to_dict("records"))
    }
    result = chatbot.answer(
        message=req.message,
        report=_cache["report"],
        results_by_txn=results_by_txn,
        audit_by_txn=_cache["audit_by_txn"],
        history=req.history,
    )
    return json_safe(result)


# ----------------------------------------------------------------------------
# Static frontend
# ----------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# Auto-load cached output on startup if the pipeline has already been run before.
@app.on_event("startup")
def _startup():
    _load_outputs_into_cache()

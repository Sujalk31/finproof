"""
Lightweight SQLite persistence (plan section 24: "SQLite can be sufficient").
Stdlib sqlite3 only — no extra dependency required.
"""

import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    transaction_id TEXT PRIMARY KEY,
    merchant_id TEXT,
    amount REAL,
    discrepancy_type TEXT,
    system_label TEXT,
    status TEXT,
    confidence REAL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    transaction_id TEXT,
    decision TEXT,
    reason TEXT,
    confidence REAL,
    evidence_json TEXT,
    agent TEXT
);
"""


def get_connection(db_path: str = "output/finproof.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def save_decisions(conn: sqlite3.Connection, final_decisions_records: list):
    cur = conn.cursor()
    cur.execute("DELETE FROM decisions")
    cur.executemany(
        """INSERT INTO decisions
           (transaction_id, merchant_id, amount, discrepancy_type, system_label, status, confidence, reason)
           VALUES (:transaction_id, :merchant_id, :amount, :discrepancy_type, :system_label, :status, :confidence, :reason)""",
        final_decisions_records,
    )
    conn.commit()


def save_audit_log(conn: sqlite3.Connection, audit_entries: list):
    cur = conn.cursor()
    cur.execute("DELETE FROM audit_log")
    cur.executemany(
        """INSERT INTO audit_log (timestamp, transaction_id, decision, reason, confidence, evidence_json, agent)
           VALUES (:timestamp, :transaction_id, :decision, :reason, :confidence, :evidence_json, :agent)""",
        [
            {**e, "evidence_json": json.dumps(e.get("evidence", {}), default=str)}
            for e in audit_entries
        ],
    )
    conn.commit()

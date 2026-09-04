"""
FinProof synthetic data generator (v2).

Strategy (per plan section 10-11):
1. Generate CLEAN, internally-consistent transactions where the correct
   settlement math is known (amount, fee, tax, net).
2. Record the ground truth (what SHOULD happen) in a hidden file that the
   reconciliation engine never sees.
3. Deliberately corrupt a controlled subset of records with realistic
   discrepancy types.
4. Feed only the corrupted, multi-source data to the reconciliation engine
   and evaluate its output against the hidden ground truth.

Sources produced (plan section 9 -- all six systems):
    data/payments.csv       - payment transactions
    data/settlements.csv    - settlement records
    data/bank.csv           - bank statement (money actually received)
    data/ledger.csv         - merchant ledger (accounting journal entries)
    data/fee_config.csv     - fee % / fixed fee per merchant, time-boxed
    data/tax.csv            - per-transaction tax records
    data/ground_truth.csv   - HIDDEN evaluation file (not used by the engine)
"""

import argparse
import csv
import os
import random
from datetime import datetime, timedelta

random.seed(42)

MERCHANTS = [
    ("M001", "ABC Technologies Pvt Ltd"),
    ("M002", "Bluewave Retail Pvt Ltd"),
    ("M003", "Nimbus Foods LLP"),
    ("M004", "Craftline Traders Pvt Ltd"),
    ("M005", "Orbit Mobility Pvt Ltd"),
    ("M006", "Sundrop Organics Pvt Ltd"),
    ("M007", "Velocity Logistics Pvt Ltd"),
    ("M008", "Harbor Analytics Pvt Ltd"),
]

NAME_VARIANTS = {
    "ABC Technologies Pvt Ltd": ["ABC Tech Pvt. Ltd.", "ABC TECHNOLOGIES", "Abc Technologies P Ltd"],
    "Bluewave Retail Pvt Ltd": ["Bluewave Retail Pvt. Ltd.", "BLUEWAVE RETAIL", "Blue Wave Retail Pvt Ltd"],
    "Nimbus Foods LLP": ["Nimbus Foods L.L.P.", "NIMBUS FOODS", "Nimbus Food LLP"],
    "Craftline Traders Pvt Ltd": ["Craftline Traders Pvt. Ltd.", "CRAFTLINE TRADERS"],
    "Orbit Mobility Pvt Ltd": ["Orbit Mobility Pvt. Ltd.", "ORBIT MOBILITY"],
    "Sundrop Organics Pvt Ltd": ["Sundrop Organics Pvt. Ltd.", "SUNDROP ORGANICS"],
    "Velocity Logistics Pvt Ltd": ["Velocity Logistics Pvt. Ltd.", "VELOCITY LOGISTICS"],
    "Harbor Analytics Pvt Ltd": ["Harbor Analytics Pvt. Ltd.", "HARBOR ANALYTICS"],
}

PAYMENT_METHODS = ["UPI", "CARD", "NETBANKING", "WALLET"]

FEE_PCT_BY_MERCHANT = {m[0]: round(random.uniform(1.5, 2.5), 2) for m in MERCHANTS}
FIXED_FEE_BY_MERCHANT = {m[0]: random.choice([0, 2, 5]) for m in MERCHANTS}
GST_RATE = 18.0

BASE_DATE = datetime(2026, 8, 1)


def _rand_amount():
    return round(random.uniform(500, 60000), 2)


def _txn_id(i):
    return f"TXN{100000 + i}"


def compute_fee_and_tax(merchant_id, amount):
    fee_pct = FEE_PCT_BY_MERCHANT[merchant_id]
    fixed_fee = FIXED_FEE_BY_MERCHANT[merchant_id]
    fee = round(amount * fee_pct / 100 + fixed_fee, 2)
    tax = round(fee * GST_RATE / 100, 2)
    net = round(amount - fee - tax, 2)
    return fee, tax, net


def generate(n_records=1000, out_dir="data"):
    os.makedirs(out_dir, exist_ok=True)

    payments, settlements, bank_rows, ledger_rows, tax_rows, ground_truth = [], [], [], [], [], []

    budget = {
        "AMOUNT_MISMATCH": 0.07,
        "MISSING_SETTLEMENT": 0.04,
        "DUPLICATE": 0.03,
        "FEE_MISMATCH": 0.04,
        "TAX_MISMATCH": 0.03,
        "DATE_ANOMALY_NORMAL": 0.03,
        "DATE_ANOMALY_BAD": 0.02,
        "IDENTITY_NOISE": 0.05,
        "PARTIAL_SETTLEMENT": 0.02,
        "MISSING_BANK_CREDIT": 0.03,
        "BANK_AMOUNT_MISMATCH": 0.03,
        "LEDGER_MISMATCH": 0.03,
        "TAX_RECORD_MISSING": 0.02,
    }

    all_indices = list(range(n_records))
    random.shuffle(all_indices)
    cursor = 0
    idx_lookup = {}
    counts = {}
    for label, pct in budget.items():
        k = int(n_records * pct)
        counts[label] = k
        chunk = all_indices[cursor:cursor + k]
        cursor += k
        for i in chunk:
            idx_lookup[i] = label

    for i in range(n_records):
        merchant_id, merchant_name = random.choice(MERCHANTS)
        amount = _rand_amount()
        txn_id = _txn_id(i)
        pay_date = BASE_DATE + timedelta(days=random.randint(0, 20), hours=random.randint(0, 23))
        fee, tax, true_net = compute_fee_and_tax(merchant_id, amount)

        error_type = idx_lookup.get(i, "CLEAN")

        pay_merchant_name = merchant_name
        settle_date = pay_date + timedelta(days=1)
        settle_amount = amount
        settle_fee = fee
        settle_tax = tax
        settle_net = true_net
        skip_settlement = False
        duplicate_row = False
        skip_bank = False
        bank_amount = None
        ledger_credit = None
        skip_tax_record = False

        gt_label = "MATCH"
        gt_expected_net = true_net
        gt_reason = "Clean transaction, no discrepancy."

        if error_type == "AMOUNT_MISMATCH":
            drift = round(random.uniform(50, 500) * random.choice([-1, 1]), 2)
            settle_net = round(true_net + drift, 2)
            gt_label = "EXCEPTION"
            gt_reason = f"Unexplained settlement drift of {drift} not attributable to fee/tax."

        elif error_type == "MISSING_SETTLEMENT":
            skip_settlement = True
            gt_label = "EXCEPTION"
            gt_reason = "Settlement record missing entirely."

        elif error_type == "DUPLICATE":
            duplicate_row = True
            gt_label = "EXCEPTION"
            gt_reason = "Duplicate payment record for same transaction."

        elif error_type == "FEE_MISMATCH":
            wrong_fee = round(fee + random.uniform(80, 300), 2)
            settle_fee = wrong_fee
            settle_tax = round(wrong_fee * GST_RATE / 100, 2)
            settle_net = round(amount - settle_fee - settle_tax, 2)
            gt_label = "EXCEPTION"
            gt_reason = f"Applied fee {settle_fee} does not match configured fee schedule ({fee})."

        elif error_type == "TAX_MISMATCH":
            wrong_tax = round(tax - random.uniform(20, 90), 2)
            settle_tax = max(wrong_tax, 0)
            settle_net = round(amount - fee - settle_tax, 2)
            gt_label = "EXCEPTION"
            gt_reason = f"Applied GST {settle_tax} does not match expected GST ({tax})."

        elif error_type == "DATE_ANOMALY_NORMAL":
            settle_date = pay_date + timedelta(days=random.choice([2, 3, 4, 6]))
            gt_label = "MATCH"
            gt_reason = "Settlement delayed by a few days; within normal processing window."

        elif error_type == "DATE_ANOMALY_BAD":
            settle_date = pay_date + timedelta(days=random.choice([6, 7, 8, 15, 25]))
            gt_label = "EXCEPTION"
            gt_reason = "Settlement delayed far beyond normal processing window; requires review."

        elif error_type == "IDENTITY_NOISE":
            pay_merchant_name = random.choice(NAME_VARIANTS[merchant_name])
            gt_label = "MATCH"
            gt_reason = "Merchant name differs only by formatting/legal-suffix noise; same entity."

        elif error_type == "PARTIAL_SETTLEMENT":
            settle_net = round(true_net * random.uniform(0.5, 0.75), 2)
            gt_label = "EXCEPTION"
            gt_reason = "Only part of the settlement amount was paid out; remainder unexplained."

        elif error_type == "MISSING_BANK_CREDIT":
            skip_bank = True
            gt_label = "EXCEPTION"
            gt_reason = "Settlement was booked but no corresponding bank credit was ever received."

        elif error_type == "BANK_AMOUNT_MISMATCH":
            bank_drift = round(random.uniform(50, 400) * random.choice([-1, 1]), 2)
            bank_amount = round(settle_net + bank_drift, 2)
            gt_label = "EXCEPTION"
            gt_reason = f"Bank credited {bank_amount} instead of the settled net {settle_net}."

        elif error_type == "LEDGER_MISMATCH":
            ledger_drift = round(random.uniform(50, 400) * random.choice([-1, 1]), 2)
            ledger_credit = round(settle_net + ledger_drift, 2)
            gt_label = "EXCEPTION"
            gt_reason = f"Merchant ledger booked {ledger_credit} instead of the settled net {settle_net}."

        elif error_type == "TAX_RECORD_MISSING":
            skip_tax_record = True
            gt_label = "EXCEPTION"
            gt_reason = "No separate tax record was filed for this transaction (compliance gap)."

        if bank_amount is None and not skip_bank:
            bank_amount = settle_net
        if ledger_credit is None:
            ledger_credit = settle_net

        payments.append({
            "transaction_id": txn_id,
            "merchant_id": merchant_id,
            "merchant_name": pay_merchant_name,
            "customer_id": f"CUST{random.randint(10000,99999)}",
            "amount": amount,
            "timestamp": pay_date.strftime("%Y-%m-%d %H:%M:%S"),
            "payment_method": random.choice(PAYMENT_METHODS),
            "status": "SUCCESS",
        })

        if duplicate_row:
            payments.append(dict(payments[-1]))

        settlement_id = f"SETL{200000+i}"
        if not skip_settlement:
            settlements.append({
                "settlement_id": settlement_id,
                "transaction_id": txn_id,
                "gross_amount": settle_amount,
                "fee": settle_fee,
                "tax": settle_tax,
                "net_amount": settle_net,
                "settlement_date": settle_date.strftime("%Y-%m-%d"),
                "status": "SETTLED",
            })

            if not skip_bank:
                bank_rows.append({
                    "bank_reference": f"BANKREF{300000+i}",
                    "settlement_id": settlement_id,
                    "amount": bank_amount,
                    "transaction_date": (settle_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                    "bank_status": "CREDITED",
                })

            ledger_rows.append({
                "journal_id": f"JNL{400000+i}",
                "merchant_id": merchant_id,
                "transaction_id": txn_id,
                "debit": 0.0,
                "credit": ledger_credit,
                "account": "Merchant Payable",
                "date": settle_date.strftime("%Y-%m-%d"),
            })

            if not skip_tax_record:
                tax_rows.append({
                    "transaction_id": txn_id,
                    "tax_type": "GST",
                    "tax_rate": GST_RATE,
                    "tax_amount": settle_tax,
                })

        ground_truth.append({
            "transaction_id": txn_id,
            "injected_error_type": error_type,
            "true_label": gt_label,
            "true_expected_net": gt_expected_net,
            "true_explanation": gt_reason,
        })

    fee_config = []
    for m_id, m_name in MERCHANTS:
        fee_config.append({
            "merchant_id": m_id,
            "fee_percentage": FEE_PCT_BY_MERCHANT[m_id],
            "fixed_fee": FIXED_FEE_BY_MERCHANT[m_id],
            "tax_rate": GST_RATE,
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        })

    _write_csv(os.path.join(out_dir, "payments.csv"), payments)
    _write_csv(os.path.join(out_dir, "settlements.csv"), settlements)
    _write_csv(os.path.join(out_dir, "bank.csv"), bank_rows)
    _write_csv(os.path.join(out_dir, "ledger.csv"), ledger_rows)
    _write_csv(os.path.join(out_dir, "tax.csv"), tax_rows)
    _write_csv(os.path.join(out_dir, "fee_config.csv"), fee_config)
    _write_csv(os.path.join(out_dir, "ground_truth.csv"), ground_truth)

    total_injected = sum(counts.values())
    print(f"Generated {len(payments)} payment rows ({n_records} unique transactions incl. "
          f"{counts['DUPLICATE']} injected duplicates), {len(settlements)} settlement rows, "
          f"{len(bank_rows)} bank rows, {len(ledger_rows)} ledger rows, {len(tax_rows)} tax rows.")
    print(f"Injected discrepancies: {total_injected} / {n_records} transactions "
          f"({total_injected/n_records:.1%})")
    print("Breakdown:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"Files written to: {os.path.abspath(out_dir)}")


def _write_csv(path, rows):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate FinProof synthetic dataset")
    parser.add_argument("--n", type=int, default=1000, help="number of transactions")
    parser.add_argument("--out", type=str, default="data", help="output directory")
    args = parser.parse_args()
    generate(n_records=args.n, out_dir=args.out)

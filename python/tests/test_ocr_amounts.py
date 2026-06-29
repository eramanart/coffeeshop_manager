"""
Smoke test for the receipt amount-extraction fix (2026-06-17).

Pure-logic tests (no OCR / torch) over scan_receipt._extract_amounts and
confidence_tier. Verifies:
  - comma-printed amounts (6,99) validate — no cosmetic separator rejection
  - the (net+VAT=gross, VAT=net*rate) triple is read from stacked layouts
  - NO fabrication: a receipt whose only "amount" is the date yields None
  - status distinguishes validated / unvalidated / no_breakdown (Phase 1 hinge)
  - confidence_tier caps unvalidated/incomplete at Tier 2 regardless of score

Run:  python tests/test_ocr_amounts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.skills.scan_receipt import _extract_amounts, confidence_tier  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS  {name}")
    else:
        failed += 1; print(f"  FAIL  {name}  {extra}")


print("Amount extraction:")

# 1) Lidl stacked block, COMMA decimals — must validate (separator is cosmetic)
lidl = "Be PVM\nPVM\nSu PVM\nA= 21.00%\n5,78\n1,21\n6,99\nData 2026.05.25 14:41"
r = _extract_amounts(lidl)
check("comma-form 5,78/1,21/6,99 validates",
      r["validated"] and r["net"] == "5.78" and r["pvm"] == "1.21" and r["gross"] == "6.99",
      str(r))
check("  status == validated", r["status"] == "validated")

# 2) Period decimals also validate (clean invoice form)
clean = "Suma be PVM: 248.76\nPVM (21% PVM1): 52.24\nIs viso su PVM: 301.00"
r = _extract_amounts(clean)
check("period-form 248.76/52.24/301.00 validates",
      r["validated"] and r["net"] == "248.76" and r["pvm"] == "52.24", str(r))

# 3) NO fabrication: card slip, only a total + a date — must NOT invent amounts
slip = "Atsiskaitymo suma:\n74,38 EUR\nIs VISO:\n74,38 EUR\nData 2026.05.25"
r = _extract_amounts(slip)
check("card slip → no amount fabricated", r["net"] is None and not r["validated"], str(r))
check("  status == no_breakdown (portal candidate)", r["status"] == "no_breakdown")

# 4) Breakdown labels present but numbers don't reconcile → unvalidated (confirm)
bad = "Be PVM 5,00\nPVM suma 1,50\nSu PVM 9,99"
r = _extract_amounts(bad)
check("unreconciled breakdown → not validated", not r["validated"], str(r))
check("  status == unvalidated (owner confirm)", r["status"] == "unvalidated")

# 5) The exact old-bug shape: date as the only large number, no real triple
datebug = "Kvito Nr. 771638\nMoketi\n37,98\nIs VISO 37,98 EUR\n2026.06.13"
r = _extract_amounts(datebug)
check("date 2026.06 NOT turned into an amount",
      r["net"] is None and r["gross"] != "2026.06", str(r))

print("Tier gate:")
base = {"confidence": 95, "supplier_vat": "LT1", "doc_date": "2026-06-13", "pvm_code": "PVM1"}

t, why = confidence_tier({**base, "amounts_validated": True, "amount_status": "validated",
                          "net_amount": "5.78", "pvm_amount": "1.21"})
check("validated + complete + 95% → Tier 1", t == 1 and not why, f"{t} {why}")

t, why = confidence_tier({**base, "amounts_validated": False, "amount_status": "no_breakdown",
                          "net_amount": None, "pvm_amount": None})
check("no_breakdown @95% → Tier 2 (capped)", t == 2)
check("  reason names portal candidate", any("portal" in w for w in why), str(why))

t, why = confidence_tier({**base, "amounts_validated": False, "amount_status": "unvalidated",
                          "net_amount": None, "pvm_amount": None})
check("unvalidated @95% → Tier 2 (capped)", t == 2)
check("  reason names reconcile failure", any("reconcile" in w for w in why), str(why))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

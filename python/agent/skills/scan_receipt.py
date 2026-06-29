"""
agent/skills/scan_receipt.py — Receipt OCR pipeline
Uses docTR (preferred) with EasyOCR as fallback.

Implements the three-tier confidence gate from the blind-spot amendment:
  Tier 1 — confidence >= 90%  → proceed to isaf_generator automatically
  Tier 2 — confidence 70-89%  → flag for owner confirmation via Telegram
  Tier 3 — confidence < 70%   → move to manual_review/, never generate XML

Returns a structured dict consumed by agent/runner.py.

Install:
    pip install "python-doctr[torch]" easyocr pillow
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

log = logging.getLogger("agent.skills.scan_receipt")

# ── Lithuanian VAT / company code patterns ────────────────────────────────────
LT_VAT_PATTERN     = re.compile(r"\bLT\d{9,12}\b")
LT_COMPANY_PATTERN = re.compile(r"\b\d{9}\b")           # 9-digit company code
DATE_PATTERN       = re.compile(
    r"\b(\d{4}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/.]\d{2}[-/.]\d{4})\b"
)
AMOUNT_PATTERN     = re.compile(r"\b(\d{1,6}[.,]\d{2})\b")

# Known PVM codes and their VAT rates
PVM_RATES = {
    "PVM1": "0.21",   # standard rate
    "PVM2": "0.09",   # reduced (food, books)
    "PVM5": "0.05",   # super-reduced
}


def scan_receipt(image_path: Path) -> dict[str, Any]:
    """
    Run OCR on a receipt image and extract structured fields.

    Args:
        image_path: Path to a .jpg, .jpeg, .png, or .pdf file.

    Returns:
        {
          "confidence":    float (0–100),
          "raw_text":      str,
          "supplier_name": str | None,
          "supplier_vat":  str | None,   # e.g. "LT123456789"
          "supplier_code": str | None,   # 9-digit Lithuanian company code
          "doc_num":       str | None,
          "doc_date":      str | None,   # YYYY-MM-DD
          "net_amount":    str | None,   # Decimal-compatible string
          "pvm_amount":    str | None,
          "pvm_code":      str,          # "PVM1" | "PVM2" | "PVM5"
          "engine":        str,          # "doctr" | "easyocr" | "stub"
        }

    Raises:
        FileNotFoundError: if image_path does not exist.
        RuntimeError: if both OCR engines fail.
    """
    if not image_path.exists():
        raise FileNotFoundError(f"Receipt file not found: {image_path}")

    suffix = image_path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".pdf"}:
        raise ValueError(f"Unsupported file type: {suffix}")

    log.info("Scanning receipt: %s", image_path.name)

    # ── Try docTR first (best for structured documents / invoices) ────────────
    raw_text, confidence, engine = _run_doctr(image_path)

    # ── Fall back to EasyOCR for hand-stamped or low-quality scans ───────────
    if confidence < 50:
        log.info("docTR confidence low (%s%%) — trying EasyOCR fallback", confidence)
        easy_text, easy_conf, _ = _run_easyocr(image_path)
        if easy_conf > confidence:
            raw_text, confidence, engine = easy_text, easy_conf, "easyocr"
            log.info("EasyOCR improved confidence to %s%%", confidence)

    log.info("Final engine=%s confidence=%s%%", engine, confidence)

    # ── Extract structured fields from raw text ───────────────────────────────
    fields = _extract_fields(raw_text)
    fields["confidence"] = confidence
    fields["raw_text"]   = raw_text
    fields["engine"]     = engine

    _log_extraction(image_path.name, fields)
    return fields


# ── OCR engines ───────────────────────────────────────────────────────────────

def _run_doctr(image_path: Path) -> tuple[str, float, str]:
    """
    Run docTR on the image.
    Returns (raw_text, confidence_0_to_100, engine_name).
    Falls back gracefully if docTR is not installed.
    """
    try:
        from doctr.io import DocumentFile
        from doctr.models import ocr_predictor

        log.debug("Loading docTR model...")
        model = ocr_predictor(pretrained=True)

        if image_path.suffix.lower() == ".pdf":
            doc = DocumentFile.from_pdf(str(image_path))
        else:
            doc = DocumentFile.from_images([str(image_path)])

        result = model(doc)

        # Extract text preserving line structure; collect per-word confidences
        line_texts, confidences = [], []
        for page in result.pages:
            for block in page.blocks:
                for line in block.lines:
                    line_words = []
                    for word in line.words:
                        line_words.append(word.value)
                        confidences.append(word.confidence)
                    line_texts.append(" ".join(line_words))

        raw_text   = "\n".join(line_texts)
        confidence = round((sum(confidences) / len(confidences) * 100)
                           if confidences else 0.0, 1)
        return raw_text, confidence, "doctr"

    except ImportError:
        log.warning("docTR not installed. Run: pip install python-doctr[torch]")
        return "", 0.0, "doctr_missing"
    except Exception as exc:
        log.error("docTR error: %s", exc)
        return "", 0.0, "doctr_error"


def _run_easyocr(image_path: Path) -> tuple[str, float, str]:
    """
    Run EasyOCR on the image.
    Returns (raw_text, confidence_0_to_100, engine_name).
    Falls back gracefully if EasyOCR is not installed.
    """
    try:
        import easyocr
        import numpy as np
        from PIL import Image

        log.debug("Loading EasyOCR reader (lt + en)...")
        reader = easyocr.Reader(["lt", "en"], gpu=False, verbose=False)

        img  = Image.open(image_path).convert("RGB")
        arr  = np.array(img)
        raws = reader.readtext(arr, detail=1)

        words, confidences = [], []
        for (_bbox, text, conf) in raws:
            words.append(text)
            confidences.append(conf)

        raw_text   = "\n".join(words)
        confidence = round((sum(confidences) / len(confidences) * 100)
                           if confidences else 0.0, 1)
        return raw_text, confidence, "easyocr"

    except ImportError:
        log.warning("EasyOCR not installed. Run: pip install easyocr pillow")
        return "", 0.0, "easyocr_missing"
    except Exception as exc:
        log.error("EasyOCR error: %s", exc)
        return "", 0.0, "easyocr_error"


# ── Field extraction ──────────────────────────────────────────────────────────

def _extract_fields(raw_text: str) -> dict[str, Any]:
    """
    Extract Lithuanian invoice fields from raw OCR text using regex heuristics.
    All returned amounts are strings safe for Decimal() conversion.
    """
    fields: dict[str, Any] = {
        "supplier_name":     None,
        "supplier_vat":      None,
        "supplier_code":     None,
        "doc_num":           None,
        "doc_date":          None,
        "net_amount":        None,
        "pvm_amount":        None,
        "gross_amount":      None,
        "amounts_validated": False,
        "amount_status":     "no_breakdown",  # validated | unvalidated | no_breakdown
        "pvm_code":          "PVM1",  # default: standard 21% rate
    }

    # ── VAT code (LT + 9-12 digits) ──────────────────────────────────────────
    vat_match = LT_VAT_PATTERN.search(raw_text.upper())
    if vat_match:
        fields["supplier_vat"] = vat_match.group()

    # ── Company code (9-digit standalone number) ──────────────────────────────
    # Exclude numbers that are part of the VAT code
    cleaned = LT_VAT_PATTERN.sub("", raw_text)
    code_matches = LT_COMPANY_PATTERN.findall(cleaned)
    if code_matches:
        fields["supplier_code"] = code_matches[0]

    # ── Document date ─────────────────────────────────────────────────────────
    date_match = DATE_PATTERN.search(raw_text)
    if date_match:
        fields["doc_date"] = _normalise_date(date_match.group())

    # ── Document number ───────────────────────────────────────────────────────
    # Excludes "PVM" prefix — that's a tax code, not an invoice number prefix
    doc_num_match = re.search(
        r"(?:SF|INV|Serija/Nr\.?|Nr\.?|No\.?)\s*[:\-]?\s*([A-Z0-9\-/]+)",
        raw_text, re.IGNORECASE
    )
    if doc_num_match:
        fields["doc_num"] = doc_num_match.group(1).strip()

    # ── Amounts: validated (net+VAT=gross) triple, or None (never fabricated) ──
    amounts = _extract_amounts(raw_text)
    fields["net_amount"]        = amounts.get("net")
    fields["pvm_amount"]        = amounts.get("pvm")
    fields["gross_amount"]      = amounts.get("gross")
    fields["amounts_validated"] = amounts.get("validated", False)
    fields["amount_status"]     = amounts.get("status", "no_breakdown")

    # ── PVM code: detect reduced rate keywords ────────────────────────────────
    upper = raw_text.upper()
    if "PVM2" in upper or "9%" in upper or "9 %" in upper:
        fields["pvm_code"] = "PVM2"
    elif "PVM5" in upper or "5%" in upper:
        fields["pvm_code"] = "PVM5"

    # ── Supplier name: first non-empty line before the VAT code ──────────────
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    if lines:
        fields["supplier_name"] = lines[0][:120]  # cap at 120 chars

    return fields


# Standard Lithuanian VAT rates, as a fraction of the net amount.
_LT_VAT_RATES = (Decimal("0.21"), Decimal("0.09"), Decimal("0.05"))


def _amount_candidates(raw_text: str) -> list[Decimal]:
    """
    Pull plausible monetary amounts from the text, EXCLUDING things that look like
    amounts but aren't: year.month date fragments (2026.05), printed VAT rates
    (21.00 / 9.00 / 5.00), and non-positive values. Keeps duplicates (an amount can
    legitimately repeat) so membership tests reflect what's actually printed.
    """
    cands: list[Decimal] = []
    for m in AMOUNT_PATTERN.finditer(raw_text):
        tok = m.group(1)
        intpart = re.split(r"[.,]", tok)[0]
        if len(intpart) == 4 and intpart[:2] in ("19", "20"):
            continue                                   # 2026.05 etc. — a date, not money
        try:
            val = Decimal(tok.replace(",", "."))
        except Exception:
            continue
        if val <= 0 or val in (Decimal("21.00"), Decimal("9.00"), Decimal("5.00")):
            continue                                   # rate printed as an amount
        cands.append(val)
    return cands


def _has_vat_breakdown(raw_text: str) -> bool:
    """
    True if the receipt prints a VAT breakdown (net / VAT / gross lines), vs. a bare
    card slip that shows only a total. We look for the net/gross breakdown labels
    specifically — plain "PVM" is excluded because it also appears in VAT-registration
    lines ("PVM mokėtojo kodas", "PVM sąskaitos faktūros išrašomos").
    """
    u = raw_text.upper()
    return any(k in u for k in ("BE PVM", "PVM SUMA", "SU PVM", "SUMA SU PVM"))


def _extract_amounts(raw_text: str) -> dict[str, Any]:
    """
    Identify the (net, VAT, gross) triple the receipt actually contains, validated by
    the relationships that MUST hold on a real Lithuanian VAT receipt:

        net + VAT == gross   AND   VAT == round(net × rate)   for rate in {21,9,5}%

    This is layout-independent and self-checking (all comparisons are in Decimal, so
    comma- and period-printed amounts compare equal), so it ignores dates, the printed
    "21,00 %" rate, card numbers, etc. The candidate gross must literally appear among
    the printed amounts.

    Returns {net, pvm, gross, validated, status}. When no valid triple is present we
    return all-None and DISTINGUISH why via status — the hinge for downstream routing:
      "validated"    — a real net+VAT=gross triple was read.
      "unvalidated"  — VAT breakdown lines are printed but don't reconcile → owner
                       confirm (OCR likely misread a digit).
      "no_breakdown" — no net/VAT lines at all (a card slip with only a total) → the
                       VAT invoice lives in the supplier portal (Phase 1 candidate).
    We never fabricate (the old fallback derived numbers from the date, e.g. gross
    "2026.05" → net 1674.42).
    """
    result: dict[str, Any] = {"net": None, "pvm": None, "gross": None,
                              "validated": False, "status": "no_breakdown"}
    has_breakdown = _has_vat_breakdown(raw_text)
    cands = _amount_candidates(raw_text)

    if len(cands) >= 2:
        uniq = list(dict.fromkeys(cands))   # distinct values, printed order
        cent = Decimal("0.01")
        best = None                          # prefer the triple with the largest gross
        for net in uniq:
            for vat in uniq:
                if vat >= net:
                    continue                 # VAT (≤21%) is always smaller than net
                gross = net + vat
                if gross not in cands:       # gross must be printed on the receipt
                    continue
                for rate in _LT_VAT_RATES:
                    if abs(vat - (net * rate).quantize(cent, ROUND_HALF_UP)) <= cent:
                        if best is None or gross > best[2]:
                            best = (net, vat, gross)
                        break
        if best:
            net, vat, gross = best
            result.update(net=str(net), pvm=str(vat), gross=str(gross),
                          validated=True, status="validated")
            log.info("Amounts validated: net=%s pvm=%s gross=%s", net, vat, gross)
            return result

    # No validated triple — distinguish unreconciled breakdown from no breakdown.
    result["status"] = "unvalidated" if has_breakdown else "no_breakdown"
    log.info("No validated triple — status=%s", result["status"])
    return result


def _normalise_date(date_str: str) -> str:
    """
    Convert various date formats to YYYY-MM-DD.
    Handles: YYYY-MM-DD, DD-MM-YYYY, YYYY/MM/DD, DD.MM.YYYY
    """
    date_str = date_str.replace("/", "-").replace(".", "-")
    parts    = date_str.split("-")
    if len(parts) == 3:
        if len(parts[0]) == 4:                   # YYYY-MM-DD
            return date_str
        elif len(parts[2]) == 4:                 # DD-MM-YYYY
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return date_str                              # return as-is if unrecognised


def _log_extraction(filename: str, fields: dict) -> None:
    """Log a summary of extracted fields for audit purposes."""
    log.info(
        "Extraction summary — file=%s vat=%s code=%s date=%s "
        "net=%s pvm=%s pvm_code=%s confidence=%s%%",
        filename,
        fields.get("supplier_vat", "?"),
        fields.get("supplier_code", "?"),
        fields.get("doc_date", "?"),
        fields.get("net_amount", "?"),
        fields.get("pvm_amount", "?"),
        fields.get("pvm_code", "?"),
        fields.get("confidence", "?"),
    )


# ── Confidence + integrity tier gate ──────────────────────────────────────────

# Fields that MUST be present and trustworthy before a receipt may auto-process.
REQUIRED_FIELDS = ("supplier_vat", "doc_date", "net_amount", "pvm_amount", "pvm_code")


def confidence_tier(fields: dict) -> tuple[int, list[str]]:
    """
    Decide the handling tier for a scanned receipt.

    The OCR confidence score only measures how clearly text was *read* — it says
    nothing about whether the tax numbers are correct or complete. So confidence
    sets the starting tier, but two hard floors prevent auto-processing bad data:

      • amounts must be VALIDATED (a real net+VAT=gross triple was found), and
      • all REQUIRED_FIELDS must be present.

    Either failing caps the tier at 2 (owner confirmation) no matter how high the
    confidence — e.g. a 93% scan that only found the date, not a real total, is NOT
    auto-processed. Returns (tier, reasons-it-was-demoted).
    """
    conf = fields.get("confidence", 0) or 0
    tier = 1 if conf >= 90 else 2 if conf >= 70 else 3

    reasons: list[str] = []
    if not fields.get("amounts_validated"):
        status = fields.get("amount_status", "unvalidated")
        if status == "no_breakdown":
            reasons.append("no VAT breakdown printed (card slip → supplier-portal candidate)")
        else:
            reasons.append("VAT lines present but don't reconcile (net+VAT≠gross) → confirm")
        tier = max(tier, 2)
    missing = [f for f in REQUIRED_FIELDS if not fields.get(f)]
    if missing:
        reasons.append("missing required field(s): " + ", ".join(missing))
        tier = max(tier, 2)
    return tier, reasons


# ── CLI smoke test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) < 2:
        print("Usage: python scan_receipt.py <image_path>")
        sys.exit(1)
    result = scan_receipt(Path(sys.argv[1]))
    # Omit raw_text from console output for readability
    display = {k: v for k, v in result.items() if k != "raw_text"}
    print(json.dumps(display, indent=2, ensure_ascii=False))
    tier, reasons = confidence_tier(result)
    label = {1: "1 (auto)", 2: "2 (owner confirm)", 3: "3 (manual review)"}[tier]
    print(f"\nConfidence gate: Tier {label}")
    for r in reasons:
        print(f"  ! demoted: {r}")

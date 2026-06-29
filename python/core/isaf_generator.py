"""
core/isaf_generator.py — VMI i.SAF XML builder

Produces XML conforming to the official Lithuanian VMI i.SAF 1.2 XSD.
Validates output against the XSD before returning if the schema is present.

Official schema: config/schemas/isaf_v1.2.xsd

CLI:
  python -m core.isaf_generator --test
  python -m core.isaf_generator --sample   # writes data/sample_isaf.xml
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import List

try:
    from lxml import etree as _etree
except ImportError:
    _etree = None  # type: ignore

log = logging.getLogger("core.isaf_generator")

ISAF_NAMESPACE = "http://www.vmi.lt/cms/imas/isaf"
ISAF_VERSION   = "iSAF1.2"
XSI_NAMESPACE  = "http://www.w3.org/2001/XMLSchema-instance"
XSD_PATH       = Path("config/schemas/isaf_v1.2.xsd")

LT_VAT_RE  = re.compile(r"^LT\d{9,12}$")
LT_CODE_RE = re.compile(r"^\d{9}$")

VALID_PVM_CODES = {
    "PVM1","PVM2","PVM3","PVM4","PVM5","PVM6","PVM7",
    "PVM8","PVM9","PVM10","PVM11","PVM12","PVM13","PVM14","PVM99",
}
PVM_PERCENT = {
    "PVM1": "21", "PVM2": "9", "PVM5": "5", "PVM0": "0",
}
VALID_DOC_TYPES = {"SF", "DS", "KS", "VS", "VD", "VK", "AN", ""}


# ═════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class InvoiceLine:
    """One purchase or sales invoice for i.SAF reporting."""
    doc_num:       str
    supplier_vat:  str       # LT + 9-12 digits
    supplier_code: str       # 9-digit company code
    date:          str       # YYYY-MM-DD
    net_amount:    Decimal   # TaxableValue (excl. VAT)
    pvm_amount:    Decimal   # VAT amount
    pvm_code:      str = "PVM1"
    doc_type:      str = "SF"
    supplier_name: str = "ND"
    buyer_vat:     str | None = None
    buyer_code:    str | None = None
    buyer_name:    str = "ND"

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        errors: list[str] = []
        if not LT_VAT_RE.match(self.supplier_vat.upper()):
            errors.append(f"supplier_vat '{self.supplier_vat}' must match LT + 9-12 digits")
        if not LT_CODE_RE.match(self.supplier_code):
            errors.append(f"supplier_code '{self.supplier_code}' must be exactly 9 digits")
        try:
            date.fromisoformat(self.date)
        except ValueError:
            errors.append(f"date '{self.date}' must be YYYY-MM-DD")
        if self.net_amount < Decimal("0"):
            errors.append("net_amount must be non-negative")
        if self.pvm_amount < Decimal("0"):
            errors.append("pvm_amount must be non-negative")
        if self.pvm_code.upper() not in VALID_PVM_CODES:
            errors.append(f"pvm_code '{self.pvm_code}' not in valid set")
        if self.doc_type not in VALID_DOC_TYPES:
            errors.append(f"doc_type '{self.doc_type}' not in {VALID_DOC_TYPES}")
        if errors:
            raise ValueError("InvoiceLine validation failed:\n" +
                             "\n".join(f"  • {e}" for e in errors))

    @property
    def gross_amount(self) -> Decimal:
        return (self.net_amount + self.pvm_amount).quantize(Decimal("0.01"), ROUND_HALF_UP)

    def fmt(self, d: Decimal) -> str:
        return str(d.quantize(Decimal("0.01"), ROUND_HALF_UP))


@dataclass
class ISAFReport:
    """Container for a full i.SAF monthly report."""
    file_date:        str
    period_start:     str
    period_end:       str
    taxpayer_vat:     str
    taxpayer_code:    str
    purchase_docs:    List[InvoiceLine] = field(default_factory=list)
    sales_docs:       List[InvoiceLine] = field(default_factory=list)
    software_name:    str = "CoffeeManager-OS"
    software_version: str = "1.0"


# ═════════════════════════════════════════════════════════════════════════════
# XML BUILDER  (matches iSAF 1.2 XSD exactly)
# ═════════════════════════════════════════════════════════════════════════════

def build_isaf_xml(
    lines: list[InvoiceLine],
    report: ISAFReport | None = None,
) -> bytes:
    """
    Build a valid i.SAF 1.2 XML document from InvoiceLine records.

    Validated against the VMI XSD if config/schemas/isaf_v1.2.xsd is present.
    Raises RuntimeError on XSD validation failure.
    """
    if _etree is None:
        raise ImportError("lxml is required: pip install lxml")
    etree = _etree

    if not lines:
        raise ValueError("Cannot build i.SAF XML: no invoice lines provided")

    today = date.today().isoformat()
    now_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if report is None:
        report = ISAFReport(
            file_date=today,
            period_start=min(l.date for l in lines),
            period_end=max(l.date for l in lines),
            taxpayer_vat=_infer_taxpayer_vat(),
            taxpayer_code=_infer_taxpayer_code(),
            purchase_docs=lines,
        )

    ns  = ISAF_NAMESPACE
    xsi = XSI_NAMESPACE
    nsmap = {None: ns, "xsi": xsi}

    root = etree.Element(f"{{{ns}}}iSAFFile", nsmap=nsmap)

    # ── Header > FileDescription ──────────────────────────────────────────────
    header = etree.SubElement(root, f"{{{ns}}}Header")
    fd     = etree.SubElement(header, f"{{{ns}}}FileDescription")

    _add(fd, ns, "FileVersion",        ISAF_VERSION)
    _add(fd, ns, "FileDateCreated",    now_dt)

    has_purchases = bool(report.purchase_docs)
    has_sales     = bool(report.sales_docs)
    data_type = "F" if (has_purchases and has_sales) else ("S" if has_sales else "P")
    _add(fd, ns, "DataType",           data_type)

    _add(fd, ns, "SoftwareCompanyName", report.software_name)
    _add(fd, ns, "SoftwareName",        report.software_name)
    _add(fd, ns, "SoftwareVersion",     report.software_version)
    _add(fd, ns, "RegistrationNumber",  report.taxpayer_code)
    _add(fd, ns, "NumberOfParts",       "1")
    _add(fd, ns, "PartNumber",          "1")

    sc = etree.SubElement(fd, f"{{{ns}}}SelectionCriteria")
    _add(sc, ns, "SelectionStartDate", report.period_start)
    _add(sc, ns, "SelectionEndDate",   report.period_end)

    # ── SourceDocuments ───────────────────────────────────────────────────────
    sd = etree.SubElement(root, f"{{{ns}}}SourceDocuments")

    if has_purchases:
        pi = etree.SubElement(sd, f"{{{ns}}}PurchaseInvoices")
        for line in report.purchase_docs:
            _build_purchase_invoice(pi, ns, xsi, line)

    if has_sales:
        si_sec = etree.SubElement(sd, f"{{{ns}}}SalesInvoices")
        for line in report.sales_docs:
            _build_sales_invoice(si_sec, ns, xsi, line)

    # ── Serialise ─────────────────────────────────────────────────────────────
    xml_bytes = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )

    if XSD_PATH.exists():
        _validate_against_xsd(root, xml_bytes)
    else:
        log.warning(
            "XSD not found at %s — skipping validation. "
            "Place the VMI schema there to enable.", XSD_PATH
        )

    log.info(
        "i.SAF XML built: %d purchase + %d sales docs, %d bytes",
        len(report.purchase_docs), len(report.sales_docs), len(xml_bytes),
    )
    return xml_bytes


def _build_purchase_invoice(parent, ns: str, xsi: str, line: InvoiceLine) -> None:
    inv = _sub(parent, ns, "Invoice")
    _add(inv, ns, "InvoiceNo", line.doc_num)

    si = _sub(inv, ns, "SupplierInfo")
    _add(si, ns, "VATRegistrationNumber", line.supplier_vat.upper())
    if line.supplier_code:
        _add(si, ns, "RegistrationNumber", line.supplier_code)
    _nil(si, ns, xsi, "Country")
    _add(si, ns, "Name", line.supplier_name or "ND")

    _add(inv, ns, "InvoiceDate",      line.date)
    _add(inv, ns, "InvoiceType",      line.doc_type if line.doc_type in VALID_DOC_TYPES else "SF")
    _add(inv, ns, "SpecialTaxation",  "")
    _sub(inv, ns, "References")           # empty — no credit-note references
    _nil(inv, ns, xsi, "VATPointDate")
    _nil(inv, ns, xsi, "RegistrationAccountDate")

    dt  = _sub(inv, ns, "DocumentTotals")
    tot = _sub(dt,  ns, "DocumentTotal")
    _add(tot, ns, "TaxableValue",  line.fmt(line.net_amount))
    _nil(tot, ns, xsi, "TaxCode")         # nil for domestic LT purchase (no reverse charge)
    _add(tot, ns, "TaxPercentage", PVM_PERCENT.get(line.pvm_code.upper(), "21"))
    _add(tot, ns, "Amount",        line.fmt(line.pvm_amount))


def _build_sales_invoice(parent, ns: str, xsi: str, line: InvoiceLine) -> None:
    inv = _sub(parent, ns, "Invoice")
    _add(inv, ns, "InvoiceNo", line.doc_num)

    ci = _sub(inv, ns, "CustomerInfo")
    _add(ci, ns, "VATRegistrationNumber", line.buyer_vat.upper() if line.buyer_vat else "ND")
    if line.buyer_code:
        _add(ci, ns, "RegistrationNumber", line.buyer_code)
    _nil(ci, ns, xsi, "Country")
    _add(ci, ns, "Name", line.buyer_name or "ND")

    _add(inv, ns, "InvoiceDate",     line.date)
    _add(inv, ns, "InvoiceType",     line.doc_type if line.doc_type in VALID_DOC_TYPES else "SF")
    _add(inv, ns, "SpecialTaxation", "")
    _sub(inv, ns, "References")
    _nil(inv, ns, xsi, "VATPointDate")

    dt  = _sub(inv, ns, "DocumentTotals")
    tot = _sub(dt,  ns, "DocumentTotal")
    _add(tot, ns, "TaxableValue",  line.fmt(line.net_amount))
    _nil(tot, ns, xsi, "TaxCode")
    _add(tot, ns, "TaxPercentage", PVM_PERCENT.get(line.pvm_code.upper(), "21"))
    _add(tot, ns, "Amount",        line.fmt(line.pvm_amount))


# ── lxml helpers ──────────────────────────────────────────────────────────────

def _sub(parent, ns: str, tag: str):
    return _etree.SubElement(parent, f"{{{ns}}}{tag}")

def _add(parent, ns: str, tag: str, text: str) -> None:
    el      = _sub(parent, ns, tag)
    el.text = text

def _nil(parent, ns: str, xsi: str, tag: str) -> None:
    el = _sub(parent, ns, tag)
    el.set(f"{{{xsi}}}nil", "true")


# ── XSD validation ────────────────────────────────────────────────────────────

def _validate_against_xsd(root, xml_bytes: bytes) -> None:
    try:
        with XSD_PATH.open("rb") as f:
            schema = _etree.XMLSchema(_etree.parse(f))
        schema.assertValid(root)
        log.info("XSD validation passed ✓")
    except _etree.DocumentInvalid as exc:
        errors = "\n".join(str(e) for e in schema.error_log)
        raise RuntimeError(
            f"i.SAF XML failed XSD validation:\n{errors}\n\n"
            f"Check that config/schemas/isaf_v1.2.xsd is the current VMI schema."
        ) from exc
    except Exception as exc:
        log.warning("XSD validation error (non-fatal): %s", exc)


# ── Config helpers ────────────────────────────────────────────────────────────

def _env_code(name: str) -> str:
    """
    Read an identifier-style env var, stripping surrounding whitespace.
    A stray trailing space (e.g. 'VMI_VAT_CODE=LT100000000000 ') is a common config
    foot-gun that would otherwise fail the strict regex validation below. Stripping
    on read prevents the whole class rather than chasing one bad line.
    """
    import os
    return os.getenv(name, "").strip()


def _infer_taxpayer_vat() -> str:
    vat = _env_code("VMI_VAT_CODE")
    if not vat or not LT_VAT_RE.match(vat.upper()):
        raise EnvironmentError(
            "VMI_VAT_CODE is missing or invalid. "
            "Set to your LT VAT code (e.g. LT123456789) in settings.env."
        )
    return vat.upper()


def _infer_taxpayer_code() -> str:
    code = _env_code("VMI_COMPANY_CODE")
    if not code or not LT_CODE_RE.match(code):
        raise EnvironmentError(
            "VMI_COMPANY_CODE is missing or invalid. "
            "Set to your 9-digit company code in settings.env."
        )
    return code


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def _run_tests() -> None:
    print("Running isaf_generator unit tests...")
    errors: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ✓ {name}")
        else:
            errors.append(f"FAIL {name}: {detail}")

    line = InvoiceLine(
        doc_num="SF-2025-001", supplier_vat="LT123456789",
        supplier_code="123456789", date="2025-05-01",
        net_amount=Decimal("100.00"), pvm_amount=Decimal("21.00"),
    )
    check("valid InvoiceLine", True)
    check("gross_amount", line.gross_amount == Decimal("121.00"))

    import os
    os.environ.setdefault("VMI_VAT_CODE",     "LT100000000000")
    os.environ.setdefault("VMI_COMPANY_CODE", "300000000")
    try:
        xml = build_isaf_xml([line])
        check("XML builds",             isinstance(xml, bytes))
        check("non-empty",              len(xml) > 100)
        check("namespace present",      ISAF_NAMESPACE.encode() in xml)
        check("FileVersion present",    b"iSAF1.2" in xml)
        check("SourceDocuments",        b"SourceDocuments" in xml)
        check("PurchaseInvoices",       b"PurchaseInvoices" in xml)
        check("SupplierVAT present",    b"LT123456789" in xml)
        check("InvoiceNo present",      b"SF-2025-001" in xml)
        check("TaxableValue present",   b"100.00" in xml)
        check("Amount (VAT) present",   b"21.00" in xml)
    except Exception as exc:
        errors.append(f"FAIL XML generation: {exc}")

    try:
        InvoiceLine(doc_num="X", supplier_vat="INVALID",
                    supplier_code="123", date="2025-01-01",
                    net_amount=Decimal("10"), pvm_amount=Decimal("2"))
        errors.append("FAIL bad VAT: should have raised ValueError")
    except ValueError:
        print("  ✓ bad supplier_vat raises ValueError")

    try:
        build_isaf_xml([])
        errors.append("FAIL empty lines: should have raised ValueError")
    except ValueError:
        print("  ✓ empty lines raises ValueError")

    print()
    if errors:
        for e in errors: print(f"  {e}")
        print(f"\n{len(errors)} test(s) FAILED.")
        sys.exit(1)
    else:
        print("All tests passed ✅")


def _write_sample() -> None:
    import os
    os.environ.setdefault("VMI_VAT_CODE",     "LT100000000000")
    os.environ.setdefault("VMI_COMPANY_CODE", "300000000")

    lines = [
        InvoiceLine(
            doc_num="SF-2025-0042",
            supplier_vat="LT987654321", supplier_code="987654321",
            supplier_name="Kavos Tiekėjas UAB",
            date="2025-05-01",
            net_amount=Decimal("248.76"), pvm_amount=Decimal("52.24"),
            pvm_code="PVM1",
        ),
        InvoiceLine(
            doc_num="SF-2025-0043",
            supplier_vat="LT555444333", supplier_code="555444333",
            supplier_name="Pieno Žvaigždės AB",
            date="2025-05-03",
            net_amount=Decimal("91.74"), pvm_amount=Decimal("8.26"),
            pvm_code="PVM2",
        ),
    ]
    xml = build_isaf_xml(lines)
    out = Path("data/sample_isaf.xml")
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(xml)
    print(f"Sample i.SAF XML written to {out} ({len(xml)} bytes)")
    print(xml.decode("utf-8"))


if __name__ == "__main__":
    import argparse
    sys.path.insert(0, str(Path(__file__).parent.parent))

    parser = argparse.ArgumentParser(description="CoffeeManager-OS i.SAF generator")
    parser.add_argument("--test",   action="store_true")
    parser.add_argument("--sample", action="store_true")
    args = parser.parse_args()

    if args.test:
        _run_tests()
    elif args.sample:
        _write_sample()
    else:
        parser.print_help()

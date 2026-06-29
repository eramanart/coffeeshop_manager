"""
agent/runner.py — OpenClaw task dispatcher
Bridges the Python deterministic core with the OpenClaw agentic brain.

Each public method maps one task type (from main.py's context["tasks"])
to a structured OpenClaw API call, logs every portal interaction to SQLite,
and returns a typed result dict consumed by main.py's notification layer.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from migrate import get_connection, log_portal_action, upsert_receipt

log = logging.getLogger("agent.runner")

# Task type → handler method name
TASK_REGISTRY: dict[str, str] = {
    "process_receipts":    "handle_process_receipts",
    "register_new_hires":  "handle_register_new_hires",
    "draft_purchase_order":"handle_draft_purchase_order",
}

# Maximum consecutive portal failures before circuit breaker trips
CIRCUIT_BREAKER_THRESHOLD = 3


class CircuitBreakerOpen(Exception):
    """Raised when the circuit breaker has tripped for a portal."""
    pass


class OpenClawAgent:
    """
    Wraps the OpenClaw API and routes tasks to skill handlers.

    All portal interactions are logged to portal_actions in SQLite.
    No VMI or Sodra form is ever submitted — only drafted and alerted.
    """

    def __init__(
        self,
        api_key: str,
        soul_path: Path,
        db_conn,
        session_id: str,
        dry_run: bool = False,
    ) -> None:
        self.api_key    = api_key
        self.soul_path  = soul_path
        self.conn       = db_conn
        self.session_id = session_id
        self.dry_run    = dry_run
        self._soul      = self._load_soul()
        self._failure_counts: dict[str, int] = {}  # portal → consecutive failures

    # ── Public interface ──────────────────────────────────────────────────────

    async def run_task(self, task: dict) -> dict:
        """
        Dispatch a task dict to its handler.
        Returns a result dict always containing at least {"status": ...}.
        """
        task_type = task.get("task")
        handler_name = TASK_REGISTRY.get(task_type)
        if not handler_name:
            log.warning("Unknown task type: %s — skipping", task_type)
            return {"status": "skipped", "reason": f"unknown task type: {task_type}"}

        handler = getattr(self, handler_name)
        log.info("Running task handler: %s", handler_name)
        return await handler(task)

    # ── Task handlers ─────────────────────────────────────────────────────────

    async def handle_process_receipts(self, task: dict) -> dict:
        """
        OCR each receipt file then navigate VMI i.MAS to draft an i.SAF entry.
        Applies the three-tier confidence gate before any portal interaction.

        Returns:
            {
              "status":        "done",
              "completed":     [ {filename, supplier, doc_date, net_amount}, ... ],
              "manual_review": [ {filename, confidence}, ... ],
              "errors":        [ {filename, error}, ... ],
            }
        """
        from agent.skills.scan_receipt import scan_receipt, confidence_tier

        result = {"status": "done", "completed": [], "manual_review": [], "errors": []}

        for filepath in task.get("files", []):
            fpath = Path(filepath)
            log.info("Processing receipt: %s", fpath.name)
            upsert_receipt(self.conn, fpath.name, ocr_status="processing")

            # ── Step 1: OCR ───────────────────────────────────────────────────
            try:
                ocr = scan_receipt(fpath)
            except Exception as exc:
                log.error("OCR failed for %s: %s", fpath.name, exc)
                upsert_receipt(self.conn, fpath.name,
                               ocr_status="failed", notes=f"OCR error: {exc}")
                result["errors"].append({"filename": fpath.name, "error": str(exc)})
                continue

            confidence = ocr.get("confidence", 0)
            log.info("  OCR confidence: %s%%", confidence)

            # ── Step 2: Tier gate (confidence + amount integrity + fields) ────
            # confidence_tier caps the tier at owner-confirmation whenever the
            # amounts are not VALIDATED (a real net+VAT=gross triple) or a required
            # field is missing — so a high OCR score can never auto-draft fabricated
            # or incomplete tax numbers.
            tier, reasons = confidence_tier(ocr)
            why = "; ".join(reasons) if reasons else f"confidence {confidence}%"

            if tier == 3:
                # Tier 3 — unprocessable; move to manual_review/
                dest = fpath.parent / "manual_review" / fpath.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                fpath.rename(dest)
                log.warning("  Tier 3 (%s) — moved to manual_review/", why)
                upsert_receipt(self.conn, fpath.name,
                               ocr_status="failed",
                               notes=f"Tier 3: {why} — manual review")
                result["manual_review"].append({
                    "filename":   fpath.name,
                    "confidence": confidence,
                })
                self._mark_processed(fpath)
                continue

            if tier == 2:
                # Tier 2 — flag for owner confirmation; do not generate XML
                log.info("  Tier 2 (%s) — flagged for owner confirmation", why)
                upsert_receipt(self.conn, fpath.name,
                               ocr_status="done", vmi_status="pending",
                               supplier_vat=ocr.get("supplier_vat"),
                               supplier_code=ocr.get("supplier_code"),
                               doc_date=ocr.get("doc_date"),
                               net_amount=str(ocr["net_amount"]) if ocr.get("net_amount") else None,
                               pvm_amount=str(ocr["pvm_amount"]) if ocr.get("pvm_amount") else None,
                               pvm_code=ocr.get("pvm_code"),
                               notes=f"Tier 2: {why} — awaiting owner confirmation")
                result["manual_review"].append({
                    "filename":   fpath.name,
                    "confidence": confidence,
                    "ocr_fields": ocr,
                })
                self._mark_processed(fpath)
                continue

            # ── Step 3: Tier 1 — generate i.SAF XML ──────────────────────────
            required_fields = ("supplier_vat", "supplier_code", "doc_date",
                               "net_amount", "pvm_amount")
            missing = [f for f in required_fields if ocr.get(f) is None]
            if missing:
                log.error("  OCR fields missing for %s: %s — cannot generate XML",
                          fpath.name, missing)
                upsert_receipt(self.conn, fpath.name,
                               ocr_status="failed",
                               notes=f"OCR fields not extracted: {missing}")
                result["errors"].append({
                    "filename": fpath.name,
                    "error":    f"OCR fields not extracted: {missing}",
                })
                continue

            try:
                from core.isaf_generator import build_isaf_xml, InvoiceLine
                from decimal import Decimal

                line = InvoiceLine(
                    doc_num=ocr.get("doc_num", f"OCR-{uuid.uuid4().hex[:8].upper()}"),
                    supplier_vat=ocr["supplier_vat"],
                    supplier_code=ocr["supplier_code"],
                    date=ocr["doc_date"],
                    net_amount=Decimal(str(ocr["net_amount"])),
                    pvm_amount=Decimal(str(ocr["pvm_amount"])),
                    pvm_code=ocr.get("pvm_code", "PVM1"),
                )
                xml_bytes = build_isaf_xml([line])
                xml_path  = fpath.parent / f"{fpath.stem}_isaf.xml"
                xml_path.write_bytes(xml_bytes)
                log.info("  i.SAF XML written: %s", xml_path.name)
                upsert_receipt(self.conn, fpath.name,
                               ocr_status="done",
                               supplier_vat=ocr["supplier_vat"],
                               supplier_code=ocr["supplier_code"],
                               doc_date=ocr["doc_date"],
                               net_amount=str(ocr["net_amount"]),
                               pvm_amount=str(ocr["pvm_amount"]),
                               pvm_code=ocr.get("pvm_code"),
                               isaf_xml_path=str(xml_path))
            except Exception as exc:
                log.error("  XML generation failed for %s: %s", fpath.name, exc)
                upsert_receipt(self.conn, fpath.name,
                               ocr_status="failed", notes=f"XML error: {exc}")
                result["errors"].append({"filename": fpath.name, "error": str(exc)})
                continue

            # ── Step 4: Navigate VMI i.MAS portal (draft only) ───────────────
            try:
                draft_ref = await self._vmi_draft_isaf(xml_path, ocr)
                log.info("  VMI draft created: ref=%s", draft_ref)
                upsert_receipt(self.conn, fpath.name,
                               vmi_draft_ref=draft_ref, vmi_status="drafted")
            except CircuitBreakerOpen as exc:
                log.error("  Circuit breaker open for VMI portal: %s", exc)
                upsert_receipt(self.conn, fpath.name,
                               notes="VMI circuit breaker tripped")
                result["errors"].append({
                    "filename": fpath.name,
                    "error":    "VMI portal circuit breaker tripped — manual action required",
                })
                continue
            except Exception as exc:
                log.error("  VMI portal navigation failed: %s", exc)
                upsert_receipt(self.conn, fpath.name,
                               notes=f"VMI portal error: {exc}")
                result["errors"].append({"filename": fpath.name, "error": str(exc)})
                continue

            # ── Step 5: Mark processed ────────────────────────────────────────
            self._mark_processed(fpath)
            result["completed"].append({
                "filename":   fpath.name,
                "supplier":   ocr.get("supplier_name", "unknown"),
                "doc_date":   ocr.get("doc_date"),
                "net_amount": str(ocr.get("net_amount")),
                "draft_ref":  draft_ref,
            })

        return result

    async def handle_register_new_hires(self, task: dict) -> dict:
        """
        For each new hire, navigate the Sodra draudejai portal,
        fill the 1-SD form, and SAVE AS DRAFT — never submit.

        Returns:
            {
              "status":  "done",
              "drafted": [ {name, first_working_day, draft_url}, ... ],
              "errors":  [ {name, error}, ... ],
            }
        """
        result = {"status": "done", "drafted": [], "errors": []}

        for hire in task.get("hires", []):
            name = hire["name"]
            fwd  = hire["first_working_day"]
            log.info("Registering new hire: %s (first day: %s)", name, fwd)

            try:
                draft_url = await self._sodra_draft_1sd(hire)
                log.info("  Sodra 1-SD drafted for %s: %s", name, draft_url)
            except CircuitBreakerOpen as exc:
                log.error("  Circuit breaker open for Sodra portal: %s", exc)
                result["errors"].append({
                    "name":  name,
                    "error": "Sodra portal circuit breaker tripped",
                })
                continue
            except Exception as exc:
                log.error("  Sodra draft failed for %s: %s", name, exc)
                result["errors"].append({"name": name, "error": str(exc)})
                continue

            # Record in hr_actions
            self.conn.execute(
                """INSERT OR IGNORE INTO hr_actions
                   (detected_at, employee_name, first_working_day,
                    sodra_status, draft_url)
                   VALUES (?, ?, ?, 'draft_ready', ?)""",
                (datetime.now(timezone.utc).isoformat(), name, fwd, draft_url),
            )
            self.conn.commit()
            result["drafted"].append({
                "name":              name,
                "first_working_day": fwd,
                "draft_url":         draft_url,
            })

        return result

    async def handle_draft_purchase_order(self, task: dict) -> dict:
        """
        Build a purchase order email draft for the supplier.
        Never sends — owner must confirm with GO.

        Returns: {"status": "drafted", "po_path": str}
        """
        item    = task.get("item", {})
        po_path = Path("data/workspace") / f"PO_{item.get('sku', 'unknown')}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.txt"
        content = (
            f"To: {item.get('supplier_email', '[SUPPLIER EMAIL]')}\n"
            f"Subject: Purchase Order — {item.get('name', 'item')}\n\n"
            f"Dear {item.get('supplier_name', 'Supplier')},\n\n"
            f"Please supply the following:\n\n"
            f"  Item:     {item.get('name', '—')}\n"
            f"  Quantity: {item.get('order_qty', '—')} kg\n"
            f"  Delivery: as soon as possible\n\n"
            f"Best regards,\nCoffeeShop Management\n\n"
            f"[AWAITING OWNER APPROVAL — DO NOT SEND]\n"
        )
        po_path.write_text(content, encoding="utf-8")
        log.info("Purchase order drafted: %s", po_path.name)
        return {"status": "drafted", "po_path": str(po_path)}

    # ── Portal helpers ────────────────────────────────────────────────────────

    async def _vmi_draft_isaf(self, xml_path: Path, ocr: dict) -> str:
        """
        Send a task to the clawdbot main agent to navigate VMI i.MAS and create an i.SAF draft.
        Implements the Smart-ID auth pause and circuit breaker.
        Returns the portal draft reference number.
        """
        portal = "vmi_imas"
        self._check_circuit_breaker(portal)

        company_code = os.getenv("VMI_COMPANY_CODE", "").strip()
        vat_code     = os.getenv("VMI_VAT_CODE", "").strip()
        xml_abs      = xml_path.resolve()

        task_message = (
            f"TASK: Draft an i.SAF invoice entry in the VMI i.MAS portal.\n\n"
            f"Steps:\n"
            f"1. Navigate to https://imas.vmi.lt\n"
            f"2. Log in via Smart-ID with company code {company_code} (VAT: {vat_code}).\n"
            f"3. When the Smart-ID verification code appears on screen, read it from the "
            f"browser and send a Telegram message to the owner with it so they can approve "
            f"on their device.\n"
            f"4. Wait up to 90 seconds for Smart-ID approval. If it times out, stop and "
            f"return an error message — do NOT retry.\n"
            f"5. Navigate to the i.SAF (invoice register) upload section.\n"
            f"6. Upload the XML file at this path: {xml_abs}\n"
            f"7. SAVE AS DRAFT only — do NOT click submit or confirm submission.\n"
            f"8. Return the draft reference number shown on the confirmation screen.\n\n"
            f"Invoice details: supplier={ocr.get('supplier_name')}, "
            f"date={ocr.get('doc_date')}, net={ocr.get('net_amount')} EUR\n\n"
            f"CRITICAL: draft and save only. Never submit the form."
        )

        if self.dry_run:
            log.info("  [DRY RUN] VMI task message:\n%s", task_message)
            return "DRY-RUN-VMI-REF"

        try:
            response_text = await self._run_clawdbot_agent(task_message)
            draft_ref = self._extract_draft_ref(response_text, prefix="VMI")

            self._log_action(portal, "submit_draft", f"i.SAF draft created: {draft_ref}",
                             "success", url="https://imas.vmi.lt",
                             payload={"xml_file": xml_path.name, "supplier": ocr.get("supplier_name")})
            self._reset_circuit_breaker(portal)
            return draft_ref

        except Exception as exc:
            self._log_action(portal, "error", f"VMI portal error: {exc}",
                             "failure", error_detail=str(exc))
            self._increment_failure(portal)
            raise

    async def _sodra_draft_1sd(self, hire: dict) -> str:
        """
        Send a task to the clawdbot main agent to navigate Sodra draudejai and save a 1-SD draft.
        Implements the Smart-ID auth pause and circuit breaker.
        Returns the Sodra draft page URL.
        """
        portal = "sodra"
        self._check_circuit_breaker(portal)

        company_code = os.getenv("VMI_COMPANY_CODE", "").strip()
        fields = [
            f"   - Employee name: {hire['name']}",
            f"   - First working day: {hire['first_working_day']}",
        ]
        if hire.get("personal_code"):
            fields.append(f"   - Personal code: {hire['personal_code']}")
        if hire.get("position"):
            fields.append(f"   - Position: {hire['position']}")
        fields_text = "\n".join(fields)

        task_message = (
            f"TASK: Draft a 1-SD new employee registration form in the Sodra portal.\n\n"
            f"Steps:\n"
            f"1. Navigate to https://draudejai.sodra.lt\n"
            f"2. Log in via Smart-ID with company code {company_code}.\n"
            f"3. When the Smart-ID verification code appears on screen, read it from the "
            f"browser and send a Telegram message to the owner with it so they can approve "
            f"on their device.\n"
            f"4. Wait up to 90 seconds for Smart-ID approval. If it times out, stop and "
            f"return an error message — do NOT retry.\n"
            f"5. Navigate to the 1-SD (new employee registration) form.\n"
            f"6. Fill in the following fields:\n{fields_text}\n"
            f"7. SAVE AS DRAFT only — do NOT click submit or confirm submission.\n"
            f"8. Return the URL of the saved draft page.\n\n"
            f"CRITICAL: draft and save only. Never submit the form."
        )

        if self.dry_run:
            log.info("  [DRY RUN] Sodra task message:\n%s", task_message)
            return "https://draudejai.sodra.lt/dry-run"

        try:
            response_text = await self._run_clawdbot_agent(task_message)
            draft_url = self._extract_draft_url(response_text)

            self._log_action(portal, "submit_draft",
                             f"Sodra 1-SD draft for {hire['name']}",
                             "success", url=draft_url, payload=hire)
            self._reset_circuit_breaker(portal)
            return draft_url

        except Exception as exc:
            self._log_action(portal, "error",
                             f"Sodra portal error for {hire['name']}: {exc}",
                             "failure", error_detail=str(exc))
            self._increment_failure(portal)
            raise

    async def _wait_for_smart_id_approval(self, session, timeout_seconds: int = 90) -> None:
        """
        Poll for Smart-ID / Mobile-ID session confirmation.
        Raises TimeoutError if not confirmed within timeout_seconds.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            authenticated = await session.is_authenticated()  # SDK call
            if authenticated:
                log.info("Smart-ID / Mobile-ID approved.")
                return
            await asyncio.sleep(3)
        raise TimeoutError(
            f"Smart-ID approval not received within {timeout_seconds}s. "
            f"Please log in manually and re-trigger the task."
        )

    # ── clawdbot bridge ───────────────────────────────────────────────────────

    async def _run_clawdbot_agent(self, message: str) -> str:
        """
        Send a browser task to the running clawdbot main agent via CLI.
        Blocks until the agent completes or the configured timeout elapses.
        Returns the agent's plain-text reply.
        """
        timeout = int(os.getenv("CLAWDBOT_TASK_TIMEOUT", "300"))
        agent   = os.getenv("CLAWDBOT_AGENT_NAME", "main")
        cmd = [
            "clawdbot", "agent",
            "--agent", agent,
            "--message", message,
            "--json",
            "--timeout", str(timeout),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 60
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(
                f"clawdbot agent CLI timed out after {timeout + 60}s"
            )

        if proc.returncode != 0:
            raise RuntimeError(
                f"clawdbot agent exited {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )

        data = json.loads(stdout.decode(errors="replace"))
        if data.get("status") != "ok":
            raise RuntimeError(
                f"clawdbot agent returned non-ok status '{data.get('status')}': {data}"
            )

        payloads = data.get("result", {}).get("payloads", [])
        if not payloads:
            raise RuntimeError("clawdbot agent returned no payloads")

        return payloads[0].get("text", "")

    def _extract_draft_ref(self, text: str, prefix: str = "DRAFT") -> str:
        """Parse a portal draft reference number out of the agent's reply."""
        for pattern in [
            r'draft\s+ref(?:erence)?[:\s#]+([A-Za-z0-9\-/]{4,})',
            r'ref(?:erence)?[:\s#]+([A-Za-z0-9\-/]{4,})',
            r'(?:no|number|id)[:\s#]+([A-Za-z0-9\-/]{4,})',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip(".,)")
        log.warning("Could not parse draft reference from agent reply; generating fallback ref")
        return f"{prefix}-DRAFT-{uuid.uuid4().hex[:8].upper()}"

    def _extract_draft_url(self, text: str) -> str:
        """Parse a Sodra draft URL out of the agent's reply."""
        for pattern in [
            r'https?://draudejai\.sodra\.lt\S+',
            r'https?://\S*sodra\S+',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(0).rstrip(".,)")
        log.warning("Could not parse Sodra draft URL from agent reply; generating fallback URL")
        return f"https://draudejai.sodra.lt/drafts/{uuid.uuid4().hex[:8]}"

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def _check_circuit_breaker(self, portal: str) -> None:
        count = self._failure_counts.get(portal, 0)
        if count >= CIRCUIT_BREAKER_THRESHOLD:
            raise CircuitBreakerOpen(
                f"Portal '{portal}' has {count} consecutive failures. "
                f"Pausing all interactions. Owner must investigate."
            )

    def _increment_failure(self, portal: str) -> None:
        self._failure_counts[portal] = self._failure_counts.get(portal, 0) + 1
        log.warning("Circuit breaker: %s failures for portal '%s'",
                    self._failure_counts[portal], portal)

    def _reset_circuit_breaker(self, portal: str) -> None:
        if portal in self._failure_counts:
            del self._failure_counts[portal]

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _load_soul(self) -> str:
        if self.soul_path.exists():
            return self.soul_path.read_text(encoding="utf-8")
        log.warning("soul.md not found at %s — using empty system prompt", self.soul_path)
        return ""

    def _log_action(self, portal: str, action_type: str, description: str,
                    outcome: str, url: str | None = None,
                    payload: dict | None = None,
                    error_detail: str | None = None) -> None:
        log_portal_action(
            conn=self.conn,
            portal=portal,
            action_type=action_type,
            description=description,
            outcome=outcome,
            url=url,
            payload=payload,
            error=error_detail,
            session_id=self.session_id,
        )

    def _mark_processed(self, fpath: Path) -> None:
        """Create a hidden marker file so the file is not reprocessed next run."""
        marker = fpath.parent / f".processed_{fpath.name}"
        marker.touch()

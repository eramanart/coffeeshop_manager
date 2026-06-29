"""
agent/runner.py — package entry point.

Re-exports OpenClawAgent from the root runner module and provides
start_openclaw_agent(), the async entry point called by main_gateway_loop_.py.
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

from runner import OpenClawAgent, CircuitBreakerOpen, TASK_REGISTRY  # noqa: F401

log = logging.getLogger("agent.runner")

# Project root is three levels up from this file (python/agent/runner.py)
_PROJECT_ROOT = Path(__file__).parent.parent.parent


async def start_openclaw_agent(context: dict) -> None:
    """
    Initialise the OpenClawAgent and dispatch any tasks implied by context.

    Context keys:
        audit   — result dict from core.accounting.run_daily_audit()
        stock   — result dict from core.inventory.check_stock_levels()
        workspace — path string for output files
    """
    from dotenv import load_dotenv
    from migrate import apply_migrations, get_connection

    load_dotenv(_PROJECT_ROOT / "settings.env")

    api_key   = os.getenv("OPENCLAW_API_KEY", "")
    dry_run   = os.getenv("DRY_RUN", "true").lower() == "true"
    db_path   = Path(os.getenv("DB_PATH", "data/memory.sqlite"))
    soul_path = _PROJECT_ROOT / "soul.md"
    workspace = Path(os.getenv("WORKSPACE_PATH", "data/workspace"))

    workspace.mkdir(parents=True, exist_ok=True)

    apply_migrations(db_path)
    conn = get_connection(db_path)

    agent = OpenClawAgent(
        api_key=api_key,
        soul_path=soul_path,
        db_conn=conn,
        session_id=uuid.uuid4().hex,
        dry_run=dry_run,
    )

    tasks = []

    if context.get("receipts"):
        tasks.append({"task": "process_receipts", "files": context["receipts"]})

    if context.get("new_hires"):
        tasks.append({"task": "register_new_hires", "hires": context["new_hires"]})

    for item in context.get("stock", {}).get("low_stock", []):
        tasks.append({"task": "draft_purchase_order", "item": item})

    if not tasks:
        log.info("No agent tasks required this cycle.")
    else:
        log.info("Running %d agent task(s)...", len(tasks))
        for task in tasks:
            result = await agent.run_task(task)
            log.info("Task %s → %s", task["task"], result.get("status"))

    conn.close()

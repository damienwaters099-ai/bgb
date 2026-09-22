"""
EFACS JSON export — phase 1 integration point.
==================================================
BGB's phase-1 ask (from their email): opportunities and brush enquiries logged
in this app need to be "logged directly into EFACS (json file generation)".
This module does exactly that — every time a new opportunity, an opportunity
activity, or a brush enquiry is saved, it also drops a JSON file into
EFACS_EXPORT_DIR, shaped by the field maps below.

WHAT'S STILL OPEN (blocked on BGB's mapping document, in progress on their side):
  - The exact EFACS field codes each app field should be written under. Right
    now the JSON keys are just this app's own field names — update
    OPPORTUNITY_FIELD_MAP / ENQUIRY_FIELD_MAP below once the mapping doc
    lands. Nothing else in this file, or in main.py, needs to change.
  - How EFACS actually ingests these files — a watched folder, SFTP drop, or a
    scheduled import job someone on BGB's side points at EFACS_EXPORT_DIR.
    That decides where EFACS_EXPORT_DIR should physically point, and whether
    this needs to become an HTTP POST instead of a file drop.

RENDER DISK WARNING — same caveat as persistence.py: local disk on Render's
standard plan is ephemeral and does NOT survive a redeploy. Files written here
will look fine in testing and then quietly go missing in production unless
EFACS_EXPORT_DIR points at a mounted persistent disk, or the mapping doc
settles on a push (HTTP/API) model instead of a file drop. Flag this before
relying on it beyond a pilot.

Toggle with EFACS_EXPORT_ENABLED=false to turn off. Default is on — this only
adds files, it never touches the app's own opportunity/enquiry JSON logs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

EFACS_EXPORT_DIR = Path(os.getenv("EFACS_EXPORT_DIR", Path(__file__).resolve().parent / "efacs_outbox"))
EFACS_EXPORT_ENABLED = os.getenv("EFACS_EXPORT_ENABLED", "true").strip().lower() != "false"

# TODO: replace the values (EFACS field codes) once BGB's mapping doc lands —
# keys (this app's own field names) should stay as-is.
OPPORTUNITY_FIELD_MAP: dict[str, str] = {
    "id": "id",
    "timestamp": "timestamp",
    "logged_by": "logged_by",
    "opportunity_name": "opportunity_name",
    "customer_name": "customer_name",
    "customer_code": "customer_code",
    "is_existing_customer": "is_existing_customer",
    "assigned_to": "assigned_to",
    "prospect_stage": "prospect_stage",
    "details": "details",
    "target_close_date": "target_close_date",
    "probability_label": "probability_label",
    "probability_pct": "probability_pct",
    "currency": "currency",
    "total_value": "total_value",
    "total_net_margin_pct": "total_net_margin_pct",
    "total_margin_on_material_pct": "total_margin_on_material_pct",
    "in_year_revenue": "in_year_revenue",
    "in_year_net_margin_pct": "in_year_net_margin_pct",
    "in_year_margin_on_material_pct": "in_year_margin_on_material_pct",
    "ongoing_revenue": "ongoing_revenue",
    "ongoing_net_margin_pct": "ongoing_net_margin_pct",
    "ongoing_margin_on_material_pct": "ongoing_margin_on_material_pct",
    "activities": "activities",
}

ENQUIRY_FIELD_MAP: dict[str, str] = {
    "id": "id",
    "timestamp": "timestamp",
    "logged_by": "logged_by",
    "sales_rep": "sales_rep",
    "bgb_company": "bgb_company",
    "customer_name": "customer_name",
    "trader_id": "trader_id",
    "customer_order_reference": "customer_order_reference",
    "is_existing_customer": "is_existing_customer",
    "customer_type": "customer_type",
    "wind_farm_ownership": "wind_farm_ownership",
    "lines": "lines",
    "total_qty": "total_qty",
    "current_supplier": "current_supplier",
    "timeline": "timeline",
    "score": "score",
    "band": "band",
    "routing": "routing",
    "notes": "notes",
    "pipeline_value_usd": "pipeline_value_usd",
}


def _write(record_type: str, record_id: str, mapped: dict[str, Any]) -> None:
    if not EFACS_EXPORT_ENABLED:
        return
    EFACS_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = EFACS_EXPORT_DIR / f"{record_type}_{record_id}_{stamp}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapped, f, indent=2, default=str)


def _map(record: dict[str, Any], field_map: dict[str, str]) -> dict[str, Any]:
    return {efacs_key: record.get(app_key) for app_key, efacs_key in field_map.items()}


def export_opportunity(entry: dict[str, Any]) -> None:
    """Call after every opportunity insert or activity-log mutation — writes
    the opportunity's full current state (activities included) as one file."""
    _write("opportunity", entry.get("id", "unknown"), _map(entry, OPPORTUNITY_FIELD_MAP))


def export_enquiry(entry: dict[str, Any]) -> None:
    """Call after every brush enquiry insert."""
    _write("enquiry", entry.get("id", "unknown"), _map(entry, ENQUIRY_FIELD_MAP))

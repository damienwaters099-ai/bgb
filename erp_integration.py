"""
ERP integration layer — placeholder for the developer.
=========================================================

WHAT THIS FILE IS
------------------
Today, main.py loads all reference data (brush catalogue, silver/copper price
lists, stock levels, customer masters, sales reps, opportunity dropdowns)
straight from static JSON files sitting in the repo. That's fine for a pilot,
but it means every change (a new part, a stock level, a new customer) has to
be edited into a JSON file and redeployed by hand.

This file defines ONE interface (`ERPClient`) that main.py will call instead
of reading JSON files directly. Two implementations are provided:

  - MockERPClient   — reads the exact same JSON files as today. Drop-in,
                       zero behaviour change. This is what runs until the
                       real EFACS connection is built.
  - LiveEFACSClient  — placeholder for the real thing. Every method raises
                       NotImplementedError with a comment showing roughly
                       what needs to happen. This is what you (developer)
                       need to fill in.

Which one runs is chosen by an environment variable:

    ERP_MODE=mock   (default — same as today)
    ERP_MODE=live   (uses LiveEFACSClient — needs the TODOs below filled in)

WHAT'S STILL OPEN (needs EFACS admin / IT input before LiveEFACSClient can
be written for real):
  1. Base URL and environment (prod / test) for the EFACS API or reporting
     DB — is there a REST API, an OData feed, a direct DB connection, or a
     scheduled export/import (e.g. a shared folder of CSVs) already in use
     elsewhere at BGB?
  2. Auth method — API key, OAuth client credentials, service account +
     DB login, VPN-only access, etc.
  3. Field mapping — EFACS field names/codes for: part number, stock qty,
     unit price/price breaks, customer trader ID, sales rep, currency. The
     placeholder methods below list exactly what field each one needs to
     return so this is a fill-in-the-blanks job once that's known.
  4. Refresh strategy — pull live on every request (simple, slower, always
     current) vs. a background refresh every N minutes into a local cache
     (faster, needs a scheduler). Recommend starting with a cache — see the
     `_ttl_cache` helper stubbed below.

NOTE — get_customers() in LiveEFACSClient is already wired up for real SQL
Server access via pyodbc (see EFACS_SQL_* env vars) — only the table/column
names and the connection credentials themselves are still placeholders.
Everything else below is still fully unimplemented pending a decision on
API vs DB vs scheduled export.

HOW TO WIRE THIS INTO main.py
------------------------------
Replace this block in main.py (around the "brush" data loading section):

    CATALOGUE = _load_json("catalogue_data.json")
    BRUSHES = CATALOGUE.get("brushes", [])
    REFERENCES = CATALOGUE.get("references", [])
    SILVER_BRUSHES = _load_json("silver_catalogue.json", [])
    COPPER_BRUSHES = _load_json("copper_catalogue.json", [])
    CUSTOMERS_V2 = _load_json("customers_v2.json", [])
    SALES_REPS = _load_json("sales_reps.json", [])
    STOCK_MAP = _load_json("stock_map.json", {})

with:

    from erp_integration import get_erp_client
    erp = get_erp_client()
    BRUSHES = erp.get_brush_catalogue()
    REFERENCES = erp.get_references()
    SILVER_BRUSHES = erp.get_silver_price_list()
    COPPER_BRUSHES = erp.get_copper_price_list()
    CUSTOMERS_V2 = erp.get_customers()
    SALES_REPS = erp.get_sales_reps()
    STOCK_MAP = erp.get_stock_levels()

Same pattern for the Opportunities customer/dropdown data (currently
_load_opp_json("customers.json") etc. — see `get_opportunity_dropdowns()`
below). Everything downstream of these variables (search, pricing,
margin calcs) is untouched — this only changes where the data comes from.

NOTE — there are currently TWO separate customer lists in the app:
customers.json (used by Opportunities) and customers_v2.json (used by the
Brush tool). Worth deciding with BGB whether EFACS should feed one shared
customer list into both, rather than keeping two.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default if default is not None else []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class ERPClient(ABC):
    """Everything main.py needs from the ERP. One method per dataset —
    each returns exactly the shape the app already expects (same as the
    current JSON files), so swapping implementations needs no other
    code changes."""

    @abstractmethod
    def get_brush_catalogue(self) -> list[dict]:
        """List of brush master-list records (non-priced parts).
        Fields used downstream: spc_part_no, app_description,
        efacs_description, bgb_grade, oe_ref, turbine_oem, brush_type,
        material, dimensions."""

    @abstractmethod
    def get_references(self) -> list[dict]:
        """Cross-reference table: title, oem_part, purchased_part,
        saleable_part, bgb_description, bgb_grade, source_grade."""

    @abstractmethod
    def get_silver_price_list(self) -> list[dict]:
        """Silver-brush priced catalogue. Fields used downstream:
        bgb_ref, gerken_ref, oem_ref, turbine, grade, description,
        option, status, price_breaks_usd / price_breaks_eur (each a list
        of {qty, j_slope, k_intercept, list_price})."""

    @abstractmethod
    def get_copper_price_list(self) -> list[dict]:
        """Copper-brush priced catalogue. Same shape as silver, but
        price_breaks (list of {qty, price_eur}) instead of price_breaks_usd/eur."""

    @abstractmethod
    def get_stock_levels(self) -> dict:
        """Dict keyed by part number -> stock record (at minimum a
        quantity-on-hand field; the app's stock_map.json today also
        carries lead time / notes per part — keep that shape if EFACS
        has it). THIS is the dataset that most needs to be live —
        everything else changes rarely, stock changes daily."""

    @abstractmethod
    def get_customers(self) -> list[dict]:
        """Brush-tool customer master. Fields used downstream: name,
        trader_id."""

    @abstractmethod
    def get_sales_reps(self) -> list[dict]:
        """Fields used downstream: name, bgb_entity."""

    @abstractmethod
    def get_opportunity_dropdowns(self) -> dict:
        """Returns {"customers": [...], "assigned_to": [...],
        "prospect_stages": [...], "probability": [...], "currencies": [...]}
        — same shape as opportunities/data/*.json today."""


class MockERPClient(ERPClient):
    """Reads today's static JSON files. This is the default — behaviour
    is identical to the app as it stands now. Safe to ship; nothing here
    needs EFACS access."""

    def __init__(self, brush_dir: Path | None = None, opp_dir: Path | None = None):
        self.brush_dir = brush_dir or (BASE_DIR / "brush")
        self.opp_dir = opp_dir or (BASE_DIR / "opportunities" / "data")

    def get_brush_catalogue(self) -> list[dict]:
        catalogue = _load_json(self.brush_dir / "catalogue_data.json", {})
        return catalogue.get("brushes", [])

    def get_references(self) -> list[dict]:
        catalogue = _load_json(self.brush_dir / "catalogue_data.json", {})
        return catalogue.get("references", [])

    def get_silver_price_list(self) -> list[dict]:
        return _load_json(self.brush_dir / "silver_catalogue.json", [])

    def get_copper_price_list(self) -> list[dict]:
        return _load_json(self.brush_dir / "copper_catalogue.json", [])

    def get_stock_levels(self) -> dict:
        return _load_json(self.brush_dir / "stock_map.json", {})

    def get_customers(self) -> list[dict]:
        return _load_json(self.brush_dir / "customers_v2.json", [])

    def get_sales_reps(self) -> list[dict]:
        return _load_json(self.brush_dir / "sales_reps.json", [])

    def get_opportunity_dropdowns(self) -> dict:
        return {
            "customers": _load_json(self.opp_dir / "customers.json", []),
            "assigned_to": _load_json(self.opp_dir / "assigned_to.json", []),
            "prospect_stages": _load_json(self.opp_dir / "prospect_stages.json", []),
            "probability": _load_json(self.opp_dir / "probability.json", []),
            "currencies": _load_json(self.opp_dir / "currencies.json", []),
        }


def _ttl_cache(seconds: int):
    """Tiny decorator: cache a no-arg method's result for `seconds`, then
    refetch. Use this in LiveEFACSClient once real calls are wired up, so
    every page load doesn't hit EFACS directly. Not needed for MockERPClient."""
    def decorator(fn):
        state = {"value": None, "fetched_at": 0.0}

        def wrapper(self):
            now = time.monotonic()
            if state["value"] is None or (now - state["fetched_at"]) > seconds:
                state["value"] = fn(self)
                state["fetched_at"] = now
            return state["value"]
        return wrapper
    return decorator


class LiveEFACSClient(ERPClient):
    """PLACEHOLDER for most datasets — every method below still needs a real
    implementation once we have the EFACS API/DB connection details, auth,
    and field mapping (see the module docstring). Left as NotImplementedError
    on purpose — do not silently fall back to mock data from here; that
    hides the gap.

    get_customers() is wired up for real (SQL Server via pyodbc), because
    that's the one dataset the client confirmed is a direct DB lookup. It's
    still blocked on: the actual table/column names (placeholders below,
    swap once confirmed) and the connection details themselves (env vars,
    filled in once IT provisions a service account / VPN access).

    Everything else here still needs an approach decided (REST API vs DB
    vs scheduled export) before it can be written the same way.
    """

    def __init__(self):
        self.base_url = os.getenv("EFACS_BASE_URL", "")
        self.api_key = os.getenv("EFACS_API_KEY", "")
        # SQL Server connection for get_customers() — separate from the
        # REST-style vars above since BGB's customer lookup is a direct DB read.
        self.sql_host = os.getenv("EFACS_SQL_HOST", "")
        self.sql_port = os.getenv("EFACS_SQL_PORT", "1433")
        self.sql_database = os.getenv("EFACS_SQL_DATABASE", "")
        self.sql_user = os.getenv("EFACS_SQL_USER", "")
        self.sql_password = os.getenv("EFACS_SQL_PASSWORD", "")
        self.sql_driver = os.getenv("EFACS_SQL_DRIVER", "{ODBC Driver 18 for SQL Server}")

    @_ttl_cache(seconds=900)  # 15 min — adjust once we know how EFACS behaves under load
    def get_brush_catalogue(self) -> list[dict]:
        # TODO: e.g. GET {base_url}/parts?category=brush  -> map to the field
        # names listed on ERPClient.get_brush_catalogue's docstring.
        raise NotImplementedError("TODO: implement live EFACS brush catalogue pull")

    def get_references(self) -> list[dict]:
        raise NotImplementedError("TODO: implement live EFACS cross-reference pull")

    @_ttl_cache(seconds=900)
    def get_silver_price_list(self) -> list[dict]:
        raise NotImplementedError("TODO: implement live EFACS silver price-list pull")

    @_ttl_cache(seconds=900)
    def get_copper_price_list(self) -> list[dict]:
        raise NotImplementedError("TODO: implement live EFACS copper price-list pull")

    @_ttl_cache(seconds=300)  # stock should refresh more often than pricing
    def get_stock_levels(self) -> dict:
        # TODO: this is the highest-value live link — stock goes stale fastest.
        raise NotImplementedError("TODO: implement live EFACS stock-level pull")

    def _sql_connect(self):
        """One connection per call — fine at pilot volumes. If this becomes
        a bottleneck, switch to a pooled connection instead."""
        import pyodbc  # imported lazily so the app still runs in ERP_MODE=mock
        if not all([self.sql_host, self.sql_database, self.sql_user, self.sql_password]):
            raise RuntimeError(
                "ERP_MODE=live customer lookup needs EFACS_SQL_HOST / EFACS_SQL_DATABASE / "
                "EFACS_SQL_USER / EFACS_SQL_PASSWORD set as environment variables — "
                "never commit them to a file."
            )
        conn_str = (
            f"DRIVER={self.sql_driver};SERVER={self.sql_host},{self.sql_port};"
            f"DATABASE={self.sql_database};UID={self.sql_user};PWD={self.sql_password};"
            "Encrypt=yes;TrustServerCertificate=no;"
        )
        return pyodbc.connect(conn_str, timeout=10)

    @_ttl_cache(seconds=300)
    def get_customers(self) -> list[dict]:
        # TODO: swap table/column names below once BGB confirms the customer
        # master's real table name and field names — everything else
        # (connection, cursor handling, shaping into dicts) is already wired.
        query = "SELECT customer_name, trader_id FROM dbo.CustomerMaster"
        with self._sql_connect() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_sales_reps(self) -> list[dict]:
        raise NotImplementedError("TODO: implement live EFACS sales rep pull")

    def get_opportunity_dropdowns(self) -> dict:
        raise NotImplementedError("TODO: implement live EFACS dropdown/reference-data pull")


def get_erp_client() -> ERPClient:
    """Single switch point. ERP_MODE env var controls it — defaults to
    mock so nothing breaks until the live client is actually finished
    and deliberately turned on."""
    mode = os.getenv("ERP_MODE", "mock").strip().lower()
    if mode == "live":
        return LiveEFACSClient()
    return MockERPClient()

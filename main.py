"""
BGB CRM — unified app combining:
  - Opportunities (AI-assisted company check + qualify, plus manual "Log opportunity")
  - BGB Brush (catalogue search, silver/copper pricing, enquiry logging, stock admin)
  - Total Cost of Ownership Calculator (Downtime cost, Wind farm lookup, Cost of ownership)

Single deployment, single set of credentials. See README.md for what's real vs
placeholder, and for the sources behind the Downtime Cost Calculator's defaults.
"""
import os, json, uuid, csv, io, secrets, time, sqlite3
import httpx
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="BGB CRM")
security = HTTPBasic()

# ---- ERP integration point -------------------------------------------
# See erp_integration.py. ERP_MODE=mock (default) reads today's JSON
# files; ERP_MODE=live reads from EFACS once that client is built.
from erp_integration import get_erp_client
from persistence import load_records, save_records
from efacs_export import export_opportunity, export_enquiry
erp = get_erp_client()

APP_USERNAME = os.getenv("APP_USERNAME", "bgb")
APP_PASSWORD = os.getenv("APP_PASSWORD", "bgb2026")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "bgbadmin2026")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


def verify_password(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, APP_USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Incorrect credentials",
                             headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Admin credentials required",
                             headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def read_html(*parts) -> str:
    with open(BASE_DIR.joinpath(*parts), "r") as f:
        return f.read()


# =====================================================================
# HOME
# =====================================================================

@app.get("/", response_class=HTMLResponse)
async def home(username: str = Depends(verify_password)):
    return read_html("home.html")


# =====================================================================
# OPPORTUNITIES  (/opportunities/*)
# =====================================================================

opp_router = APIRouter(prefix="/opportunities", tags=["opportunities"])

BGB_COMPANY_PROMPT = """You are a sales intelligence analyst for BGB Group (bgbinnovation.com), a UK-based rotary transmission engineering and manufacturing company.

BGB products: Slip rings, FORJs (Fibre Optic Rotary Joints), Carbon brush holders & brush gear, Rotary unions, Lightning protection systems.
BGB markets: Wind energy (EU market leader), Radar & defence, Marine & subsea, Medical/scientific, Industrial automation, Wastewater/utilities, Aerospace.

Respond ONLY with a JSON object, no markdown:
{
  "company": "...",
  "sector": "...",
  "description": "1-2 sentence description",
  "bgb_products": ["product1"],
  "growth_potential": "High|Medium|Low|Drain",
  "rationale": "2-3 sentences",
  "red_flags": "concerns or null",
  "is_competitor": true/false,
  "is_distributor": true/false
}"""

BGB_QUALIFY_PROMPT = """You are a senior commercial analyst for BGB Group, a specialist rotary transmission manufacturer (slip rings, FORJs, brush gear, rotary unions, lightning protection).

You will receive structured enquiry data and must produce a qualification assessment.

Scoring weights:
- Budget confirmed: +25 points
- Decision maker involved: +20 points
- Clear technical requirement: +15 points
- Prior BGB contact / existing relationship: +15 points
- Reasonable timeline (not absurdly urgent or vague): +10 points
- Referral or inbound (not cold): +10 points
- Volume meaningful for BGB (not one-off tiny): +10 points
- Repair/replacement (known need): +10 points
- Custom/new design (higher value but more risk): +5 points
- Deductions: price fishing -20, competitor intel suspected -30, no budget -15, vague requirement -10, single unit prototype only -5

Score 0-100. Band: 75-100 = Hot, 50-74 = Warm, 25-49 = Cool, 0-24 = Cold.

Respond ONLY with JSON, no markdown:
{
  "score": 0-100,
  "band": "Hot|Warm|Cool|Cold",
  "key_positives": ["up to 3 short bullet points"],
  "key_risks": ["up to 3 short bullet points"],
  "price_fishing_suspected": true/false,
  "competitor_intel_suspected": true/false,
  "recommended_action": "one clear sentence on what to do next",
  "routing": "Nick Hubbard|Ben Murphy|Paul Hussein|Engineering Review|Decline",
  "routing_reason": "one sentence why"
}"""

# Both logs now load from and save to JSON files on disk (see
# persistence.py) instead of living in memory only, so a restart doesn't
# wipe the pipeline / enquiry history.
opportunity_log = load_records("opportunity_checks")       # AI-assisted "Opportunity & customer check" log
manual_opportunity_log = load_records("opportunities")      # manual "Log opportunity" pipeline — real fields, from Opportunity Information.xlsx

OPP_DATA_DIR = BASE_DIR / "opportunities" / "data"


def _load_opp_json(name):
    with open(OPP_DATA_DIR / name) as f:
        return json.load(f)


# Opportunities' customer master now comes through the same ERP client call
# as the Brush tool's — one shared customer list, per BGB's decision that
# the EFACS customer master feeds every part of the app, not two separate lists.
_opp_dropdowns = erp.get_opportunity_dropdowns()
OPP_CUSTOMERS = _opp_dropdowns["customers"]
OPP_CUSTOMERS_BY_NAME = {c["name"].upper(): c for c in OPP_CUSTOMERS}
OPP_ASSIGNED_TO = _opp_dropdowns["assigned_to"]
OPP_PROSPECT_STAGES = _opp_dropdowns["prospect_stages"]
OPP_PROBABILITY = _opp_dropdowns["probability"]
OPP_CURRENCIES = _opp_dropdowns["currencies"]
OPP_ACTIVITY_TYPES = _load_opp_json("activity_types.json")

# Fields flagged "Goes into EFAC's? = Yes" in Opportunity Information.xlsx — these are
# the columns the EFACS CSV export includes. Cost of Materials / NRE Hours Cost /
# Prototype Hours Cost (and their In Year / On-going equivalents) are "No" — internal
# margin inputs only, never exported.
EFACS_EXPORT_FIELDS = [
    ("opportunity_name", "Opportunity Name"),
    ("customer_name", "Customer Name"),
    ("assigned_to", "Assigned to"),
    ("prospect_stage", "Prospect Stage"),
    ("details", "Details"),
    ("target_close_date", "Target close date"),
    ("probability_label", "Probability"),
    ("currency", "Currency"),
    ("total_value", "Total Opportunity Value"),
    ("total_net_margin_pct", "Total Opportunity Net Margin %"),
    ("total_margin_on_material_pct", "Total Opportunity Margin on Material %"),
    ("in_year_revenue", "In Year Revenue"),
    ("in_year_net_margin_pct", "In Year Opportunity Net Margin %"),
    ("in_year_margin_on_material_pct", "In Year Opportunity Margin on Material %"),
    ("ongoing_revenue", "On-going Revenue"),
    ("ongoing_net_margin_pct", "On-going Opportunity Net Margin %"),
    ("ongoing_margin_on_material_pct", "On-going Opportunity Margin on Material %"),
    ("activities_summary", "Activities"),
]


def _margins(value, cost_materials, nre_cost, prototype_cost):
    """Per the Calculations tab of Opportunity Information.xlsx:
      Net Margin %          = (Value - Cost of Materials - NRE - Prototype) / Value
      Margin on Material %  = (Value - Cost of Materials) / Value
    Returns (net_margin_pct, margin_on_material_pct), rounded, or (None, None) if value is 0."""
    if not value:
        return None, None
    net_margin_value = value - (cost_materials or 0) - (nre_cost or 0) - (prototype_cost or 0)
    margin_on_material_value = value - (cost_materials or 0)
    return (
        round(net_margin_value / value * 100, 1),
        round(margin_on_material_value / value * 100, 1),
    )


async def call_anthropic(system: str, user_msg: str, use_search: bool = False) -> str:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if use_search:
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        headers["anthropic-beta"] = "web-search-2025-03-05"

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {resp.text}")

    data = resp.json()
    return "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")


@opp_router.get("", response_class=HTMLResponse)
@opp_router.get("/", response_class=HTMLResponse)
async def opportunities_home(username: str = Depends(verify_password)):
    return read_html("opportunities", "hub.html")


@opp_router.get("/check", response_class=HTMLResponse)
async def opportunities_check(username: str = Depends(verify_password)):
    return read_html("opportunities", "opportunities.html")


@opp_router.get("/new", response_class=HTMLResponse)
async def opportunities_new(username: str = Depends(verify_password)):
    return read_html("opportunities", "new_opportunity.html")


@opp_router.post("/research-company")
async def research_company(request: Request, username: str = Depends(verify_password)):
    body = await request.json()
    company = body.get("company", "").strip()
    if not company:
        raise HTTPException(status_code=400, detail="Company name required")

    text = await call_anthropic(BGB_COMPANY_PROMPT,
        f"Research this company for BGB Group: {company}", use_search=True)

    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return JSONResponse(json.loads(clean))
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Failed to parse AI response")


@opp_router.post("/qualify")
async def qualify_opportunity(request: Request, username: str = Depends(verify_password)):
    body = await request.json()

    enquiry_text = f"""
Company: {body.get('company_name', 'Unknown')}
Sector: {body.get('sector', 'Unknown')}
Company growth potential: {body.get('growth_potential', 'Unknown')}
Company is competitor: {body.get('is_competitor', False)}
Company is distributor: {body.get('is_distributor', False)}

ENQUIRY DETAILS:
Product requested: {body.get('product', 'Not specified')}
Enquiry type: {body.get('enquiry_type', 'Not specified')}
Market/application: {body.get('market', 'Not specified')}
Technical requirement clarity: {body.get('tech_clarity', 'Not specified')}
Requirement description: {body.get('requirement_desc', '')}

Annual volume estimate: {body.get('annual_volume', 'Not specified')}
Budget confirmed: {body.get('budget_confirmed', 'No')}
Decision maker involved: {body.get('decision_maker', 'Unknown')}
Timeline: {body.get('timeline', 'Not specified')}

Prior BGB contact: {body.get('prior_contact', 'No')}
How they found BGB: {body.get('source', 'Not specified')}
Price sensitivity signals: {body.get('price_signals', 'None noted')}
Competitor mentioned: {body.get('competitor_mentioned', 'None')}
Additional notes: {body.get('notes', '')}
"""

    text = await call_anthropic(BGB_QUALIFY_PROMPT, f"Qualify this sales enquiry for BGB Group:\n{enquiry_text}")

    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(clean)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Failed to parse qualification response")

    log_entry = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().strftime("%d %b %Y %H:%M"),
        "logged_by": username,
        "company_name": body.get("company_name", "Unknown"),
        "sector": body.get("sector", ""),
        "product": body.get("product", ""),
        "enquiry_type": body.get("enquiry_type", ""),
        "market": body.get("market", ""),
        "score": result.get("score", 0),
        "band": result.get("band", "Cold"),
        "key_positives": result.get("key_positives", []),
        "key_risks": result.get("key_risks", []),
        "price_fishing_suspected": result.get("price_fishing_suspected", False),
        "competitor_intel_suspected": result.get("competitor_intel_suspected", False),
        "recommended_action": result.get("recommended_action", ""),
        "routing": result.get("routing", ""),
        "routing_reason": result.get("routing_reason", ""),
        "notes": body.get("notes", ""),
        "annual_volume": body.get("annual_volume", ""),
        "timeline": body.get("timeline", ""),
    }
    opportunity_log.insert(0, log_entry)
    return JSONResponse({"qualification": result, "log_entry": log_entry})


@opp_router.get("/log")
async def get_opportunity_log(username: str = Depends(verify_password)):
    return JSONResponse(opportunity_log)


@opp_router.get("/dropdowns")
async def opportunities_dropdowns(username: str = Depends(verify_password)):
    """All lookup lists for the New Opportunity form, sourced directly from
    Opportunity Information.xlsx — customers (1,283), assigned-to (11),
    prospect stage (6), probability (4), currency (3), activity type (15)."""
    return JSONResponse({
        "assigned_to": OPP_ASSIGNED_TO,
        "prospect_stages": OPP_PROSPECT_STAGES,
        "probability": OPP_PROBABILITY,
        "currencies": OPP_CURRENCIES,
        "activity_types": OPP_ACTIVITY_TYPES,
    })


@opp_router.get("/customer-lookup")
async def opportunities_customer_lookup(name: str, username: str = Depends(verify_password)):
    """Same pattern as the Brush tool's /brush/customer-lookup, against the
    1,283-customer list from Opportunity Information.xlsx (a different,
    Opportunities-specific customer master — not the Brush customer list)."""
    name = name.strip()
    if not name:
        return JSONResponse({"is_existing": False, "matched_name": "", "code": None, "suggestions": []})
    name_upper = name.upper()
    exact = OPP_CUSTOMERS_BY_NAME.get(name_upper)
    if exact:
        return JSONResponse({"is_existing": True, "matched_name": exact["name"], "code": exact["code"], "suggestions": []})
    suggestions = [c["name"] for c in OPP_CUSTOMERS if name_upper in c["name"].upper()][:8]
    return JSONResponse({"is_existing": False, "matched_name": name, "code": None, "suggestions": suggestions})


@opp_router.post("/log-opportunity")
async def log_opportunity(request: Request, username: str = Depends(verify_password)):
    """New Opportunity screen submit — fields per Opportunity Information.xlsx
    'Information Needed' tab. Margins are recomputed server-side (never trust
    the client's arithmetic) using the Calculations tab's formulas."""
    body = await request.json()

    opportunity_name = (body.get("opportunity_name") or "").strip()
    customer_name = (body.get("customer_name") or "").strip()
    if not opportunity_name or not customer_name:
        raise HTTPException(status_code=400, detail="Opportunity name and customer name are required")

    def num(key):
        try:
            return float(body.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    total_value = num("total_value")
    total_cost_materials = num("cost_materials")
    total_nre = num("nre_hours_cost")
    total_prototype = num("prototype_hours_cost")
    total_net_margin_pct, total_margin_on_material_pct = _margins(total_value, total_cost_materials, total_nre, total_prototype)

    in_year_revenue = num("in_year_revenue")
    in_year_cost_materials = num("in_year_cost_materials")
    in_year_nre = num("in_year_nre_hours_cost")
    in_year_prototype = num("in_year_prototype_hours_cost")
    in_year_net_margin_pct, in_year_margin_on_material_pct = _margins(in_year_revenue, in_year_cost_materials, in_year_nre, in_year_prototype)

    ongoing_revenue = num("ongoing_revenue")
    ongoing_cost_materials = num("ongoing_cost_materials")
    ongoing_nre = num("ongoing_nre_hours_cost")
    ongoing_prototype = num("ongoing_prototype_hours_cost")
    ongoing_net_margin_pct, ongoing_margin_on_material_pct = _margins(ongoing_revenue, ongoing_cost_materials, ongoing_nre, ongoing_prototype)

    probability_pct = None
    probability_label = body.get("probability_label", "")
    match = next((p for p in OPP_PROBABILITY if p["label"] == probability_label), None)
    if match:
        probability_pct = match["pct"]

    customer_record = OPP_CUSTOMERS_BY_NAME.get(customer_name.upper())

    entry = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().strftime("%d %b %Y %H:%M"),
        "logged_by": username,
        "opportunity_name": opportunity_name,
        "customer_name": customer_name,
        "customer_code": customer_record["code"] if customer_record else None,
        "is_existing_customer": customer_record is not None,
        "assigned_to": body.get("assigned_to", ""),
        "prospect_stage": body.get("prospect_stage", ""),
        "details": body.get("details", ""),
        "target_close_date": body.get("target_close_date", ""),
        "probability_label": probability_label,
        "probability_pct": probability_pct,
        "currency": body.get("currency", ""),
        "total_value": total_value,
        "cost_materials": total_cost_materials,
        "nre_hours_cost": total_nre,
        "prototype_hours_cost": total_prototype,
        "total_net_margin_pct": total_net_margin_pct,
        "total_margin_on_material_pct": total_margin_on_material_pct,
        "in_year_revenue": in_year_revenue,
        "in_year_cost_materials": in_year_cost_materials,
        "in_year_nre_hours_cost": in_year_nre,
        "in_year_prototype_hours_cost": in_year_prototype,
        "in_year_net_margin_pct": in_year_net_margin_pct,
        "in_year_margin_on_material_pct": in_year_margin_on_material_pct,
        "ongoing_revenue": ongoing_revenue,
        "ongoing_cost_materials": ongoing_cost_materials,
        "ongoing_nre_hours_cost": ongoing_nre,
        "ongoing_prototype_hours_cost": ongoing_prototype,
        "ongoing_net_margin_pct": ongoing_net_margin_pct,
        "ongoing_margin_on_material_pct": ongoing_margin_on_material_pct,
        "activities": [],
    }
    manual_opportunity_log.insert(0, entry)
    save_records("opportunities", manual_opportunity_log)
    export_opportunity(entry)
    return JSONResponse({"entry": entry})


@opp_router.get("/manual-log")
async def get_manual_opportunity_log(username: str = Depends(verify_password)):
    return JSONResponse(manual_opportunity_log)


ACTIVITY_TYPES = ["Meeting", "Email", "Phone call"]


@opp_router.post("/log-activity")
async def log_activity(request: Request, username: str = Depends(verify_password)):
    """Pipeline drawer 'Add activity' — appends a dated activity record to an
    existing logged opportunity. Activities are a list per opportunity (not a
    single scalar) so a pipeline entry can carry its full contact history."""
    body = await request.json()
    opportunity_id = (body.get("opportunity_id") or "").strip()
    activity_type = (body.get("activity_type") or "").strip()
    activity_date = (body.get("activity_date") or "").strip()
    detail = (body.get("detail") or "").strip()

    if activity_type not in ACTIVITY_TYPES:
        raise HTTPException(status_code=400, detail="Activity type must be Meeting, Email or Phone call")
    if not detail:
        raise HTTPException(status_code=400, detail="Detail is required")

    entry = next((e for e in manual_opportunity_log if e.get("id") == opportunity_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Opportunity not found — it may be from a previous session")

    activity = {
        "id": str(uuid.uuid4())[:8],
        "activity_type": activity_type,
        "activity_date": activity_date,
        "detail": detail,
        "logged_by": username,
        "timestamp": datetime.now().strftime("%d %b %Y %H:%M"),
    }
    entry.setdefault("activities", []).insert(0, activity)
    save_records("opportunities", manual_opportunity_log)
    export_opportunity(entry)
    return JSONResponse({"activity": activity, "entry": entry})


@opp_router.get("/export-opportunities-csv")
async def export_opportunities_csv(username: str = Depends(verify_password)):
    """CSV export restricted to the fields flagged 'Goes into EFAC's? = Yes' in
    Opportunity Information.xlsx — Cost of Materials / NRE / Prototype Hours
    Cost (and their In Year / On-going equivalents) stay internal-only."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in EFACS_EXPORT_FIELDS])
    for entry in manual_opportunity_log:
        row = dict(entry)
        activities = entry.get("activities") or []
        row["activities_summary"] = " | ".join(
            f"{a.get('activity_type','')} {a.get('activity_date','')}: {a.get('detail','')}".strip()
            for a in activities
        )
        writer.writerow([row.get(key, "") for key, _ in EFACS_EXPORT_FIELDS])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=bgb_opportunities_efacs_export_{datetime.now().strftime('%Y%m%d')}.csv"})


app.include_router(opp_router)


# =====================================================================
# BGB BRUSH  (/brush/*)
# =====================================================================

brush_router = APIRouter(prefix="/brush", tags=["brush"])
BRUSH_DIR = BASE_DIR / "brush"


def _load_json(name, default=None):
    try:
        with open(BRUSH_DIR / name) as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else {}


CATALOGUE = _load_json("catalogue_data.json")
CUSTOMERS = set(c.upper() for c in CATALOGUE.get("customers", []))
BRUSHES = erp.get_brush_catalogue()
REFERENCES = erp.get_references()

SILVER_BRUSHES = erp.get_silver_price_list()
COPPER_BRUSHES = erp.get_copper_price_list()
PRICING_META = _load_json("pricing_meta.json", {})

CUSTOMERS_V2 = erp.get_customers()
CUSTOMERS_BY_NAME = {c["name"].upper(): c for c in CUSTOMERS_V2}

SALES_REPS = erp.get_sales_reps()
SALES_REPS_BY_NAME = {r["name"]: r for r in SALES_REPS}

BRUSH_STATUS_MAP = _load_json("brush_status_map.json", {})
CARBEX_DATA = _load_json("carbex_prices.json", {})
CARBEX_BY_BGP = CARBEX_DATA.get("by_bgb_part", {})

STOCK_MAP = erp.get_stock_levels()

STOCK_OVERRIDES_PATH = BRUSH_DIR / "stock_overrides.json"
try:
    with open(STOCK_OVERRIDES_PATH) as f:
        _stock_overrides = json.load(f)
    for part, override in _stock_overrides.items():
        STOCK_MAP[part] = {**STOCK_MAP.get(part, {}), **override}
except FileNotFoundError:
    _stock_overrides = {}

MASTER_MAP = _load_json("master_map.json", {})
CUSTOM_IMAGES = _load_json("custom_images.json", {})

raw_xref = _load_json("cross_references_v2.json", {})
if isinstance(raw_xref, list):
    PART_EQUIVALENCE_MAP = {}
    BGB_TO_AFTERMARKET_MAP = {}
    for ref in raw_xref:
        bgb_part = ref.get("bgb_branded_part", "")
        if not bgb_part:
            continue
        for key, val in ref.get("all_equivalent_refs", {}).items():
            if val and str(val).upper() != "PENDING":
                PART_EQUIVALENCE_MAP[str(val).strip().upper()] = bgb_part
        aftermarket = ref.get("all_equivalent_refs", {}).get("Aftermarket", "")
        if aftermarket:
            BGB_TO_AFTERMARKET_MAP[bgb_part.upper()] = aftermarket
else:
    PART_EQUIVALENCE_MAP = {k.upper(): v for k, v in raw_xref.items()}
    BGB_TO_AFTERMARKET_MAP = {}

_aftermarket_override = _load_json("bgb_to_aftermarket_v2.json", None)
if _aftermarket_override:
    BGB_TO_AFTERMARKET_MAP = {k.upper(): v for k, v in _aftermarket_override.items()}

CARBEX_BY_BGB = {k.upper(): v for k, v in CARBEX_BY_BGP.items()}

# EXAMPLE DATA — these four values and the price lists in silver_catalogue.json /
# copper_catalogue.json are placeholders for this demo build, not BGB's real
# supplier pricing. Real values come from the live EFACS/supplier link once built.
GERKEN_LOCKED_EUR_KG = 1500.0
GERKEN_LOCKED_USD_KG = 1725.0
GERKEN_USD_EUR_FX = 1.15
GERKEN_STOCK_KG = 0.0
PRICE_LIST_DATE = "2026-01-01"
PRICE_LIST_REVISION = "DEMO-1"
VALID_STATUSES = {"Sellable", "Prototype", "In development"}


def _status_for_part(part_no: str) -> str:
    raw = BRUSH_STATUS_MAP.get((part_no or "").strip())
    if not raw:
        raw = BRUSH_STATUS_MAP.get((part_no or "").strip().upper())
    if raw in VALID_STATUSES:
        return raw
    if raw == "Yes":
        return "Sellable"
    return "Unknown"


def bgb_to_aftermarket_lookup(part_no: str) -> str:
    return BGB_TO_AFTERMARKET_MAP.get((part_no or "").strip().upper(), "")


def _stock_for_part(part_no: str) -> dict:
    entry = STOCK_MAP.get((part_no or "").strip())
    if entry is None:
        entry = STOCK_MAP.get((part_no or "").strip().upper())
    if entry:
        return {
            "free_stock": entry["free_stock"],
            "on_order": entry.get("on_order", 0),
            "last_refreshed": entry.get("last_refreshed", ""),
            "last_order_date": entry.get("last_order_date", ""),
        }
    return {"free_stock": None, "on_order": 0, "last_refreshed": "", "last_order_date": ""}


def _master_enrichment(part_no: str) -> dict:
    entry = MASTER_MAP.get((part_no or "").strip())
    if entry is None:
        entry = MASTER_MAP.get((part_no or "").strip().upper())
    if entry:
        return entry
    return {"oem_refs": [], "turbine_oem": "", "turbine_type": "", "brush_type": "", "material": ""}


def _drawing_url(part_no: str, price_list_image: str):
    custom = CUSTOM_IMAGES.get((part_no or "").strip())
    if custom:
        return f"/brush/drawings/{custom}"
    if price_list_image:
        return f"/brush/drawings/{price_list_image}"
    return None


_silver_rate_cache = {
    "rate_usd_kg": GERKEN_LOCKED_USD_KG,
    "market_rate_usd_kg": GERKEN_LOCKED_USD_KG,
    "source": f"Gerken locked rate (price list {PRICE_LIST_DATE} Rev {PRICE_LIST_REVISION})",
    "market_source": "Pending live fetch",
    "fetched_at": 0,
    "is_manual_override": False,
    "manual_override_rate": None,
}
CACHE_TTL_SECONDS = 4 * 60 * 60


async def fetch_live_silver_rate() -> dict:
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get("https://metals.live/api/v1/spot")
            if resp.status_code == 200:
                data = resp.json()
                silver_oz = None
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "silver" in item:
                            silver_oz = float(item["silver"]); break
                elif isinstance(data, dict):
                    silver_oz = float(data.get("silver", 0)) or None
                if silver_oz:
                    usd_kg = round(silver_oz * 32.1507, 2)
                    eur_kg = round(usd_kg / GERKEN_USD_EUR_FX, 2)
                    return {"ok": True, "eur_kg": eur_kg, "usd_kg": usd_kg, "source": "metals.live spot", "fetched_at": time.time()}
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get("https://www.gold-api.com/price/XAG")
            if resp.status_code == 200:
                data = resp.json()
                price_usd = data.get("price")
                if price_usd:
                    usd_kg = round(float(price_usd) * 32.1507, 2)
                    eur_kg = round(usd_kg / GERKEN_USD_EUR_FX, 2)
                    return {"ok": True, "eur_kg": eur_kg, "usd_kg": usd_kg, "source": "gold-api.com spot", "fetched_at": time.time()}
    except Exception:
        pass
    return {"ok": False}


UNKNOWN_CUSTOMER_TRADER_ID = "PRO005"
MARGIN_MULTIPLIER = 1.10


def _interpolate_price(breaks_sorted, qty, price_key):
    exact = next((b for b in breaks_sorted if b["qty"] == qty), None)
    if exact:
        return exact[price_key]
    if qty < breaks_sorted[0]["qty"]:
        return breaks_sorted[0][price_key]
    if qty > breaks_sorted[-1]["qty"]:
        return breaks_sorted[-1][price_key]
    lower = max((b for b in breaks_sorted if b["qty"] <= qty), key=lambda b: b["qty"])
    upper = min((b for b in breaks_sorted if b["qty"] >= qty), key=lambda b: b["qty"])
    if lower["qty"] == upper["qty"]:
        return lower[price_key]
    frac = (qty - lower["qty"]) / (upper["qty"] - lower["qty"])
    return lower[price_key] + frac * (upper[price_key] - lower[price_key])


enquiry_log = []
customer_enrichment = {}

app.mount("/brush/drawings", StaticFiles(directory=str(BRUSH_DIR / "drawings")), name="brush-drawings")


@brush_router.get("", response_class=HTMLResponse)
@brush_router.get("/", response_class=HTMLResponse)
async def brush_home(username: str = Depends(verify_password)):
    return read_html("brush", "hub.html")


@brush_router.get("/tool", response_class=HTMLResponse)
async def brush_tool(username: str = Depends(verify_password)):
    return read_html("brush", "brush.html")


# ── Admin: stock management ──

@brush_router.get("/admin", response_class=HTMLResponse)
async def admin_page(username: str = Depends(verify_admin)):
    all_parts = sorted(set(list(STOCK_MAP.keys())))
    for s in SILVER_BRUSHES:
        if s["bgb_ref"] not in all_parts:
            all_parts.append(s["bgb_ref"])
    for c in COPPER_BRUSHES:
        if c["bgb_ref"] not in all_parts:
            all_parts.append(c["bgb_ref"])
    all_parts.sort()

    rows = ""
    for part in all_parts:
        entry = STOCK_MAP.get(part, {})
        is_override = part in _stock_overrides
        free = entry.get("free_stock", 0) or 0
        on_order = entry.get("on_order", 0) or 0
        order_date = entry.get("last_order_date", "") or ""
        refreshed = entry.get("last_refreshed", "") or ""
        override_badge = '<span style="background:#fef9e7;color:#7d5a00;padding:1px 6px;border-radius:4px;font-size:0.7rem;font-weight:600">MANUAL</span>' if is_override else ""
        refreshed_short = refreshed[:10] if refreshed else "—"
        clear_btn = (
            '<button onclick="clearOverride(\'' + part + '\')" '
            'style="background:#fee2e2;color:#991b1b;border:none;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:0.82rem;margin-left:4px">Clear</button>'
        ) if is_override else ""
        rows += f"""<tr id="row-{part}">
          <td style="font-weight:500;white-space:nowrap">{part} {override_badge}</td>
          <td><input type="number" min="0" value="{free}" style="width:80px;padding:4px 6px;border:1.5px solid #d1d5db;border-radius:5px;font-size:0.85rem" id="free-{part}" /></td>
          <td><input type="number" min="0" value="{on_order}" style="width:80px;padding:4px 6px;border:1.5px solid #d1d5db;border-radius:5px;font-size:0.85rem" id="order-{part}" /></td>
          <td><input type="date" value="{order_date}" style="padding:4px 6px;border:1.5px solid #d1d5db;border-radius:5px;font-size:0.85rem" id="date-{part}" /></td>
          <td style="color:#9ca3af;font-size:0.75rem">{refreshed_short}</td>
          <td>
            <button onclick="savePart('{part}')" style="background:#0f3d2e;color:#fff;border:none;padding:4px 12px;border-radius:5px;cursor:pointer;font-size:0.82rem">Save</button>
            {clear_btn}
          </td>
        </tr>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BGB Brushes — Stock Admin</title>
<style>
* {{box-sizing:border-box;margin:0;padding:0}}
body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f0f2f5;min-height:100vh;font-size:14px}}
header {{background:#0f3d2e;padding:0.875rem 2rem;color:#fff;display:flex;align-items:center;gap:1rem;position:sticky;top:0;z-index:10}}
header h1 {{font-size:1rem;font-weight:600}}
header span {{color:#8fc2ac;font-size:0.8rem}}
.container {{max-width:1000px;margin:2rem auto;padding:0 1.5rem}}
.card {{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,0.08);overflow:hidden}}
.card-header {{padding:1rem 1.5rem;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;justify-content:space-between}}
.card-header h2 {{font-size:0.95rem;font-weight:600;color:#0f3d2e}}
.card-body {{padding:1.25rem 1.5rem}}
table {{width:100%;border-collapse:collapse}}
th {{background:#f0f5f2;color:#0f3d2e;padding:8px 12px;text-align:left;font-size:0.75rem;font-weight:700;text-transform:uppercase;border-bottom:1px solid #d1d5db}}
td {{padding:7px 12px;border-bottom:0.5px solid #f3f4f6;vertical-align:middle}}
tr:hover td {{background:#fafafa}}
.msg {{padding:10px 14px;border-radius:7px;font-size:0.82rem;margin-bottom:1rem;display:none}}
.msg.ok {{background:#dcfce7;color:#166534}}
.msg.err {{background:#fee2e2;color:#991b1b}}
input:focus {{outline:none;border-color:#0f3d2e!important}}
.note {{font-size:0.78rem;color:#6b7280;margin-bottom:1rem}}
</style>
</head>
<body>
<header>
  <div><h1>BGB Brushes — Stock Admin</h1><span>Manual stock overrides · logged in as {username}</span></div>
  <div style="margin-left:auto;display:flex;gap:10px;align-items:center">
    <a href="/brush/admin/stock-map/download" style="background:#0f3d2e;color:#fff;padding:5px 12px;border-radius:6px;font-size:0.78rem;text-decoration:none">⬇ Download stock_map.json</a>
    <a href="/brush" style="color:#8fc2ac;font-size:0.8rem">← Back to tool</a>
  </div>
</header>
<div class="container">
  <div class="msg" id="msg"></div>
  <div class="note">
    <strong>Workflow:</strong> Make your stock changes below and click Save. Then click <strong>⬇ Download stock_map.json</strong> — this gives you the complete file with all your changes merged in, ready to upload to GitHub as <code>stock_map.json</code>. That's all you need to do to make changes permanent.<br><br>
    Parts marked <strong>MANUAL</strong> have unsaved-to-GitHub overrides active in this session. Changes are lost if the server restarts before you download and commit the file.
  </div>
  <div class="card">
    <div class="card-header">
      <h2>Stock levels</h2>
      <span style="font-size:0.78rem;color:#6b7280">{len(all_parts)} parts · {len(_stock_overrides)} manual overrides active</span>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>Part number</th><th>Free stock</th><th>On order</th>
          <th>Last order date</th><th>EFACS refreshed</th><th></th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>
<script>
async function savePart(part) {{
  const free = parseInt(document.getElementById('free-'+part).value) || 0;
  const onOrder = parseInt(document.getElementById('order-'+part).value) || 0;
  const date = document.getElementById('date-'+part).value || '';
  const resp = await fetch('/brush/admin/stock-override', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{part_no: part, free_stock: free, on_order: onOrder, last_order_date: date}})
  }});
  const d = await resp.json();
  showMsg(d.ok ? 'Saved: '+part : 'Error: '+d.detail, d.ok ? 'ok' : 'err');
  if (d.ok) setTimeout(() => location.reload(), 800);
}}

async function clearOverride(part) {{
  const resp = await fetch('/brush/admin/stock-override/'+encodeURIComponent(part), {{method: 'DELETE'}});
  const d = await resp.json();
  showMsg(d.ok ? 'Override cleared: '+part : 'Error: '+d.detail, d.ok ? 'ok' : 'err');
  if (d.ok) setTimeout(() => location.reload(), 800);
}}

function showMsg(text, type) {{
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + type;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 4000);
}}
</script>
</body></html>""")


@brush_router.get("/admin/stock-map/download")
async def download_merged_stock_map(username: str = Depends(verify_admin)):
    try:
        with open(BRUSH_DIR / "stock_map.json") as f:
            base = json.load(f)
    except FileNotFoundError:
        base = {}
    merged = dict(base)
    for part, override in _stock_overrides.items():
        clean = {k: v for k, v in override.items() if k != "_manual_override"}
        merged[part] = {**merged.get(part, {}), **clean}
    merged = dict(sorted(merged.items()))
    content = json.dumps(merged, indent=2)
    return StreamingResponse(iter([content]), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=stock_map.json"})


@brush_router.get("/admin/stock-overrides/download")
async def download_stock_overrides(username: str = Depends(verify_admin)):
    content = json.dumps(_stock_overrides, indent=2)
    return StreamingResponse(iter([content]), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=stock_overrides.json"})


@brush_router.post("/admin/stock-override")
async def save_stock_override(request: Request, username: str = Depends(verify_admin)):
    body = await request.json()
    part_no = (body.get("part_no") or "").strip()
    if not part_no:
        raise HTTPException(status_code=400, detail="part_no required")
    override = {
        "free_stock": int(body.get("free_stock", 0) or 0),
        "on_order": int(body.get("on_order", 0) or 0),
        "last_order_date": str(body.get("last_order_date", "") or ""),
        "last_refreshed": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_manual_override": True,
    }
    _stock_overrides[part_no] = override
    STOCK_MAP[part_no] = {**STOCK_MAP.get(part_no, {}), **override}
    try:
        with open(STOCK_OVERRIDES_PATH, "w") as f:
            json.dump(_stock_overrides, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save: {e}")
    return JSONResponse({"ok": True, "part_no": part_no})


@brush_router.delete("/admin/stock-override/{part_no}")
async def clear_stock_override(part_no: str, username: str = Depends(verify_admin)):
    part_no = part_no.strip()
    if part_no in _stock_overrides:
        del _stock_overrides[part_no]
        try:
            with open(STOCK_OVERRIDES_PATH, "w") as f:
                json.dump(_stock_overrides, f, indent=2)
            try:
                with open(BRUSH_DIR / "stock_map.json") as f:
                    base = json.load(f)
                if part_no in base:
                    STOCK_MAP[part_no] = base[part_no]
                elif part_no in STOCK_MAP:
                    del STOCK_MAP[part_no]
            except Exception:
                pass
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save: {e}")
    return JSONResponse({"ok": True, "part_no": part_no})


@brush_router.get("/silver-rate")
async def get_silver_rate(username: str = Depends(verify_password)):
    now = time.time()
    if _silver_rate_cache["is_manual_override"] and _silver_rate_cache["manual_override_rate"]:
        rate = _silver_rate_cache["manual_override_rate"]
        age_hours = (now - _silver_rate_cache["fetched_at"]) / 3600
        return JSONResponse({
            "gerken_locked_eur_kg": GERKEN_LOCKED_EUR_KG, "gerken_locked_usd_kg": GERKEN_LOCKED_USD_KG,
            "gerken_stock_kg": GERKEN_STOCK_KG, "price_list_date": PRICE_LIST_DATE,
            "price_list_revision": PRICE_LIST_REVISION, "market_rate_usd_kg": rate,
            "market_rate_eur_kg": round(rate / GERKEN_USD_EUR_FX, 2), "market_source": "Manual override",
            "age_hours": round(age_hours, 1), "is_manual_override": True, "stale": False, "fx": GERKEN_USD_EUR_FX,
        })
    if _silver_rate_cache["fetched_at"] == 0 or (now - _silver_rate_cache["fetched_at"]) >= CACHE_TTL_SECONDS:
        result = await fetch_live_silver_rate()
        if result["ok"]:
            _silver_rate_cache["market_rate_usd_kg"] = result["usd_kg"]
            _silver_rate_cache["market_source"] = result["source"]
            _silver_rate_cache["fetched_at"] = result["fetched_at"]
    age_hours = (now - _silver_rate_cache["fetched_at"]) / 3600 if _silver_rate_cache["fetched_at"] > 0 else 9999
    stale = age_hours > 24
    return JSONResponse({
        "gerken_locked_eur_kg": GERKEN_LOCKED_EUR_KG, "gerken_locked_usd_kg": GERKEN_LOCKED_USD_KG,
        "gerken_stock_kg": GERKEN_STOCK_KG, "price_list_date": PRICE_LIST_DATE,
        "price_list_revision": PRICE_LIST_REVISION, "market_rate_usd_kg": _silver_rate_cache["market_rate_usd_kg"],
        "market_rate_eur_kg": round(_silver_rate_cache["market_rate_usd_kg"] / GERKEN_USD_EUR_FX, 2),
        "market_source": _silver_rate_cache.get("market_source", "Pending"), "age_hours": round(age_hours, 1),
        "is_manual_override": False, "stale": stale, "fx": GERKEN_USD_EUR_FX,
    })


@brush_router.post("/silver-rate/override")
async def set_silver_rate_override(request: Request, username: str = Depends(verify_password)):
    body = await request.json()
    rate = body.get("rate_usd_kg")
    if not rate or float(rate) <= 0:
        raise HTTPException(status_code=400, detail="Valid rate_usd_kg required")
    _silver_rate_cache["is_manual_override"] = True
    _silver_rate_cache["manual_override_rate"] = float(rate)
    _silver_rate_cache["fetched_at"] = time.time()
    return JSONResponse({"ok": True, "rate_usd_kg": float(rate), "source": "Manual override"})


@brush_router.post("/silver-rate/clear-override")
async def clear_silver_rate_override(username: str = Depends(verify_password)):
    _silver_rate_cache["is_manual_override"] = False
    _silver_rate_cache["manual_override_rate"] = None
    _silver_rate_cache["fetched_at"] = 0
    return JSONResponse({"ok": True})


_fx_cache = {"EUR_USD": 1.14, "EUR_GBP": 0.86, "fetched_at": 0, "source": "Rate pending fetch (ECB via open.er-api.com)"}
FX_CACHE_TTL = 6 * 60 * 60


async def fetch_live_fx() -> dict:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://open.er-api.com/v6/latest/EUR")
            if resp.status_code == 200:
                data = resp.json()
                rates = data.get("rates", {})
                usd, gbp = rates.get("USD"), rates.get("GBP")
                if usd and gbp:
                    return {"ok": True, "EUR_USD": round(usd, 4), "EUR_GBP": round(gbp, 4),
                            "source": "open.er-api.com (ECB reference rates)", "fetched_at": time.time()}
    except Exception:
        pass
    return {"ok": False}


@brush_router.get("/fx-rates")
async def get_fx_rates(username: str = Depends(verify_password)):
    now = time.time()
    if _fx_cache["fetched_at"] == 0 or (now - _fx_cache["fetched_at"]) >= FX_CACHE_TTL:
        result = await fetch_live_fx()
        if result["ok"]:
            _fx_cache["EUR_USD"] = result["EUR_USD"]; _fx_cache["EUR_GBP"] = result["EUR_GBP"]
            _fx_cache["source"] = result["source"]; _fx_cache["fetched_at"] = result["fetched_at"]
    age_hours = round((now - _fx_cache["fetched_at"]) / 3600, 1) if _fx_cache["fetched_at"] > 0 else None
    return JSONResponse({"EUR_USD": _fx_cache["EUR_USD"], "EUR_GBP": _fx_cache["EUR_GBP"],
        "source": _fx_cache["source"], "age_hours": age_hours, "stale": age_hours is None or age_hours > 24})


@brush_router.get("/sales-reps")
async def get_sales_reps(username: str = Depends(verify_password)):
    return JSONResponse(SALES_REPS)


@brush_router.get("/customer-lookup")
async def customer_lookup(name: str, username: str = Depends(verify_password)):
    name = name.strip()
    if not name:
        return JSONResponse({"is_existing": False, "enrichment": None, "suggestions": [], "trader_id": None, "bgb_entity": None})
    name_upper = name.upper()
    exact = CUSTOMERS_BY_NAME.get(name_upper)
    if exact:
        enrichment = customer_enrichment.get(name_upper)
        return JSONResponse({"is_existing": True, "matched_name": exact["name"], "trader_id": exact["trader_id"],
            "bgb_entity": exact["bgb_entity"], "enrichment": enrichment, "suggestions": []})
    suggestions = [c["name"] for c in CUSTOMERS_V2 if name_upper in c["name"].upper()][:8]
    enrichment = customer_enrichment.get(name_upper)
    return JSONResponse({"is_existing": False, "matched_name": name, "trader_id": UNKNOWN_CUSTOMER_TRADER_ID,
        "bgb_entity": None, "enrichment": enrichment, "suggestions": suggestions})


@brush_router.post("/customer-enrichment")
async def save_enrichment(request: Request, username: str = Depends(verify_password)):
    body = await request.json()
    name = body.get("name", "").strip().upper()
    if not name:
        raise HTTPException(status_code=400, detail="Customer name required")
    customer_enrichment[name] = {"type": body.get("type", ""), "ownership": body.get("ownership", ""),
        "updated_by": username, "updated_at": datetime.now().strftime("%d %b %Y %H:%M")}
    return JSONResponse({"saved": True, "enrichment": customer_enrichment[name]})


@brush_router.get("/brush-search")
async def brush_search(q: str = "", username: str = Depends(verify_password)):
    q = q.strip().upper()
    if not q or len(q) < 2:
        return JSONResponse({"results": []})

    results = []
    bgb_equivalent = PART_EQUIVALENCE_MAP.get(q)

    if bgb_equivalent and bgb_equivalent.upper() != q:
        equiv_silver = next((s for s in SILVER_BRUSHES if s["bgb_ref"].upper() == bgb_equivalent.upper()), None)
        if equiv_silver:
            results.append({
                "source": "silver_priced", "part_no": equiv_silver.get("bgb_ref"), "description": equiv_silver.get("description"),
                "oem": "Vestas" if equiv_silver.get("oem_ref") else "", "turbine_type": equiv_silver.get("turbine"),
                "grade": equiv_silver.get("grade"), "oe_ref": equiv_silver.get("oem_ref"), "material": "Silver",
                "option": equiv_silver.get("option"), "status": equiv_silver.get("status", "Unknown"),
                "aftermarket_ref": equiv_silver.get("aftermarket_ref", ""),
                "drawing_url": _drawing_url(equiv_silver.get("bgb_ref", ""), equiv_silver.get("drawing_image")),
                "has_pricing": True, "price_breaks_usd": equiv_silver.get("price_breaks_usd"),
                "price_breaks_eur": equiv_silver.get("price_breaks_eur"), "carbex": equiv_silver.get("carbex"),
                "units_from_gerken_50kg": equiv_silver.get("units_from_gerken_50kg"), "j_slope_ref": equiv_silver.get("j_slope_ref"),
                **_master_enrichment(equiv_silver.get("bgb_ref", "")), "stock": _stock_for_part(equiv_silver.get("bgb_ref", "")),
                "resolved_from_typed_ref": q,
                "cross_reference_note": f"'{q}' is a non-BGB reference — resolved to BGB equivalent {bgb_equivalent} for logging this enquiry.",
            })

    for b in BRUSHES:
        haystack = " ".join([b.get("spc_part_no", ""), b.get("drawing_no", ""), b.get("oe_ref", ""),
            b.get("app_description") or "", b.get("efacs_description", ""), b.get("turbine_oem", ""), b.get("brush_type", ""), b.get("bgb_grade", "")]).upper()
        if q in haystack:
            results.append({
                "source": "master_list", "part_no": b.get("spc_part_no"), "description": b.get("app_description") or b.get("efacs_description") or b.get("brush_type"),
                "oem": b.get("turbine_oem"), "turbine_type": b.get("turbine_type"), "brush_type": b.get("brush_type"),
                "grade": b.get("bgb_grade"), "oe_ref": b.get("oe_ref"), "material": b.get("material"), "dimensions": b.get("dimensions"),
                "has_pricing": False, "status": _status_for_part(b.get("spc_part_no", "")),
                "aftermarket_ref": bgb_to_aftermarket_lookup(b.get("spc_part_no", "")), "bgb_equivalent_part": bgb_equivalent,
                "stock": _stock_for_part(b.get("spc_part_no", "")), **_master_enrichment(b.get("spc_part_no", "")),
            })

    for r in REFERENCES:
        haystack = " ".join([r.get("title", ""), r.get("oem_part", ""), r.get("purchased_part", ""),
            r.get("saleable_part", ""), r.get("bgb_description", "")]).upper()
        if q in haystack:
            results.append({
                "source": "reference", "part_no": r.get("saleable_part") or r.get("purchased_part"), "description": r.get("title"),
                "oem": "Vestas" if "VESTAS" in r.get("oem_part", "").upper() else "", "grade": r.get("bgb_grade"),
                "oe_ref": r.get("oem_part"), "material": r.get("source_grade"), "purchased_part": r.get("purchased_part"),
                "saleable_part": r.get("saleable_part"), "has_pricing": False,
            })

    for s in SILVER_BRUSHES:
        haystack = " ".join([s.get("bgb_ref") or "", s.get("gerken_ref") or "", s.get("oem_ref") or "",
            s.get("turbine") or "", s.get("description") or "", s.get("grade") or ""]).upper()
        if q in haystack:
            results.append({
                "source": "silver_priced", "part_no": s.get("bgb_ref"), "description": s.get("description"),
                "oem": s.get("turbine", ""), "turbine_type": s.get("turbine"), "grade": s.get("grade"), "oe_ref": s.get("oem_ref"),
                "material": "Silver", "option": s.get("option"), "status": s.get("status", "Unknown"),
                "aftermarket_ref": s.get("aftermarket_ref", ""), "drawing_url": _drawing_url(s.get("bgb_ref", ""), s.get("drawing_image")),
                "has_pricing": True, "price_breaks_usd": s.get("price_breaks_usd"), "price_breaks_eur": s.get("price_breaks_eur"),
                "carbex": s.get("carbex"), "units_from_gerken_50kg": s.get("units_from_gerken_50kg"), "j_slope_ref": s.get("j_slope_ref"),
                "stock": _stock_for_part(s.get("bgb_ref", "")), **_master_enrichment(s.get("bgb_ref", "")),
            })

    for c in COPPER_BRUSHES:
        haystack = " ".join([c.get("bgb_ref") or "", c.get("gerken_ref") or "", c.get("oem_ref") or "", c.get("description") or "", c.get("grade") or ""]).upper()
        if q in haystack:
            results.append({
                "source": "copper_priced", "part_no": c.get("bgb_ref"), "description": c.get("description"), "oem": "",
                "grade": c.get("grade"), "oe_ref": c.get("oem_ref"), "material": "Copper", "option": c.get("option"),
                "status": c.get("status", "Unknown"), "aftermarket_ref": c.get("aftermarket_ref", ""),
                "drawing_url": _drawing_url(c.get("bgb_ref", ""), c.get("drawing_image")), "has_pricing": True, "is_copper": True,
                "price_breaks_eur": c.get("price_breaks"), "stock": _stock_for_part(c.get("bgb_ref", "")), **_master_enrichment(c.get("bgb_ref", "")),
            })

    def _resolve_bgb_part(r):
        part = (r.get("part_no") or "").strip()
        if "\n" in part or "BGB Branded:" in part:
            for line in part.split("\n"):
                if "BGB BRANDED" in line.upper():
                    return line.split(":", 1)[1].strip()
            return None
        return part if part else None

    upgraded = []
    for r in results:
        if r["source"] in ("master_list", "reference"):
            bgb_part = _resolve_bgb_part(r)
            if bgb_part:
                part_upper = bgb_part.upper()
                silver_match = next((s for s in SILVER_BRUSHES if s["bgb_ref"].upper() == part_upper), None)
                copper_match = next((c for c in COPPER_BRUSHES if c["bgb_ref"].upper() == part_upper), None)
                if silver_match:
                    upgraded.append({
                        "source": "silver_priced", "part_no": silver_match.get("bgb_ref"), "description": silver_match.get("description"),
                        "turbine_type": silver_match.get("turbine"), "grade": silver_match.get("grade"), "oe_ref": silver_match.get("oem_ref"),
                        "option": silver_match.get("option"), "status": silver_match.get("status", "Unknown"),
                        "aftermarket_ref": silver_match.get("aftermarket_ref", ""),
                        "drawing_url": _drawing_url(silver_match.get("bgb_ref", ""), silver_match.get("drawing_image")), "has_pricing": True,
                        "price_breaks_usd": silver_match.get("price_breaks_usd"), "price_breaks_eur": silver_match.get("price_breaks_eur"),
                        "carbex": silver_match.get("carbex"), "units_from_gerken_50kg": silver_match.get("units_from_gerken_50kg"),
                        "j_slope_ref": silver_match.get("j_slope_ref"), "stock": _stock_for_part(silver_match.get("bgb_ref", "")),
                        **_master_enrichment(silver_match.get("bgb_ref", "")),
                    })
                    continue
                if copper_match:
                    upgraded.append({
                        "source": "copper_priced", "part_no": copper_match.get("bgb_ref"), "description": copper_match.get("description"),
                        "grade": copper_match.get("grade"), "oe_ref": copper_match.get("oem_ref"), "option": copper_match.get("option"),
                        "status": copper_match.get("status", "Unknown"), "aftermarket_ref": copper_match.get("aftermarket_ref", ""),
                        "drawing_url": _drawing_url(copper_match.get("bgb_ref", ""), copper_match.get("drawing_image")), "has_pricing": True,
                        "is_copper": True, "price_breaks_eur": copper_match.get("price_breaks"), "stock": _stock_for_part(copper_match.get("bgb_ref", "")),
                        **_master_enrichment(copper_match.get("bgb_ref", "")),
                    })
                    continue
        upgraded.append(r)
    results = upgraded

    SOURCE_PRIORITY = {"silver_priced": 0, "copper_priced": 0, "master_list": 1, "reference": 2}

    def dedup_key(r):
        part = (r.get("part_no") or "").strip().upper()
        if "BGB BRANDED:" in part:
            for line in part.split("\n"):
                if "BGB BRANDED" in line.upper():
                    part = line.split(":", 1)[1].strip().upper()
                    break
        return part

    best_by_part = {}
    for r in results:
        key = dedup_key(r)
        if not key:
            continue
        existing = best_by_part.get(key)
        if existing is None or SOURCE_PRIORITY.get(r["source"], 9) < SOURCE_PRIORITY.get(existing["source"], 9):
            best_by_part[key] = r

    seen = set()
    deduped_results = []
    for r in results:
        key = dedup_key(r)
        if key in seen:
            continue
        seen.add(key)
        deduped_results.append(best_by_part.get(key, r))
    results = deduped_results

    STATUS_ORDER = {"Sellable": 0, "Prototype": 1, "In development": 2, "Unknown": 3}
    results.sort(key=lambda r: STATUS_ORDER.get(r.get("status", "Unknown"), 3))

    return JSONResponse({"results": results[:20]})


@brush_router.get("/silver-cost")
async def silver_cost(part_no: str, qty: int, currency: str = "USD", username: str = Depends(verify_password)):
    part_no = part_no.strip().upper()
    product = next((s for s in SILVER_BRUSHES if s["bgb_ref"].upper() == part_no), None)
    if not product:
        raise HTTPException(status_code=404, detail="Part not found in silver pricing catalogue")

    fx = GERKEN_USD_EUR_FX
    breaks_usd = product.get("price_breaks_usd", [])
    breaks_eur = product.get("price_breaks_eur", [])

    def get_jk(breaks, target_qty):
        sorted_b = sorted(breaks, key=lambda b: b["qty"])
        if not sorted_b:
            return None, None
        exact = next((b for b in sorted_b if b["qty"] == target_qty), None)
        if exact:
            return exact.get("j_slope"), exact.get("k_intercept")
        if target_qty < sorted_b[0]["qty"]:
            return sorted_b[0].get("j_slope"), sorted_b[0].get("k_intercept")
        if target_qty > sorted_b[-1]["qty"]:
            return sorted_b[-1].get("j_slope"), sorted_b[-1].get("k_intercept")
        lower = max((b for b in sorted_b if b["qty"] <= target_qty), key=lambda b: b["qty"])
        upper = min((b for b in sorted_b if b["qty"] >= target_qty), key=lambda b: b["qty"])
        frac = (target_qty - lower["qty"]) / (upper["qty"] - lower["qty"]) if upper["qty"] != lower["qty"] else 0
        j = lower["j_slope"] + frac * (upper["j_slope"] - lower["j_slope"])
        k = lower["k_intercept"] + frac * (upper["k_intercept"] - lower["k_intercept"])
        return j, k

    def calc_price(j, k, silver_usd_kg, currency):
        if j is None or k is None:
            return None
        if currency == "USD":
            return round(j * silver_usd_kg + k * fx, 2)
        else:
            return round(j * (silver_usd_kg / fx) + k, 2)

    def get_list_price(breaks, target_qty):
        """Fallback for parts with no silver-linked slope/intercept — interpolate the flat list price instead."""
        sorted_b = sorted([b for b in breaks if b.get("list_price") is not None], key=lambda b: b["qty"])
        if not sorted_b:
            return None
        exact = next((b for b in sorted_b if b["qty"] == target_qty), None)
        if exact:
            return exact["list_price"]
        if target_qty < sorted_b[0]["qty"]:
            return sorted_b[0]["list_price"]
        if target_qty > sorted_b[-1]["qty"]:
            return sorted_b[-1]["list_price"]
        lower = max((b for b in sorted_b if b["qty"] <= target_qty), key=lambda b: b["qty"])
        upper = min((b for b in sorted_b if b["qty"] >= target_qty), key=lambda b: b["qty"])
        frac = (target_qty - lower["qty"]) / (upper["qty"] - lower["qty"]) if upper["qty"] != lower["qty"] else 0
        return round(lower["list_price"] + frac * (upper["list_price"] - lower["list_price"]), 2)

    j_usd, k_usd = get_jk(breaks_usd, qty)
    j_eur, k_eur = get_jk(breaks_eur, qty) if breaks_eur else (j_usd, k_usd)

    fixed_price_mode = j_usd is None or k_usd is None
    if fixed_price_mode:
        # No silver-linked formula for this part (e.g. negligible silver content) — use the flat list price.
        gerken_price = get_list_price(breaks_usd if currency == "USD" else breaks_eur, qty)
    else:
        gerken_price = calc_price(j_usd, k_usd, GERKEN_LOCKED_USD_KG, currency)
    market_usd = (_silver_rate_cache["manual_override_rate"] if _silver_rate_cache["is_manual_override"] and _silver_rate_cache["manual_override_rate"]
        else _silver_rate_cache["market_rate_usd_kg"])
    market_source = "Manual override" if _silver_rate_cache["is_manual_override"] else _silver_rate_cache.get("market_source", "Pending live fetch")
    market_stale = not _silver_rate_cache["is_manual_override"] and (
        (time.time() - _silver_rate_cache["fetched_at"]) / 3600 > 24 if _silver_rate_cache["fetched_at"] > 0 else True)
    market_price = gerken_price if fixed_price_mode else calc_price(j_usd, k_usd, market_usd, currency)

    silver_kg_per_unit = j_usd if j_usd else 0
    units_from_stock = int(GERKEN_STOCK_KG / silver_kg_per_unit) if silver_kg_per_unit > 0 else None

    sym = "$" if currency == "USD" else "€"
    market_eur_kg = round(market_usd / fx, 2)

    return JSONResponse({
        "part_no": product["bgb_ref"], "description": product["description"], "qty": qty, "currency": currency,
        "silver_kg_per_unit": round(silver_kg_per_unit, 5), "units_from_gerken_50kg_stock": units_from_stock,
        "pricing": {
            "gerken_locked": {"unit_price": gerken_price, "total": round(gerken_price * qty, 2) if gerken_price else None,
                "rate_eur_kg": GERKEN_LOCKED_EUR_KG, "rate_usd_kg": GERKEN_LOCKED_USD_KG,
                "label": "Fixed list price" if fixed_price_mode else "Gerken quoted rate",
                "note": "This part has no silver-linked pricing formula in the price list — quoted at a fixed price regardless of silver market." if fixed_price_mode else ""},
            "market_rate": {"unit_price": market_price, "total": round(market_price * qty, 2) if market_price else None,
                "rate_eur_kg": market_eur_kg, "rate_usd_kg": market_usd, "source": market_source,
                "label": "Fixed list price" if fixed_price_mode else "Live market rate (for comparison)",
                "note": ("Same fixed price — this part's cost does not move with the silver market." if fixed_price_mode else
                    "Gerken holds no dedicated silver stock — every order is priced at their quoted rate above. This is a live/manual LBMA check for comparison only. Subject to LBMA ±3% confirmation at time of order."),
                "stale": False if fixed_price_mode else market_stale},
        },
        "price_list_date": product.get("price_list_date", PRICE_LIST_DATE), "price_list_revision": PRICE_LIST_REVISION,
        "fx": {"EUR_USD": _fx_cache["EUR_USD"], "EUR_GBP": _fx_cache["EUR_GBP"], "fx_source": _fx_cache["source"]},
    })


@brush_router.get("/copper-cost")
async def copper_cost(part_no: str, qty: int, username: str = Depends(verify_password)):
    part_no = part_no.strip().upper()
    product = next((c for c in COPPER_BRUSHES if c["bgb_ref"].upper() == part_no), None)
    if not product:
        raise HTTPException(status_code=404, detail="Part not found in copper pricing catalogue")
    breaks_sorted = sorted(product["price_breaks"], key=lambda b: b["qty"])
    exact = next((b for b in breaks_sorted if b["qty"] == qty), None)
    if exact:
        unit_price = exact["price_eur"]; basis = f"exact quantity break ({qty} units)"
    elif qty < breaks_sorted[0]["qty"]:
        unit_price = breaks_sorted[0]["price_eur"]; basis = f"below minimum break — priced at {breaks_sorted[0]['qty']} unit rate"
    elif qty > breaks_sorted[-1]["qty"]:
        unit_price = breaks_sorted[-1]["price_eur"]; basis = f"above maximum break — priced at {breaks_sorted[-1]['qty']} unit rate"
    else:
        lower = max((b for b in breaks_sorted if b["qty"] <= qty), key=lambda b: b["qty"])
        upper = min((b for b in breaks_sorted if b["qty"] >= qty), key=lambda b: b["qty"])
        if lower["qty"] == upper["qty"]:
            unit_price = lower["price_eur"]
        else:
            frac = (qty - lower["qty"]) / (upper["qty"] - lower["qty"])
            unit_price = lower["price_eur"] + frac * (upper["price_eur"] - lower["price_eur"])
        basis = f"interpolated between {lower['qty']} and {upper['qty']} unit breaks"
    total = round(unit_price * qty, 2)
    return JSONResponse({"part_no": product["bgb_ref"], "description": product["description"], "qty": qty, "currency": "EUR",
        "unit_price": round(unit_price, 2), "total_price": total, "pricing_basis": basis,
        "note": "Fixed price list rate — copper has no precious-metal cost component, so this price does not fluctuate with silver markets. Please request a quotation to confirm delivery time and price."})


@brush_router.get("/silver-pricing-meta")
async def get_pricing_meta(username: str = Depends(verify_password)):
    return JSONResponse(PRICING_META)


@brush_router.post("/submit-enquiry")
async def submit_enquiry(request: Request, username: str = Depends(verify_password)):
    body = await request.json()
    customer_name = body.get("customer_name", "").strip()
    is_existing = body.get("is_existing_customer", False)
    lines = body.get("lines", [])
    current_supplier = body.get("current_supplier", "")
    sales_rep = body.get("sales_rep", "")

    if not customer_name or not lines:
        raise HTTPException(status_code=400, detail="Customer name and at least one part line required")

    customer_record = CUSTOMERS_BY_NAME.get(customer_name.upper())
    trader_id = customer_record["trader_id"] if customer_record else UNKNOWN_CUSTOMER_TRADER_ID
    customer_order_reference = "" if customer_record else customer_name

    rep_record = SALES_REPS_BY_NAME.get(sales_rep)
    bgb_company = rep_record["bgb_entity"] if rep_record else ""

    pipeline_value = 0.0
    for line in lines:
        part_no = (line.get("matched_part_no") or line.get("part_no") or "").strip().upper()
        qty = int(line.get("qty", 0) or 0)
        silver_match = next((s for s in SILVER_BRUSHES if s["bgb_ref"].upper() == part_no), None)
        copper_match = next((c for c in COPPER_BRUSHES if c["bgb_ref"].upper() == part_no), None)
        if silver_match and qty > 0:
            breaks_sorted = sorted(silver_match["price_breaks_usd"], key=lambda b: b["qty"])
            unit_price = _interpolate_price(breaks_sorted, qty, "list_price")
            pipeline_value += unit_price * qty * MARGIN_MULTIPLIER
        elif copper_match and qty > 0:
            breaks_sorted = sorted(copper_match["price_breaks"], key=lambda b: b["qty"])
            unit_price_eur = _interpolate_price(breaks_sorted, qty, "price_eur")
            unit_price_usd = unit_price_eur * PRICING_META["usd_eur_fx"]
            pipeline_value += unit_price_usd * qty * MARGIN_MULTIPLIER
    pipeline_value = round(pipeline_value, 2)

    score = 30
    positives, risks = [], []
    if is_existing:
        score += 20; positives.append("Existing BGB customer")
    else:
        risks.append("New / unverified customer")

    customer_type = body.get("customer_type", "")
    if customer_type == "OEM":
        score += 15; positives.append("OEM-level customer")
    elif customer_type == "Operator":
        score += 12; positives.append("Operator with direct turbine access")
    elif customer_type == "Distributor":
        score += 5
    elif customer_type == "Unknown":
        risks.append("Customer type unconfirmed")

    ownership = body.get("wind_farm_ownership", "")
    if ownership and "owns" in ownership.lower():
        score += 10; positives.append("Confirmed wind farm asset ownership")

    total_qty = sum(int(l.get("qty", 0) or 0) for l in lines)
    if total_qty > 0:
        if not is_existing and total_qty > 1000:
            score -= 15; risks.append(f"Large volume ({total_qty} units) from unverified customer — verify credibility")
        elif total_qty >= 50:
            score += 10; positives.append(f"Meaningful volume ({total_qty} units)")

    if current_supplier:
        positives.append(f"Currently using {current_supplier} — known displacement target"); score += 5

    timeline = body.get("timeline", "")
    if "trial" in timeline.lower():
        positives.append("Trial-first approach — realistic path to OEM approval evidence"); score += 8

    score = max(0, min(100, score))
    band = "Hot" if score >= 75 else "Warm" if score >= 50 else "Cool" if score >= 25 else "Cold"

    any_part_unknown = any(not l.get("matched_part_no") for l in lines)
    if any_part_unknown:
        routing = "Nick Hubbard"
        routing_reason = "One or more requested parts are not in the current BGB brush catalogue — requires sourcing/engineering review"
    else:
        routing = "Purchasing"
        routing_reason = "All requested parts exist in the BGB brush catalogue — standard purchasing workflow"

    entry = {
        "id": str(uuid.uuid4())[:8], "timestamp": datetime.now().strftime("%d %b %Y %H:%M"), "logged_by": username,
        "sales_rep": sales_rep, "bgb_company": bgb_company, "customer_name": customer_name, "trader_id": trader_id,
        "customer_order_reference": customer_order_reference, "is_existing_customer": is_existing, "customer_type": customer_type,
        "wind_farm_ownership": ownership, "lines": lines, "total_qty": total_qty, "current_supplier": current_supplier,
        "timeline": timeline, "score": score, "band": band, "key_positives": positives[:4], "key_risks": risks[:4],
        "routing": routing, "routing_reason": routing_reason, "notes": body.get("notes", ""), "pipeline_value_usd": pipeline_value,
    }
    enquiry_log.insert(0, entry)
    save_records("enquiries", enquiry_log)
    export_enquiry(entry)
    return JSONResponse({"entry": entry})


@brush_router.get("/log")
async def get_brush_log(username: str = Depends(verify_password)):
    return JSONResponse(enquiry_log)


@brush_router.get("/export-efacs-csv")
async def export_efacs_csv(username: str = Depends(verify_password)):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Company", "Sales Representative", "Enquiry Date", "Trader ID", "Customer Order Reference",
        "Enquiry Category", "Part no", "Qty"])
    for entry in enquiry_log:
        date_str = entry["timestamp"]
        for line in entry.get("lines", []):
            part_no = line.get("matched_part_no") or line.get("part_no", "")
            writer.writerow([entry.get("bgb_company", ""), entry.get("sales_rep", ""), date_str,
                entry.get("trader_id", UNKNOWN_CUSTOMER_TRADER_ID), entry.get("customer_order_reference", ""),
                "BGB Brushes", part_no, line.get("qty", "")])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=bgb_brushes_efacs_export_{datetime.now().strftime('%Y%m%d')}.csv"})


app.include_router(brush_router)


# =====================================================================
# TOTAL COST OF OWNERSHIP CALCULATOR  (/tco/*)
# =====================================================================

tco_router = APIRouter(prefix="/tco", tags=["tco"])
TCO_DIR = BASE_DIR / "tco"
WINDFARM_DB = TCO_DIR / "data" / "windfarms.db"


@tco_router.get("", response_class=HTMLResponse)
@tco_router.get("/", response_class=HTMLResponse)
async def tco_home(username: str = Depends(verify_password)):
    return read_html("tco", "hub.html")


@tco_router.get("/ownership", response_class=HTMLResponse)
async def tco_ownership(username: str = Depends(verify_password)):
    return read_html("tco", "ownership.html")


@tco_router.get("/downtime", response_class=HTMLResponse)
async def tco_downtime(username: str = Depends(verify_password)):
    return read_html("tco", "downtime.html")


@tco_router.get("/windfarm", response_class=HTMLResponse)
async def tco_windfarm(username: str = Depends(verify_password)):
    return read_html("tco", "windfarm.html")


def _windfarm_conn():
    conn = sqlite3.connect(str(WINDFARM_DB))
    conn.row_factory = sqlite3.Row
    return conn


@tco_router.get("/windfarm-countries")
async def windfarm_countries(username: str = Depends(verify_password)):
    conn = _windfarm_conn()
    rows = conn.execute("SELECT DISTINCT country FROM windfarms WHERE country IS NOT NULL ORDER BY country").fetchall()
    conn.close()
    return JSONResponse([r["country"] for r in rows])


@tco_router.get("/windfarm-manufacturers")
async def windfarm_manufacturers(username: str = Depends(verify_password)):
    conn = _windfarm_conn()
    rows = conn.execute("SELECT DISTINCT manufacturer FROM windfarms WHERE manufacturer IS NOT NULL ORDER BY manufacturer").fetchall()
    conn.close()
    return JSONResponse([r["manufacturer"] for r in rows])


@tco_router.get("/windfarm-search")
async def windfarm_search(
    q: str = "", country: str = "", offshore: str = "",
    manufacturer: str = "", model: str = "", owner: str = "", operator: str = "",
    username: str = Depends(verify_password),
):
    conn = _windfarm_conn()
    clauses, params = [], []
    if q:
        like = f"%{q.upper()}%"
        clauses.append(
            "(UPPER(name) LIKE ? OR UPPER(name2) LIKE ? OR UPPER(operator) LIKE ? OR UPPER(owner) LIKE ? "
            "OR UPPER(developer) LIKE ? OR UPPER(city) LIKE ? OR UPPER(area) LIKE ? "
            "OR UPPER(manufacturer) LIKE ? OR UPPER(turbine_model) LIKE ?)"
        )
        params += [like] * 9
    if country:
        clauses.append("UPPER(country) = UPPER(?)"); params.append(country)
    if offshore in ("Yes", "No"):
        clauses.append("offshore = ?"); params.append(offshore)
    if manufacturer:
        clauses.append("UPPER(manufacturer) LIKE ?"); params.append(f"%{manufacturer.upper()}%")
    if model:
        clauses.append("UPPER(turbine_model) LIKE ?"); params.append(f"%{model.upper()}%")
    if owner:
        clauses.append("UPPER(owner) LIKE ?"); params.append(f"%{owner.upper()}%")
    if operator:
        clauses.append("UPPER(operator) LIKE ?"); params.append(f"%{operator.upper()}%")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    count_sql = f"SELECT COUNT(*) FROM windfarms {where}"
    total = conn.execute(count_sql, params).fetchone()[0]
    sql = f"SELECT * FROM windfarms {where} LIMIT 100"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    results = [dict(r) for r in rows]
    return JSONResponse({"results": results, "total": total, "truncated": total > len(results)})


@tco_router.get("/windfarm-view-page", response_class=HTMLResponse)
async def windfarm_view_page(username: str = Depends(verify_password)):
    return read_html("tco", "farm-detail.html")


@tco_router.get("/windfarm-view")
async def windfarm_view(country: str = "", name: str = "", area: str = "", username: str = Depends(verify_password)):
    if not country or not name:
        return JSONResponse({"error": "country and name are required"}, status_code=400)
    conn = _windfarm_conn()
    clauses = ["country = ?", "name = ?"]
    params = [country, name]
    if area:
        clauses.append("(area = ? OR area IS NULL)"); params.append(area)
    where = "WHERE " + " AND ".join(clauses)
    rows = conn.execute(f"SELECT * FROM windfarms {where} ORDER BY commissioning_date", params).fetchall()
    conn.close()

    entries = [dict(r) for r in rows]
    if not entries:
        return JSONResponse({"error": "not found"}, status_code=404)

    total_turbines = sum(r["num_turbines_n"] or 0 for r in entries)
    total_power_kw = sum(r["total_power_kw_n"] or 0 for r in entries)
    manufacturers = sorted({r["manufacturer"] for r in entries if r["manufacturer"]})
    statuses = sorted({r["status"] for r in entries if r["status"]})
    first = entries[0]

    summary = {
        "name": first["name"], "name2": first["name2"], "country": first["country"],
        "continent": first["continent"], "area": first["area"], "city": first["city"],
        "lat": first["lat"], "lon": first["lon"], "offshore": first["offshore"],
        "shore_distance": first["shore_distance"],
        "total_turbines": total_turbines, "total_power_kw": total_power_kw,
        "manufacturers": manufacturers, "statuses": statuses, "phase_count": len(entries),
    }
    return JSONResponse({"summary": summary, "entries": entries})


app.include_router(tco_router)

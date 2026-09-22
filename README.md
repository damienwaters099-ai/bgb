# BGB CRM — combined app

One FastAPI service, one set of credentials, four home tiles: **Opportunities**, **BGB Brush**, **Total Cost of Ownership Calculator**, **Product Application** (external link to `bgbapp.netlify.app`, confirmed).

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env    # fill in APP_USERNAME / APP_PASSWORD / ADMIN_USERNAME / ADMIN_PASSWORD / ANTHROPIC_API_KEY
uvicorn main:app --reload
# http://localhost:8000
```

## Deploy
Same as the two original tools — push to GitHub, connect the repo on Render, `render.yaml` is auto-detected. Set the five env vars listed in `.env.example`. `ANTHROPIC_API_KEY` is only needed for the Opportunities screen's company lookup — Brush and TCO have no external API dependency.

## What changed from the two original tools
- **Auth unified.** Both tools used to ship their own `APP_USERNAME`/`APP_PASSWORD`. Now there's one set for the whole app, plus the Brush tool's separate `ADMIN_USERNAME`/`ADMIN_PASSWORD` for `/brush/admin` (stock overrides) — unchanged in behaviour, just namespaced under `/brush`.
- **All API paths namespaced** — `/research-company` → `/opportunities/research-company`, `/brush-search` → `/brush/brush-search`, etc. Nothing in the scoring/pricing/routing logic was touched.
- **Opportunities**: the existing "Opportunity & customer check" screen (company lookup → qualify → team log) is unchanged except one addition — a **"Log opportunity"** button next to "View team log" that opens `/opportunities/new`, a separate mini-app (New opportunity / Pipeline tabs) built to your Opportunity Information.xlsx field spec.
- **BGB Brush**: unchanged internally. A new hub screen (`/brush`) sits in front of it with two tiles — Brush search and Log enquiry — which deep-link into the existing tool with the right tab pre-selected.
- **TCO Calculator**: split into three screens behind a hub (`/tco`) — Downtime cost calculator (new), Wind farm lookup (new), Cost of ownership (ported from your TOC.html, math untouched).

## Open items — do not treat these as finished

1. **Downtime Cost Calculator — sourced vs. placeholder, explicitly tagged in the UI:**
   - **Sourced:** CTV charter day rate £2,000–£3,250/day; jack-up spot-market charter £95,300–£287,400/day; vessels ≈73% of total offshore O&M cost — all from University of Strathclyde vessel-charter-rate research (Kolios et al., *"Optimum CTV fleet selection for offshore wind farm O&M activities"* and *"Vessel charter rate estimation for offshore wind O&M activities"*). Offshore lifetime O&M benchmark £40–60/MWh is industry commentary (southern North Sea), not a single primary source.
   - **Placeholder, not sourced — you must confirm before quoting:** capacity factor (35% default), electricity price (£75/MWh default), all labour day-rates, onshore access/plant cost, and the 30% unplanned-mobilisation premium. Vessel/jack-up day rates are commercially negotiated and not published — the sourced range above is spot-market; your actual O&M contract rate is very likely lower.
   - This is flagged in the tool itself with SOURCED / PLACEHOLDER tags on every field, and the disclaimer banner names it as needing Finance/Engineering sign-off before any number reaches a customer — per your own AI Deployment Filter rule.

2. **Wind Farm lookup** is built fresh from `Windfarms World Master.xls` (43,013 rows, 132 countries), converted to an indexed SQLite file (`tco/data/windfarms.db`, 10MB) rather than shipped to the browser as JSON. Search covers name, alt name, operator, owner, developer, city, area; filterable by country and onshore/offshore. Note the source data uses `#ND` for undisclosed fields and "United-Kingdom" (hyphenated) as its country string, not "United Kingdom" — the country dropdown is populated live from the data so this doesn't break the UI, but don't hardcode country strings anywhere downstream.
   Results carry a **"Use →"** button that pre-fills the Downtime Calculator's country, onshore/offshore, and rated-capacity-per-turbine fields — the master file has no capacity factor or electricity price, so those still need entering.

3. **"Cost of ownership" screen** — "Scheduled servicing" is renamed to **"Spring Clips"** throughout (section header, both field labels, the cost-breakdown chart category, and the footer note). Internal variable names (`serviceCycle`, `serviceCost`) are untouched — only the labels a user sees changed, so nothing about the calculation logic moved.

## New Opportunity screen — built from Opportunity Information.xlsx

`/opportunities/new` is now a real mini-app (New opportunity / Pipeline tabs, mirroring the Brush tool's pattern) built directly from your field spreadsheet, not placeholders:

- **Fields**: Opportunity name, Customer name (autocomplete against the 1,283-row customer master, "existing"/"new" tag same as Brush), Assigned to (11 people), Prospect stage (6 stages, with description shown on select), Target close date, Probability (4 bands → %), Currency, Details — then three repeated value blocks (Total / In year / On-going), each with Value, Cost of materials, NRE hours cost, Prototype hours cost — then Activity type (15 options, description shown on select) + Comment.
- **Margins are calculated server-side**, per your Calculations tab: `Net margin % = (Value − Cost of materials − NRE − Prototype) / Value`, `Margin on material % = (Value − Cost of materials) / Value`. The client shows a live preview as you type, but the stored figure is always the server's recompute — never trusts client arithmetic.
- **EFACS export respects your Yes/No flags.** Cost of materials, NRE hours cost, and Prototype hours cost (and their In year / On-going equivalents) are marked "No" in your spreadsheet and are kept internal-only — visible in the app, excluded from `Export EFACS CSV`. Only the columns you flagged "Yes" are in the export.
- **Two things I interpreted rather than asked about, both cheap to change:** (1) the spreadsheet's own "Dropdown?" question marks against Customer/Assigned to/Target close date — built as autocomplete, dropdown, and date-picker respectively, the obvious reading; (2) "On-going In Year Opportunity Net Margin %" — your sheet's own label, which repeats "In Year" inside the On-going block (looks like a copy-paste artifact when that block was built from the In Year one) — displayed as "On-going Opportunity Net Margin %" for clarity, same underlying calculation.
- **Data-quality note, not a bug**: your customer list has "AECOM DESIGN BUILD IRELAND LTD" twice under two different codes (AEC001/AEC002) — it'll surface twice in autocomplete. Worth a look on your end.
- **Activity Type + Comment** is logged as the opportunity's first activity entry (e.g. "Reqs clarified" / "Customer discussion" alongside what happened) — the spreadsheet doesn't specify a repeatable activity-log UI, so this captures one activity at creation time. Say the word if you want a running activity timeline per opportunity instead of a single entry.

## File map
```
main.py                  unified FastAPI app (home + 3 routers: /opportunities, /brush, /tco)
home.html                4-tile home screen
opportunities/
  opportunities.html     existing tool, unchanged except the Log opportunity button + path prefixes
  new_opportunity.html   new — built from Opportunity Information.xlsx (New opportunity / Pipeline tabs)
  data/                  customers (1,283), assigned-to, prospect stages, probability, currency, activity types
brush/
  hub.html               new — 2-tile hub (Brush search / Log enquiry)
  brush.html             existing tool, unchanged except path prefixes + tab deep-linking
  *.json, drawings/       existing catalogue/pricing/stock data, unchanged
tco/
  hub.html               new — 3-tile hub
  ownership.html         ported from TOC.html — Spring Clips rename only
  downtime.html          new — Downtime Cost Calculator
  windfarm.html          new — Wind Farm lookup
  data/windfarms.db       new — indexed from Windfarms World Master.xls
```

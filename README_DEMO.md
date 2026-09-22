# BGB CRM — demo build (100% example data)

This is the actual app — every screen, every route, the full UI — running
on completely fabricated data. No real BGB customer names, sales rep
names, pricing, stock levels, or wind farm records are in this build.
This is what to build toward; the two jobs left are wiring in the real
EFACS connection and splitting the front-end out to Netlify.

## What's fake and what's real

**Fabricated (all of it invented, none of it is real BGB data):**
- Customer names, in both Opportunities and Brush (one shared list —
  matches BGB's decision that EFACS feeds a single customer master
  everywhere)
- Sales rep / "assigned to" names
- Every brush part number, description, grade, OEM reference, supplier
  (Gerken) reference, aftermarket code, turbine OEM/platform, and
  dimensions — nothing on a part detail screen traces back to a real BGB
  part or a real product photo. Product images are replaced with a single
  "Image coming soon" placeholder; the real photos aren't in this build
  at all.
- Silver and copper brush pricing — illustrative round figures, not
  derived from BGB's real price list by any formula or scaling factor
- Stock levels
- The wind farm database (300 example records; the real one has ~43,000
  — this is a representative sample, not a scaled copy)
- Two example logged opportunities (one with two activities attached —
  see the Pipeline drawer) and one example brush enquiry, so the screens
  aren't empty when you first open this

**Real (non-identifying industry facts, not BGB commercial data):**
turbine manufacturer/product names in the wind farm database (Vestas,
Siemens Gamesa, etc. — public information); the app's logic, layouts, and
every calculation (margin math, pricing interpolation, search).

## This build is already wired for the ERP integration

`main.py` reads all reference data through `erp_integration.py`'s
`ERPClient` interface (not raw JSON files directly), and the Opportunities
and Brush-enquiry logs save to JSON files via `persistence.py` instead of
living in memory only. Run it as-is (`ERP_MODE=mock`, the default) and
you're looking at exactly this demo. Implementing the real EFACS calls in
`LiveEFACSClient` (same file) and setting `ERP_MODE=live` is the
integration work — no other part of the app needs to change for that.

See `erp_integration.py` and `persistence.py`'s docstrings for the exact
wiring and what's still needed from EFACS (API/DB access, auth, field
mapping — none of that is decided yet, see the conversation with BGB's IT
contact about the SQL Server/VPN approach).

## Running it

```bash
pip install -r requirements.txt
export APP_USERNAME=demo APP_PASSWORD=demo
export ADMIN_USERNAME=admin ADMIN_PASSWORD=admin
export ANTHROPIC_API_KEY=sk-anything   # only used by the AI opportunity-check feature
export SECRET_KEY=anything
uvicorn main:app --reload
```

Then open `http://localhost:8000`, log in with `demo` / `demo`, and click
through Opportunities, Brush, and the TCO calculator. The Pipeline and
Brush enquiry log already have example entries in them.

## A bug this demo build caught and fixed, also present in the live app

While building this, submitting a brush enquiry for any silver-priced
part crashed the server (`KeyError: 'updated_price'`) — a leftover field
name from before the Gerken price list was refreshed; the JSON files now
use `list_price`, but two code paths (one in `main.py`, one in
`brush.html`'s "indicative cost" preview) still referenced the old name.
Fixed in both places in this build. **This same bug is live in the
production app right now** — the same fix (renaming `updated_price` to
`list_price` in those two files) needs to go out there too, separately
from anything to do with this demo or the ERP work.

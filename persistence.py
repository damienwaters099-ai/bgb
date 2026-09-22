"""
JSON-file persistence for the Enquiries and Opportunities logs — placeholder.
==============================================================================

WHAT THIS FILE IS
------------------
Today, `enquiry_log` (brush tool) and `manual_opportunity_log` (opportunities
pipeline) in main.py are plain Python lists — held in memory only. Every
time the app restarts (a deploy, a crash, Render spinning the instance
down when idle) both logs go back to empty. This module replaces the bare
list with a list that's also saved to a JSON file on disk, so it survives
a restart.

HOW TO WIRE THIS INTO main.py
------------------------------
Replace:

    opportunity_log = []
    manual_opportunity_log = []
    ...
    enquiry_log = []

with:

    from persistence import load_records, save_records
    opportunity_log = load_records("opportunity_checks")
    manual_opportunity_log = load_records("opportunities")
    ...
    enquiry_log = load_records("enquiries")

Then, EVERY place the code currently does `xxx_log.insert(0, entry)`, add a
save call straight after it:

    manual_opportunity_log.insert(0, entry)
    save_records("opportunities", manual_opportunity_log)

Same pattern for `enquiry_log.insert(0, entry)` -> save_records("enquiries", ...)
and for the new `/opportunities/log-activity` endpoint, which mutates an
*existing* entry rather than inserting a new one — same idea, just save
after the mutation instead of the insert:

    entry.setdefault("activities", []).insert(0, activity)
    save_records("opportunities", manual_opportunity_log)

That's the whole integration — three call sites, all in main.py.

WHY WHOLE-FILE OVERWRITE, NOT APPEND-ONLY
------------------------------------------
Opportunities get mutated after creation (activities get added to an
existing entry), so a simple append-only log doesn't fit — you'd have to
replay every append to reconstruct current state. Rewriting the whole file
on every save is simpler and, at pilot data volumes (dozens to low
hundreds of entries), effectively instant. If this ever needs to handle
thousands of entries with frequent concurrent writes, that's the point to
move to a real database instead of hand-rolling something more clever
here — see the warning below.

*** IMPORTANT — READ BEFORE ASSUMING THIS "SOLVES" DATA LOSS ***
-------------------------------------------------------------------
Writing to a local JSON file only survives a restart if the FILESYSTEM
itself survives the restart. On Render's standard web service plan, the
local disk is ephemeral — it resets on every deploy and can reset on an
instance restart even without a deploy. Adding this module WITHOUT ALSO
either:

  (a) attaching a Render persistent disk to this service (paid add-on,
      mounted at a fixed path — point DATA_DIR below at that mount), or
  (b) writing to genuinely external storage (a real database, S3, or the
      real EFACS system once that's live and these logs are pushed there
      instead of/as well as kept locally)

...will look like it fixed the data-loss problem in testing (because the
dev box's disk doesn't get wiped) and then quietly lose data in production
on the next deploy. Flag this to whoever owns the Render account before
this goes further than a pilot.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DATA_DIR = Path(os.getenv("APP_DATA_DIR", Path(__file__).resolve().parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _path_for(log_name: str) -> Path:
    safe_name = "".join(c for c in log_name if c.isalnum() or c in ("_", "-"))
    return DATA_DIR / f"{safe_name}.json"


def load_records(log_name: str) -> list[dict]:
    """Read a log's JSON file back into a list. Returns [] if the file
    doesn't exist yet (first run)."""
    path = _path_for(log_name)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Don't crash the app over a corrupt log file — start empty and
        # leave the bad file in place under a .bad suffix for inspection.
        try:
            path.rename(path.with_suffix(".bad"))
        except OSError:
            pass
        return []


def save_records(log_name: str, records: list[dict]) -> None:
    """Overwrite a log's JSON file with the current full list. Writes to a
    temp file first and renames over the target, so a crash mid-write
    can't leave a half-written, unreadable JSON file behind."""
    path = _path_for(log_name)
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

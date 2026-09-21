"""
ira_loaders.py
==============
Maps the raw input sheets (as lists-of-rows) to the parsed structures the engine
expects, and fabricates dummy versions of the tables the source file does not yet
contain (Sovereign rating/outlook, Dispensations, CRA breaches, Interest Rates)
so the whole pipeline runs end-to-end.

`load_tables(sheets)` is the single entry point.  `sheets` is
    {sheet_name: list_of_rows}
and it returns the `tables` dict consumed by ira_build.build_all().

Replace any generate_dummy_* call with a real parser once the corresponding
input table exists - the rest of the pipeline is unaffected.
"""

from __future__ import annotations
from typing import Dict, List, Any
import random

try:                        # inside the IRA package (Dataiku library)
    from . import ira_engine as E
    from . import ira_reftables as RT
except ImportError:         # flat import (local / standalone)
    import ira_engine as E
    import ira_reftables as RT


# Canonical sheet-name matching.  Case/space/punctuation tolerant, BUT the
# distinguishing symbols % and $ are PRESERVED - otherwise '30+%' and '30+$'
# both collapse to '30' and silently overwrite each other (the dollar table
# would be served in place of the percent table).  '+' and spaces are dropped.
def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum() or ch in "%$")


def load_tables(sheets: Dict[str, List[List[Any]]],
                seed: int = 7) -> Dict[str, Any]:
    random.seed(seed)
    idx = {_norm(k): k for k in sheets}

    def get(*names):
        for n in names:
            k = idx.get(_norm(n))
            if k is not None:
                return sheets[k]
        return None

    tables: Dict[str, Any] = {}

    # --- product-block monthly tables ------------------------------------- #
    for key, aliases in {
        "ENR": ["ENR"],
        "30+%": ["30+%", "30 + %", "30%"],
        "30+$": ["30+$", "30 + $", "30$"],
        "90+%": ["90+%", "90 + %", "90%"],
        "90+$": ["90+$", "90 + $", "90$"],
        "RWA": ["RWA"],
        "app_rate": ["Country prod level app rate"],
        "new_approved": ["#monthly new approved", "monthly new approved"],
    }.items():
        raw = get(*aliases)
        if raw is not None:
            tables[key] = E.parse_country_product_block(raw)

    # --- policy exception (side-by-side L2 | L3) -------------------------- #
    raw = get("# policy exception L2 and L3", "policy exception L2 and L3")
    if raw is not None:
        tables["policy_exception"] = E.parse_side_by_side(raw)

    # --- GCO quadrants ---------------------------------------------------- #
    raw = get("GCO %", "GCO")
    if raw is not None:
        tables["gco"] = E.parse_gco_quadrants(raw)

    # --- ECL / IIP / LI (long) ------------------------------------------- #
    raw = get("ECL IIP LI")
    if raw is not None:
        tables["ecl"] = E.parse_long(raw)

    # --- ME EA / AWC (stacked country-only) ------------------------------ #
    raw = get("ME EA AWC")
    if raw is not None:
        tables["ME_EA_AWC"] = E.parse_stacked_country_only(raw)

    # --- PvB EA / AWC ----------------------------------------------------- #
    raw = get("PvB EA AWC")
    if raw is not None:
        tables["PvB_EA_AWC"] = E.parse_stacked_country_only(raw)

    # --- PPI (currency-keyed country-only) -------------------------------- #
    raw = get("PPI")
    if raw is not None:
        tables["PPI"] = E.parse_country_only(raw, header_key="country")

    # --- CCPL volatile (horizontal) -------------------------------------- #
    raw = get("CCPL Volatile by Country")
    if raw is not None:
        tables["ccpl_volatile"] = E.parse_ccpl_volatile(raw)

    # --- LTV > 80 --------------------------------------------------------- #
    raw = get("LTV > 80 Excl MIP", "LTV>80 Excl MIP")
    if raw is not None:
        tables["LTV80"] = E.parse_country_only(raw, header_key="country")

    # --- FX --------------------------------------------------------------- #
    raw = get("Fx Rates used")
    if raw is not None:
        tables["fx"] = E.parse_fx(raw)

    # --- Interest Rates: prefer a real table (matrix or country-only) ----- #
    raw = get("Interest Rates")
    if raw and any(_row_has_content(r) for r in raw):
        tables["interest_rates"] = E.parse_country_only(raw, header_key="country")
    # (may be replaced below by the reference-table matrix parser)

    # --- REAL reference tables (dispensations / breaches / sovereign /
    #     PPI matrix / interest-rate matrix), located by title anywhere in the
    #     provided sheets.  These OVERRIDE the dummy generators. -------------- #
    ref = RT.parse_reference_tables(sheets)
    for key in ("dispensations", "cra_breaches", "sovereign"):
        if ref.get(key):
            tables[key] = ref[key]

    # --- DEDICATED sovereign (Country Outlook + Grading) pipeline ---------- #
    try:
        from . import ira_sovereign as SOV
    except ImportError:
        import ira_sovereign as SOV
    sov_data, sov_report = SOV.parse(sheets)
    tables["sovereign_report"] = sov_report
    if sov_data:
        tables["sovereign"] = sov_data

    # --- DEDICATED dispensation (1f) pipeline ------------------------------ #
    try:
        from . import ira_dispensations as DSP
    except ImportError:
        import ira_dispensations as DSP
    disp_data, disp_report = DSP.parse(sheets)
    tables["dispensations_report"] = disp_report
    if disp_data:
        tables["dispensations"] = disp_data
    if ref.get("ppi"):                 # country-keyed matrix beats currency PPI
        tables["PPI"] = ref["ppi"]
        tables["PPI_by_country"] = True
    if ref.get("interest_rates"):
        tables["interest_rates"] = ref["interest_rates"]

    # --- WM_Shortfall (Wealth Lending Private Banking shortfall tables) ----- #
    raw = get("WM_Shortfall", "WM Shortfall", "WMShortfall")
    if raw is None:                       # fuzzy: any sheet whose name mentions shortfall
        for k in sheets:
            if "shortfall" in _norm(k):
                raw = sheets[k]
                break
    if raw is not None:
        tables["wm_shortfall"] = parse_wm_shortfall(raw)

    # --- fabricate anything still missing (so the pipeline always runs) ---- #
    countries = _all_countries(tables)
    tables.setdefault("sovereign", None)
    if not tables.get("sovereign"):
        tables["sovereign"] = _dummy_sovereign(countries)
    if not tables.get("dispensations"):
        tables["dispensations"] = _dummy_dispensations(countries)
    if not tables.get("cra_breaches"):
        tables["cra_breaches"] = _dummy_cra_breaches(countries)
    if not tables.get("interest_rates"):
        tables["interest_rates"] = _dummy_interest_rates(tables)

    return tables


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def parse_wm_shortfall(rows: List[List[Any]]) -> Dict[str, Dict[str, float]]:
    """Parse the two Private Banking shortfall tables (securities/margin trading
    and real estate) into
        {'securities': {country: amount, '__total__': X},
         'real_estate': {country: amount, '__total__': Y}}.
    Detection is tolerant of title wording, row gaps and column layout: each
    table's per-country figure is the 'Amount' column of its 'Total' row, and
    '__total__' is the 'Total Amount' column of that same row."""
    def cell(r, c):
        return rows[r][c] if 0 <= r < len(rows) and 0 <= c < len(rows[r]) else None

    def num_cell(v):
        """Coerce a shortfall amount to a number: handles ints/floats, and text
        cells like '28,755', '$4,740', ' 1600 '.  Returns None when not numeric."""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return v
        s = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
        if s == "" or s in ("-", "n/a", "N/A", "na", "NA"):
            return None
        try:
            return float(s)
        except Exception:
            return None

    def norm(v):
        return "".join(ch for ch in str(v).lower() if ch.isalnum())

    def find_title(*needles):
        """First row containing a cell whose normalised text holds all needles."""
        wants = [norm(n) for n in needles]
        for r in range(len(rows)):
            for c in range(min(8, len(rows[r]))):
                v = cell(r, c)
                if isinstance(v, str):
                    nv = norm(v)
                    if all(w in nv for w in wants):
                        return r
        return None

    def parse_table(title_row):
        out = {}
        if title_row is None:
            return out
        # 1) the 'Total' row: label 'Total' (or 'Total Shortfall') in the first
        #    few columns AND carrying numeric data (so a split 'Total client' /
        #    'Total Amount' header cell is never mistaken for the total row).
        total_row = None
        for r in range(title_row + 1, min(title_row + 30, len(rows))):
            has_total = any(isinstance(cell(r, c), str) and str(cell(r, c)).strip().lower() in ("total", "total shortfall")
                            for c in range(min(4, len(rows[r]))))
            has_num = any(num_cell(cell(r, c)) is not None for c in range(len(rows[r])))
            if has_total and has_num:
                total_row = r
                break
        if total_row is None:
            return out
        # 2) the sub-header row that labels the Clients/Amount pairs: the LAST row
        #    above the Total row carrying exact 'Amount' cells.
        sub = None
        for r in range(title_row + 1, total_row):
            if any(isinstance(cell(r, c), str) and str(cell(r, c)).strip().lower() == "amount"
                   for c in range(len(rows[r]))):
                sub = r
        if sub is None:
            sub = min(title_row + 2, total_row - 1)
        # 3) header window = every row from just under the title down to the
        #    sub-header; a column's header is those cells joined (so a header split
        #    across rows - 'Total' above 'Amount' - reads as one string).
        hwin = list(range(title_row + 1, sub + 1))
        width = max((len(rows[r]) for r in hwin + [total_row]), default=0)

        def combined(c):
            parts = [str(cell(r, c)).strip() for r in hwin
                     if isinstance(cell(r, c), str) and str(cell(r, c)).strip()]
            return " ".join(parts).lower()

        # 4) Total-Amount column (the GROUP figure): header holds BOTH 'total' and
        #    'amount' but not 'client' (so 'Total client' is skipped).
        total_amt_col = None
        for c in range(width):
            cm = combined(c)
            if "total" in cm and "amount" in cm and "client" not in cm:
                total_amt_col = c
                break
        # 5) per-country Amount columns: sub-header cell == 'amount'; the country
        #    name is the nearest non-label header cell in this column, else in the
        #    paired 'Clients' column to its left.
        amt_cols = {}
        _labels = {"amount", "clients", "client", "total", "usd'000", "(usd'000)"}
        for c in range(width):
            s = cell(sub, c)
            if not (isinstance(s, str) and s.strip().lower() == "amount"):
                continue
            name = None
            for src in (c, c - 1):
                for r in hwin:
                    v = cell(r, src)
                    if isinstance(v, str) and v.strip() and v.strip().lower() not in _labels:
                        name = v.strip()
                if name:
                    break
            if name and "total" not in name.lower() and c != total_amt_col:
                amt_cols[name] = c
        # 6) read the figures off the Total row
        if total_amt_col is not None:
            out["__total__"] = num_cell(cell(total_row, total_amt_col))
        for country, c in amt_cols.items():
            val = num_cell(cell(total_row, c))
            if val is not None:
                out[country] = val
        return out

    sec = parse_table(find_title("securities") or find_title("securit") or find_title("margin"))
    re_ = parse_table(find_title("real", "estate") or find_title("realestate"))
    return {"securities": sec, "real_estate": re_}


def _row_has_content(r) -> bool:
    return any(c not in (None, "") for c in r)


_NON_COUNTRY = {"total", "group", "country and product", "country",
                "grand total", "total products"}


def _is_country_label(c: str) -> bool:
    if not isinstance(c, str) or not c.strip():
        return False
    low = c.strip().lower()
    if low in _NON_COUNTRY or low.startswith("total"):
        return False
    return True


def _all_countries(tables) -> List[str]:
    """Countries come from the core product-block tables (ENR/DPD), which carry
    the real country + product structure.  Currency-keyed tables (PPI) and
    totals are deliberately excluded."""
    seen: List[str] = []
    for key in ("ENR", "90+%", "30+%", "90+$", "30+$", "RWA"):
        t = tables.get(key)
        if not t:
            continue
        for c in t.countries():
            if _is_country_label(c) and c not in seen:
                seen.append(c)
    return seen or ["Bahrain", "China", "Falklands"]


# --------------------------------------------------------------------------- #
#  Dummy generators for tables not present in the source workbook.
#  Each is clearly a placeholder; swap for a real parser when data arrives.
# --------------------------------------------------------------------------- #
_OUTLOOKS = ["Positive", "Stable", "Negative"]
_GRADES = ["1A", "2A", "5B", "6A", "7B", "8A", "8B", "9A", "10A", "11A", "12B", "13"]


def _dummy_sovereign(countries) -> Dict[str, Dict[str, str]]:
    out = {}
    for c in countries:
        out[c] = {"outlook": random.choice(_OUTLOOKS),
                  "fcy_crg": random.choice(_GRADES)}
    return out


def _dummy_dispensations(countries) -> Dict[str, Dict[str, int]]:
    # per-category: {category: {country: active count}}
    return {cat: {c: random.randint(0, 4) for c in countries}
            for cat in E.CATEGORIES}


def _dummy_dispensations(countries) -> Dict[str, Dict[str, int]]:
    # per-category: {category: {country: count}}
    return {cat: {c: random.randint(0, 4) for c in countries}
            for cat in E.CATEGORIES}


def _dummy_cra_breaches(countries) -> Dict[str, Dict[str, int]]:
    # per-category: {category: {country: count_last_12m}}
    return {cat: {c: random.randint(0, 3) for c in countries}
            for cat in E.CATEGORIES}


def _dummy_interest_rates(tables):
    """Build a MonthTable of country monthly interest rates."""
    months_src = None
    for key in ("ENR", "90+%", "PPI"):
        if tables.get(key):
            months_src = tables[key].months
            break
    months = months_src or [f"M{i}" for i in range(13)]
    countries = _all_countries(tables)
    data = {}
    for c in countries:
        base = random.uniform(0.02, 0.05)
        data[c] = {m: round(base + random.uniform(-0.005, 0.02), 5)
                   for m in months}
    return E.MonthTable(months, {}, data, {})

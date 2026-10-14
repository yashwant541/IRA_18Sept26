"""
ira_inputs.py
=============
Flatten every parsed INPUT table into one long, tidy table:

    Period | Type | Country | Segment | Value | Month

* Period  - the run's reporting period (e.g. "Mar-26"), same on every row.
* Type    - which input table the value came from (ENR, 30+$, PPI, Sovereign, ...).
* Country - the country (or country code / "Grand Total" for roll-up rows).
* Segment - the product / sub-table (Secured, Unsecured, SME Banking,
            Wealth Lending, EA, AWC, L2, L3, a category, an attribute ...),
            blank for plain country-only tables.
* Value   - the raw input value (number, or text for sovereign outlook/grade).
* Month   - the month the value belongs to, in MMM-YY form (Mar-26); for
            reference tables that have no monthly dimension this is the
            current reporting month.

This is a faithful dump of what the engine read from the uploaded files, so a
user can download and audit every input in a single flat sheet.
"""

from __future__ import annotations
from typing import Dict, List, Any, Optional
import re

try:
    from . import ira_engine as E
except ImportError:
    import ira_engine as E

COLUMNS = ["Period", "Type", "Country", "Segment", "Value", "Month"]

# internal table-key -> human-readable Type shown in the export
TYPE_NAMES = {
    "ENR": "ENR",
    "30+%": "30+%", "30+$": "30+$", "90+%": "90+%", "90+$": "90+$",
    "RWA": "RWA",
    "app_rate": "Country prod level app rate",
    "new_approved": "# Monthly New Approved",
    "PPI": "PPI",
    "LTV80": "LTV > 80 Excl MIP",
    "interest_rates": "Interest Rates",
    "ME_EA_AWC": "ME EA AWC",
    "PvB_EA_AWC": "PvB EA AWC",
    "policy_exception": "Policy Exception L2/L3",
    "gco": "GCO %",
    "ccpl_volatile": "CCPL Volatile by Country",
    "fx": "FX Rates",
    "sovereign": "Sovereign Rating & Outlook",
    "dispensations": "Active Dispensations",
    "cra_breaches": "CRA Breaches",
    "wm_shortfall": "WM Shortfall",
}

# how each table key is shaped, so we flatten it correctly
_MT_KEYS = {"ENR", "30+%", "30+$", "90+%", "90+$", "RWA", "app_rate",
            "new_approved", "PPI", "LTV80", "interest_rates"}
_MT_DICT_KEYS = {"gco", "ME_EA_AWC", "PvB_EA_AWC", "policy_exception"}
_BYCAT_KEYS = {"dispensations", "cra_breaches"}          # {category: {country: value}}
_NUM_KEYS = {"ccpl_volatile", "fx"}                      # {code: number}
_SOV_ATTR = {"outlook": "Outlook", "grading": "Grading", "fcy": "FCY CRG",
             "lcy": "LCY CRG", "fcy_crg": "FCY CRG", "lcy_crg": "LCY CRG"}


def _clean_seg_title(s) -> str:
    """Short, readable segment label for a stacked/side-by-side sub-table title."""
    t = str(s).strip()
    low = t.lower()
    if "policy exception" in low or "policy exceptions" in low:
        if "l2" in low:
            return "L2"
        if "l3" in low:
            return "L3"
    t = re.sub(r"\s*\(?\s*in\s*\$?\s*mn\s*\)?\s*$", "", t, flags=re.I)   # drop "in $mn"
    t = re.sub(r"\s*\(?\$?mn\)?\s*$", "", t, flags=re.I)
    return t.strip() or str(s)


def _mlabel(m) -> str:
    try:
        return E.fmt_month(m)
    except Exception:
        return str(m)


def _seg(*parts) -> str:
    return " / ".join(str(p).strip() for p in parts if p not in (None, ""))


def _current_month_label(tables) -> str:
    """Latest month across the monthly tables, as a MMM-YY label (for reference
    tables that carry no month of their own)."""
    for k in ("ENR", "30+$", "90+$", "30+%"):
        mt = tables.get(k)
        months = getattr(mt, "months", None)
        if months:
            return _mlabel(months[-1])
    for v in tables.values():
        months = getattr(v, "months", None)
        if months:
            return _mlabel(months[-1])
    return ""


def _flatten_monthtable(type_, mt, out, period, seg_prefix=""):
    months = list(getattr(mt, "months", []) or [])
    pdata = getattr(mt, "product_data", {}) or {}
    cdata = getattr(mt, "country_data", {}) or {}
    gtot = getattr(mt, "grand_total", {}) or {}
    for (country, product), series in pdata.items():
        seg = _seg(seg_prefix, E.out_product_name(product))
        for m in months:
            _emit(out, period, type_, country, seg, series.get(m), _mlabel(m))
    # country rows: a per-country TOTAL when product rows also exist, else the data itself
    seg_country = _seg(seg_prefix, "Total") if pdata else seg_prefix
    for country, series in cdata.items():
        for m in months:
            _emit(out, period, type_, country, seg_country, series.get(m), _mlabel(m))
    for m in months:
        if gtot.get(m) is not None:
            _emit(out, period, type_, "Grand Total", _seg(seg_prefix, "Total"),
                  gtot.get(m), _mlabel(m))


def _emit(out, period, type_, country, segment, value, month):
    if value is None or value == "":
        return
    out.append({"Period": period, "Type": type_, "Country": str(country),
                "Segment": segment or "", "Value": value, "Month": month})


def input_long_rows(tables: Dict[str, Any], period: str) -> List[Dict[str, Any]]:
    """Flatten all input tables to long rows (list of dicts, COLUMNS keys)."""
    out: List[Dict[str, Any]] = []
    cur = _current_month_label(tables)

    for key, type_ in TYPE_NAMES.items():
        tbl = tables.get(key)
        if tbl is None:
            continue
        try:
            if key in _MT_KEYS:
                _flatten_monthtable(type_, tbl, out, period)

            elif key in _MT_DICT_KEYS and isinstance(tbl, dict):
                for sub, v in tbl.items():
                    if str(sub).endswith("_title"):
                        continue
                    if getattr(v, "months", None) is not None:      # a MonthTable
                        seg_base = tbl.get(f"{sub}_title", sub)
                        seg_base = _clean_seg_title(E.out_product_name(seg_base))
                        _flatten_monthtable(type_, v, out, period, seg_prefix=seg_base)

            elif key in _BYCAT_KEYS and isinstance(tbl, dict):
                for cat, inner in tbl.items():
                    if not isinstance(inner, dict):
                        continue
                    for country, val in inner.items():
                        if str(country).startswith("__"):
                            continue
                        if isinstance(val, dict):                   # one level deeper
                            for k2, v2 in val.items():
                                _emit(out, period, type_, country, _seg(cat, k2), v2, cur)
                        else:
                            _emit(out, period, type_, country, str(cat), val, cur)

            elif key == "sovereign" and isinstance(tbl, dict):
                for country, rec in tbl.items():
                    if not isinstance(rec, dict):
                        continue
                    for attr, val in rec.items():
                        _emit(out, period, type_, country,
                              _SOV_ATTR.get(attr, str(attr)), val, cur)

            elif key == "wm_shortfall" and isinstance(tbl, dict):
                for sub, inner in tbl.items():
                    if not isinstance(inner, dict):
                        continue
                    seg = {"securities": "Securities",
                           "real_estate": "Real Estate"}.get(sub, str(sub))
                    for country, val in inner.items():
                        ctry = "Total" if str(country).startswith("__") else country
                        _emit(out, period, type_, ctry, seg, val, cur)

            elif key in _NUM_KEYS and isinstance(tbl, dict):
                for code, val in tbl.items():
                    _emit(out, period, type_, code, "", val, cur)

            elif key == "ecl":
                _flatten_dataframe(type_, tbl, out, period, cur)
        except Exception:
            # never let one odd table break the whole export
            continue

    return out


def _flatten_dataframe(type_, df, out, period, cur):
    """Best-effort flatten of a long/tabular input (ECL IIP LI)."""
    try:
        import pandas as pd  # noqa
    except Exception:
        return
    if df is None or getattr(df, "empty", True):
        return
    cols = {str(c).strip().lower(): c for c in df.columns}
    ccol = next((cols[k] for k in cols if k in ("country", "entity")), None)
    scol = next((cols[k] for k in cols if k in ("product", "segment", "metric", "measure")), None)
    mcol = next((cols[k] for k in cols if k in ("month", "date", "period")), None)
    vcol = next((cols[k] for k in cols if k in ("value", "amount")), None)
    if ccol and vcol:                       # already long-ish
        for _, r in df.iterrows():
            _emit(out, period, type_, r.get(ccol),
                  (r.get(scol) if scol else ""),
                  r.get(vcol), _mlabel(r.get(mcol)) if mcol else cur)
        return
    # wide: id columns = non-numeric, value columns = the rest (melt)
    id_cols = [c for c in df.columns if df[c].dtype == object]
    val_cols = [c for c in df.columns if c not in id_cols]
    if not val_cols:
        return
    key_col = ccol or (id_cols[0] if id_cols else None)
    for _, r in df.iterrows():
        country = r.get(key_col) if key_col is not None else ""
        seg = _seg(*[str(r.get(c)) for c in id_cols if c != key_col])
        for vc in val_cols:
            _emit(out, period, type_, country, seg, r.get(vc), _mlabel(vc))


def input_dataframe(tables: Dict[str, Any], period: str):
    """Return the long input dump as a pandas DataFrame (COLUMNS order)."""
    import pandas as pd
    rows = input_long_rows(tables, period)
    return pd.DataFrame(rows, columns=COLUMNS)

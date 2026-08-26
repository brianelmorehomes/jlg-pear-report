"""
Extracts property/equity data from a Remine public-records report (the
4-page "Courtesy of [Agent]" branded export pulled through MLS/MRED).

This is intentionally built against ONE report format, not a generic PDF
scraper: Remine's report is the only source that gives us an actual loan
balance / net equity / percent equity for a property with zero manual
entry required, which is what makes the PEAR report's Equity Breakdown
section possible without asking the agent to know the client's mortgage
by heart. (RPR reports -- the other common source agents pull -- never
carry loan data at all, so a parser built around RPR would still need a
manual mortgage-balance field for every single report. Remine's report
covers that gap, so it's the only source this app supports. See the
Cowork conversation this was built in for the comparison.)

Every extractor below is regex-based against pdfplumber's page-by-page
text output and fails soft: a field that can't be found comes back as
None (or "" for strings) rather than raising, since the whole point of
the app's review screen is that the agent can fill in or correct
anything the parser missed. Nothing here should ever crash the upload
just because one property's report is missing a section (e.g. a
property with no active mortgage on record simply won't have an "Active
Mortgage" block at all -- that's a real, valid state, not a parse
failure).
"""
import re

import pdfplumber

MONEY_RE = r"\$?([\d,]+(?:\.\d+)?)"


def _to_number(s):
    """'$1,243,000' / '1,243,000' / '74' -> float. None-safe."""
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int_money(s):
    n = _to_number(s)
    return int(round(n)) if n is not None else None


def _clean_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _first(pattern, text, flags=0, group=1):
    m = re.search(pattern, text, flags)
    return m.group(group) if m else None


def _format_owner_names(raw):
    """Remine prints owner names in public-record order -- 'LAST FIRST
    MIDDLE, LAST2 FIRST2 MIDDLE2' (e.g. 'JENKS NATHAN, UMIEL MARK R').
    This is a best-effort reformat into a friendlier 'First Last and
    First2 Last2' for the client-name suggestion on the intake form --
    it does NOT need to be perfect, since the agent reviews/overwrites
    the client name before anything is generated. Falls back to a
    title-cased version of the raw string if the split looks unusual
    (e.g. a business/trust entity name instead of a person)."""
    if not raw:
        return ""
    people = [p.strip() for p in raw.split(",") if p.strip()]
    out = []
    for p in people:
        parts = p.split()
        if len(parts) >= 2:
            last = parts[0]
            first = parts[1]
            out.append(f"{first.title()} {last.title()}")
        else:
            out.append(p.title())
    if len(out) == 2:
        return f"{out[0]} and {out[1]}"
    return ", ".join(out)


def parse_remine_pdf(file_bytes):
    """Returns a plain dict of extracted fields. Every key is present;
    values are None/"" when not found in this particular report so the
    review form always has a consistent shape to render."""
    with pdfplumber.open(__import__("io").BytesIO(file_bytes)) as pdf:
        pages_text = [p.extract_text() or "" for p in pdf.pages]

    full_text = "\n".join(pages_text)
    page1 = pages_text[0] if pages_text else ""
    page2 = pages_text[1] if len(pages_text) > 1 else ""
    page3 = pages_text[2] if len(pages_text) > 2 else ""

    data = {
        "source_format": "remine",
        "parse_warnings": [],
    }

    # ---- Header line: "Off-Market 5541 N MAGNOLIA AVE CHICAGO IL 60640" ----
    first_line = (page1.split("\n") or [""])[0]
    m = re.match(r"^(.*?)\s+(\d+\s+\S.*)$", first_line)
    if m:
        data["status"] = _clean_ws(m.group(1))
        data["full_address"] = _clean_ws(m.group(2))
    else:
        data["status"] = ""
        data["full_address"] = _clean_ws(first_line)
        data["parse_warnings"].append("Could not split status from address on header line.")

    # ---- Stat line: beds/baths/sqft/acres/value/equity/type ----
    stat_m = re.search(
        r"(\d+)\s*Beds?\s*\S\s*(\d+)\s*Baths?\s*\S\s*([\d,]+)\s*SF\s*\S\s*([\d.]+)\s*Acres\s*\S\s*"
        r"\$([\d,]+)\s*Est Value\s*\S\s*\$([\d,]+)\s*Net Equity\s*\S\s*(.+)",
        full_text,
    )
    if stat_m:
        data["beds"] = _to_int_money(stat_m.group(1))
        data["baths"] = _to_int_money(stat_m.group(2))
        data["sqft"] = _to_int_money(stat_m.group(3))
        data["lot_acres"] = _to_number(stat_m.group(4))
        data["value_est"] = _to_int_money(stat_m.group(5))
        data["net_equity_est"] = _to_int_money(stat_m.group(6))
        data["property_type"] = _clean_ws(stat_m.group(7))
    else:
        for k in ("beds", "baths", "sqft", "lot_acres", "value_est", "net_equity_est"):
            data[k] = None
        data["property_type"] = ""
        data["parse_warnings"].append("Could not parse the beds/baths/value summary line.")

    # ---- Owned for X years / owner names ----
    owned_m = re.search(r"Owned for ([\d.]+) years?\s*\S\s*(.+)", full_text)
    if owned_m:
        data["owned_years"] = _to_number(owned_m.group(1))
        data["owner_names_raw"] = _clean_ws(owned_m.group(2))
        data["owner_names_display"] = _format_owner_names(data["owner_names_raw"])
    else:
        data["owned_years"] = None
        data["owner_names_raw"] = ""
        data["owner_names_display"] = ""

    # ---- Year built ----
    data["year_built"] = _to_int_money(_first(r"Year Built\s+(\d{4})", full_text))

    # ---- Value / Loan Balance / Net Equity block ("Net Equity" panel) ----
    data["value_est_detail"] = _to_int_money(_first(r"Value\s+\$([\d,]+)\s+est\.", full_text))
    data["loan_balance_est"] = _to_int_money(_first(r"Loan Balance\s+\$([\d,]+)\s+est\.", full_text))
    net_equity_detail = _to_int_money(_first(r"Net Equity\s+\$([\d,]+)\s+est\.", full_text))
    if net_equity_detail is not None:
        data["net_equity_est"] = net_equity_detail
    data["percent_equity"] = _to_int_money(_first(r"Percent Equity\s+(\d+)%\s*est\.", full_text))

    # ---- Active Mortgage block (may not exist -- paid-off property) ----
    mort_start = full_text.find("Active Mortgage")
    if mort_start != -1:
        mort_end = full_text.find("Flood Risk", mort_start)
        mort_chunk = full_text[mort_start: mort_end if mort_end != -1 else mort_start + 500]
        data["mortgage_orig_amount"] = _to_int_money(_first(r"Orig\.\s*Amount\s+\$([\d,]+)", mort_chunk))
        data["mortgage_term_years"] = _to_int_money(_first(r"Loan Term\s+(\d+)\s*Yrs", mort_chunk))
        data["mortgage_rate"] = _to_number(_first(r"Rate\s+([\d.]+)%", mort_chunk))
        data["mortgage_type"] = _clean_ws(_first(r"Loan Type\s+([A-Za-z][A-Za-z ]*?)\n", mort_chunk) or "")
        lender_m = re.search(r"Lender\s+(.+?)(?:\n\n|\nFlood Risk|$)", mort_chunk, re.S)
        if lender_m:
            data["mortgage_lender"] = _clean_ws(lender_m.group(1))
        else:
            data["mortgage_lender"] = ""
    else:
        data["mortgage_orig_amount"] = None
        data["mortgage_term_years"] = None
        data["mortgage_rate"] = None
        data["mortgage_type"] = ""
        data["mortgage_lender"] = ""
        data["parse_warnings"].append("No active mortgage found on record (property may be paid off, or the loan simply isn't in public filings).")

    # ---- Valuation block: 3 AVMs (First American / Zillow / Remine) ----
    val_start = full_text.find("Valuation\nFirst American")
    val_end = full_text.find("Property History", val_start) if val_start != -1 else -1
    if val_start != -1:
        val_chunk = full_text[val_start: val_end if val_end != -1 else val_start + 400]
        amounts = re.findall(r"\$([\d,]+(?:\.\d+)?)", val_chunk)
        amounts = [_to_int_money(a) for a in amounts]
        keys = ["first_american", "zillow", "remine"]
        data["valuations"] = {}
        if len(amounts) >= 9:
            for i, key in enumerate(keys):
                data["valuations"][key] = {
                    "est": amounts[i],
                    "low": amounts[3 + i * 2],
                    "high": amounts[4 + i * 2],
                }
        else:
            data["parse_warnings"].append("Could not fully parse the 3-AVM valuation table.")
    else:
        data["valuations"] = {}

    # ---- Most recent sale (first Transaction block = most recent, Remine
    # sorts Property History newest-first) ----
    ph_start = full_text.find("Property History")
    if ph_start != -1:
        ph_chunk = full_text[ph_start:ph_start + 1500]
        date_m = re.search(r"Transaction Date Document ID Book Page\s*\n(\d{2}/\d{2}/\d{2})", ph_chunk)
        price_m = re.search(r"Sold\s+.*?\n?\$([\d,]+)", ph_chunk)
        data["last_sale_date"] = date_m.group(1) if date_m else None
        data["last_sale_price"] = _to_int_money(price_m.group(1)) if price_m else None
    else:
        data["last_sale_date"] = None
        data["last_sale_price"] = None

    # ---- Owner/associated-person contact (best-effort, for the client
    # intake suggestion -- not required for the report itself) ----
    assoc_m = re.search(
        r"Associated People\s*\n(.+?)\s*,\s*Owner\s*\n\(?(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})[^\n]*\n([\w.\-]+@[\w.\-]+)",
        full_text,
    )
    if assoc_m:
        data["contact_name"] = _clean_ws(assoc_m.group(1)).title()
        data["contact_phone"] = f"({assoc_m.group(2)}) {assoc_m.group(3)}-{assoc_m.group(4)}"
        data["contact_email"] = assoc_m.group(5).lower()
    else:
        data["contact_name"] = ""
        data["contact_phone"] = ""
        data["contact_email"] = ""

    return data


def detect_and_parse(file_bytes, filename=""):
    """Entry point app.py calls. Currently Remine-only per Brian's call --
    RPR support was scoped out since Remine covers everything RPR does
    for market valuation PLUS the loan/equity data RPR never has (see
    module docstring). If a non-Remine PDF is uploaded, this still
    attempts the Remine parse (harmless -- it'll just come back mostly
    empty with warnings) so the agent lands on a normal, editable review
    screen instead of a hard error, and can fill in every field by hand."""
    data = parse_remine_pdf(file_bytes)
    if not data.get("full_address") and not data.get("value_est"):
        data["parse_warnings"].append(
            "This doesn't look like a Remine public-records report -- little or nothing could be extracted. "
            "You can still fill in every field manually below."
        )
    return data

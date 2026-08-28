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
    # This running header repeats as the first line of every page in the
    # "Public Record Full" print view, starting from page 1. But other
    # print views (e.g. "Agent Full", which some properties only offer --
    # Public Record Full isn't available for every record) insert a plain
    # cover page in front of it instead ("Report Created on [date]" /
    # address / agent info, no status+address combo line at all), pushing
    # the real running header to page 2 or later. Rather than assume it's
    # always page 1, scan every page's first line and use the first one
    # that actually matches the pattern -- cheap, and works for both
    # layouts without needing to hard-code which print view produced the
    # upload.
    header_m = None
    for pg in pages_text:
        if not pg:
            continue
        line0 = pg.split("\n")[0]
        m = re.match(r"^(.*?)\s+(\d+\s+\S.*)$", line0)
        if m:
            header_m = m
            break
    if header_m:
        data["status"] = _clean_ws(header_m.group(1))
        data["full_address"] = _clean_ws(header_m.group(2))
    else:
        data["status"] = ""
        data["full_address"] = ""
        data["parse_warnings"].append("Could not find the status/address header line on any page.")

    # ---- Stat line: beds/baths/sqft/acres/value/equity/type ----
    # This used to be ONE big regex matching the whole line as a fixed
    # template (beds -> divider -> baths -> divider -> sqft -> divider ->
    # acres -> "Acres" -> divider -> value -> "Est Value" -> divider ->
    # equity -> "Net Equity" -> divider -> type). Three real reports in a
    # row each broke that template in a different spot: one dropped the
    # "|" divider before "Est Value", one dropped the "Acres" label
    # entirely (just the bare decimal number), and there's no guarantee
    # the next report won't drop something else. A rigid all-or-nothing
    # match means any single format quirk blanks out EVERY field on the
    # line, even the ones that were printed in a perfectly normal way.
    #
    # So each field below is found independently by searching for its own
    # label, the same way the AVM valuation table and Active Mortgage
    # block are parsed elsewhere in this file. That way a missing divider
    # or label only affects the one field it touches, not the whole line.
    def _label_num(pattern):
        m = re.search(pattern, full_text)
        return m.group(1) if m else None

    beds_s = _label_num(r"(\d+)\s*Beds?\b")
    baths_s = _label_num(r"(\d+)\s*Baths?\b")
    sqft_s = _label_num(r"([\d,]+)\s*SF\b")
    value_s = _label_num(r"\$([\d,]+)\s*Est\s*Value\b")
    equity_s = _label_num(r"\$([\d,]+)\s*Net\s*Equity\b")

    # Lot size: prefer an explicit "X Acres" label. If that's missing
    # (seen on a real report -- just the bare decimal sitting between the
    # SF figure and the value dollar amount, no label at all), fall back
    # to grabbing whatever decimal number sits in that same spot.
    lot_s = _label_num(r"([\d.]+)\s*Acres\b")
    if lot_s is None and sqft_s and value_s:
        between_m = re.search(
            re.escape(sqft_s) + r"\s*SF\s*\S?\s*([\d.]+)\s*\S?\s*\$" + re.escape(value_s),
            full_text,
        )
        if between_m:
            lot_s = between_m.group(1)

    # Property type is free text with no fixed vocabulary, so it's still
    # grabbed positionally -- everything after "Net Equity" (plus an
    # optional single divider char) up to end of line.
    ptype_s = None
    type_m = re.search(r"Net\s*Equity\s*\S?\s*(.+)", full_text)
    if type_m:
        ptype_s = type_m.group(1)

    if beds_s and baths_s and sqft_s and value_s and equity_s:
        data["beds"] = _to_int_money(beds_s)
        data["baths"] = _to_int_money(baths_s)
        data["sqft"] = _to_int_money(sqft_s)
        data["lot_acres"] = _to_number(lot_s) if lot_s else None
        data["value_est"] = _to_int_money(value_s)
        data["net_equity_est"] = _to_int_money(equity_s)
        prop_type_raw = _clean_ws(ptype_s or "")
        # Some reports tack extra segments onto the end of the stat line
        # after the property type (e.g. "Single Family Residential | HOA
        # $370") -- strip anything from a trailing "HOA $..." marker
        # onward so it doesn't get displayed as part of the property type.
        prop_type_raw = re.split(r"\s*\S?\s*HOA\s*\$", prop_type_raw)[0].strip()
        data["property_type"] = prop_type_raw
        if lot_s is None:
            data["parse_warnings"].append("Could not determine lot size (acres) from the summary line -- please double-check/fill in manually.")
    else:
        for k in ("beds", "baths", "sqft", "lot_acres", "value_est", "net_equity_est"):
            data[k] = None
        data["property_type"] = ""
        data["parse_warnings"].append("Could not parse the beds/baths/value summary line.")

    # ---- Owned for X years / owner names ----
    # The owner-name line is always the line immediately after the
    # beds/baths/value stat line -- true in every sample seen, whether or
    # not Remine attached an "Owned for X years" duration prefix to it.
    # A property with no ownership-tenure data on record just prints the
    # bare name with no prefix at all (e.g. "SCHWEIZER GREGORY" alone,
    # nothing before it). This used to be a standalone search for the
    # literal phrase "Owned for ... years" anywhere in the document,
    # which came back completely empty in two real cases: (1) that
    # no-prefix case, since there's no such phrase to find at all, and
    # (2) "Owned for 30+ years" -- the "+" broke the "\d+ years?" match
    # outright. Anchoring on position (right after the stat line) instead
    # of that phrase fixes both, and is also safer than anchoring near
    # the later "Taxes" section heading, which can be pages away from the
    # owner line in the "Agent Full" cover-page print view and would pick
    # up unrelated text sitting just above it there.
    owner_line = None
    if type_m:
        after_stat = full_text[type_m.end():]
        line_m = re.match(r"\s*\n([^\n]+)", after_stat)
        if line_m:
            owner_line = line_m.group(1).strip()

    if owner_line:
        dur_m = re.match(r"Owned for\s+([\d.]+)\+?\s*years?\s*\S?\s*(.+)$", owner_line)
        if dur_m:
            data["owned_years"] = _to_number(dur_m.group(1))
            data["owner_names_raw"] = _clean_ws(dur_m.group(2))
        else:
            data["owned_years"] = None
            data["owner_names_raw"] = _clean_ws(owner_line)
        data["owner_names_display"] = _format_owner_names(data["owner_names_raw"])
    else:
        data["owned_years"] = None
        data["owner_names_raw"] = ""
        data["owner_names_display"] = ""
        data["parse_warnings"].append("Could not find the owner name on record -- please fill it in manually.")

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
    # Some reports have a refi/2nd-lien history and print BOTH "Active
    # Mortgage" (the current/primary loan) and "Active Mortgage 2" (an
    # older or secondary lien) -- and thanks to a 2-column page layout
    # that pdfplumber's linear text extraction doesn't preserve, the
    # FIRST "Active Mortgage" heading in the raw text can land on a
    # broken/interleaved fragment (mixed in with Flood Risk column data)
    # rather than the real, data-rich block. The real block consistently
    # shows up as the LAST "Active Mortgage" heading before "Active
    # Mortgage 2" (or the last one overall, for the common single-loan
    # case). Scoping the chunk to end at whichever of "Active Mortgage 2"
    # / "Flood Risk" / "Valuation" comes first afterward keeps this block
    # from ever bleeding into the second lien's details (e.g. accidentally
    # showing the 2nd lien's lender as if it were the primary loan's).
    am_heading_positions = [m.start() for m in re.finditer(r"Active Mortgage(?!\s*2)\b", full_text)]
    mort_start = am_heading_positions[-1] if am_heading_positions else -1
    if mort_start != -1:
        boundary_candidates = [
            pos for pos in (
                full_text.find("Active Mortgage 2", mort_start + 1),
                full_text.find("Flood Risk", mort_start + len("Active Mortgage")),
                full_text.find("Valuation", mort_start),
            )
            if pos != -1
        ]
        mort_end = min(boundary_candidates) if boundary_candidates else -1
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
        if "Active Mortgage 2" in full_text:
            data["parse_warnings"].append(
                "This property shows more than one loan on record (a refinance and/or "
                "2nd lien). The loan-detail fields above reflect only the primary loan -- "
                "the mortgage balance used for the equity math already combines both, but "
                "double-check the loan details before sending."
            )
    else:
        data["mortgage_orig_amount"] = None
        data["mortgage_term_years"] = None
        data["mortgage_rate"] = None
        data["mortgage_type"] = ""
        data["mortgage_lender"] = ""
        data["parse_warnings"].append("No active mortgage found on record (property may be paid off, or the loan simply isn't in public filings).")

    # ---- Valuation block: 3 AVMs (First American / Zillow / Remine) ----
    # NOTE: this table can get split across a page break -- the "Est.
    # Value" row lands on one page and the "Range" row(s) land after a
    # repeated header/footer on the next, and that repeated header carries
    # its OWN dollar amounts (the stat line's "$X Est Value \xb7 $Y Net
    # Equity \xb7 HOA $Z"). An earlier version of this parser grabbed every
    # "$..." token in the chunk and assumed a fixed position for each of
    # the 9 values (est/est/est/low/high/low/high/low/high) -- that broke
    # silently (no warning, just visibly wrong numbers) whenever the
    # header's stray dollar amounts landed inside the window, shifting
    # every value after them by one or more slots. Anchoring on the
    # literal "Est. Value" / "Range" keywords instead means only real
    # table cells ever get captured, regardless of what unrelated dollar
    # figures happen to sit between them.
    val_start = full_text.find("Valuation\nFirst American")
    val_end = full_text.find("Property History", val_start) if val_start != -1 else -1
    if val_start != -1:
        val_chunk = full_text[val_start: val_end if val_end != -1 else val_start + 600]
        ests = [_to_int_money(a) for a in re.findall(r"Est\.\s*Value\s+\$([\d,]+(?:\.\d+)?)", val_chunk)]
        ranges = [
            (_to_int_money(lo), _to_int_money(hi))
            for lo, hi in re.findall(r"Range\s+\$([\d,]+(?:\.\d+)?)\s*-\s*\$([\d,]+(?:\.\d+)?)", val_chunk)
        ]
        keys = ["first_american", "zillow", "remine"]
        data["valuations"] = {}
        if len(ests) >= 3:
            for i, key in enumerate(keys):
                entry = {"est": ests[i], "low": None, "high": None}
                if len(ranges) >= 3:
                    entry["low"], entry["high"] = ranges[i]
                data["valuations"][key] = entry
            if len(ranges) < 3:
                data["parse_warnings"].append("Found all 3 AVM estimates but not all 3 low/high ranges -- ranges left blank, please check the source report.")
        else:
            data["parse_warnings"].append("Could not fully parse the 3-AVM valuation table.")
    else:
        data["valuations"] = {}

    # ---- Blended value: average of whichever AVM estimates were found.
    # Remine's own headline "$X Est Value" on the stat line (captured above
    # as value_est) is NOT an average -- it's just whatever single source
    # Remine chose to feature (in practice this has been seen to exactly
    # mirror the First American figure), which could read as one AVM
    # dressed up as "the" estimate. Brian's call: default the report to
    # the average of all three sources instead, since that's less swayed
    # by any one model's quirks. value_est (Remine's own headline number)
    # is kept separately for reference/comparison, not used as the default.
    ests = [v["est"] for v in data["valuations"].values() if v.get("est") is not None]
    data["value_est_avg"] = round(sum(ests) / len(ests)) if ests else None

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

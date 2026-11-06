"""
Extracts property/equity data from a Remine public-records report (the
4-page "Courtesy of [Agent]" branded export pulled through MLS/MRED).

This module supports two report formats, each its own function: Remine
public-records reports (parse_remine_pdf, below) and CoreLogic/Compass
"Property Details" reports pulled through Home Platform
(parse_corelogic_pdf, added Nov 2026 -- see its own docstring for why
it's a meaningfully rougher source for the mortgage-balance figure
specifically). Remine was originally the ONLY source supported, for one
reason: it's the only one of the two that gives us an actual loan
balance / net equity / percent equity for a property with zero manual
entry required, which is what makes the PEAR report's Equity Breakdown
section possible without asking the agent to know the client's mortgage
by heart. (RPR reports -- a third common source agents pull -- never
carry loan data at all, so a parser built around RPR would still need a
manual mortgage-balance field for every single report; RPR support was
scoped out for that reason and still is. See the Cowork conversation
this was built in for the comparison.)

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
from datetime import date

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
    # Entities (trusts, LLCs, etc.) aren't "LAST FIRST" -- keep the name as is.
    raw = re.sub(r"\s*\S?\s*absentee\s*$", "", raw, flags=re.I).strip()
    if re.search(r"\b(TRUST|TRUSTEE|LLC|L\.L\.C|INC|CORP|LP|LTD|ESTATE|PARTNERS|HOLDINGS|BANK|ASSOCIATION)\b", raw, re.I):
        return raw.title()
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

    # Baths/sqft are optional on the stat line: some records (e.g. a newer
    # condo with no public-record square footage and no bath count) simply
    # omit them -- "3 Beds · 0.906 Acres | $870,000 Est Value ...". Only the
    # value and equity figures are truly required. Baths fall back to the
    # Building Features block ("Full Baths N" / "Half Baths N") when the
    # summary line doesn't carry them.
    if baths_s is None:
        fb = re.search(r"Full Baths\s+(\d+)", full_text)
        hb = re.search(r"Half Baths\s+(\d+)", full_text)
        # A "0 full / 0 half" count is missing data, not a real zero-bath home.
        if fb and (int(fb.group(1)) > 0 or (hb and int(hb.group(1)) > 0)):
            baths_s = fb.group(1)
    if value_s and equity_s:
        data["beds"] = _to_int_money(beds_s) if beds_s else None
        data["baths"] = _to_int_money(baths_s) if baths_s else None
        data["sqft"] = _to_int_money(sqft_s) if sqft_s else None
        if not sqft_s:
            data["parse_warnings"].append("No square footage on record for this property -- fill in manually if you want it shown.")
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
        # "Owned for less than a year" (a recent purchase) has no number.
        short_m = re.match(r"Owned for\s+less than (?:a|1)\s+year\s*\S?\s*(.+)$", owner_line, re.I)
        if dur_m:
            data["owned_years"] = _to_number(dur_m.group(1))
            data["owner_names_raw"] = _clean_ws(dur_m.group(2))
        elif short_m:
            data["owned_years"] = 0
            data["owner_names_raw"] = _clean_ws(short_m.group(1))
        else:
            data["owned_years"] = None
            data["owner_names_raw"] = _clean_ws(owner_line)
        data["owner_names_display"] = _format_owner_names(data["owner_names_raw"])
    else:
        data["owned_years"] = None
        data["owner_names_raw"] = ""
        data["owner_names_display"] = ""
        data["parse_warnings"].append("Could not find the owner name on record -- please fill it in manually.")

    # ---- Owner mailing address (may differ from the property address) ----
    # Confirmed against two real samples: 749 Holland St, Saugatuck (owners
    # Robert & Maria Siegel, mailing address 1105 Nashville Ave, New
    # Orleans -- a 2-line wrap) and 1447 N Cleveland Ave Apt E, Chicago
    # (mailing address 400L E Randolph St Apt 2508, Chicago -- a 3-line
    # wrap). It lives in the "Public Record Details" section as a
    # left-column "Key Stats" field, always immediately followed by
    # "County" as the next Key Stats field -- so "County" is a reliable
    # right-hand boundary for the whole chunk regardless of how many lines
    # the address itself wraps to.
    #
    # That two-column layout is also the trap: pdfplumber's linear text
    # extraction splices the neighboring "Building Features" column's
    # label+value onto whichever row it lands on. For a short (1-2 line)
    # address that's just trailing noise on the same line as real address
    # text (e.g. "Mailing Address 1105 NASHVILLE AVE Full Baths 2"). But a
    # longer wrap desyncs the two columns further with each extra line, so
    # unrelated fields can land on their OWN line in between real address
    # lines too -- the Cleveland Ave sample actually printed "Year Built
    # 1997" and "Basement Type Unfinished Basement" as stray lines sitting
    # between "APT 2508" and "CHICAGO IL 60601". So this can't just strip
    # trailing noise from one line; it walks every line in the chunk,
    # truncates each one at the first recognized non-address label it
    # finds (dropping the label and everything after it on that line --
    # if the label is the very first thing on the line, the whole line is
    # dropped), and keeps whatever real address text is left, in order.
    _NON_ADDRESS_LABELS = (
        "Full Baths", "Half Baths", "Bedrooms", "Total SqFt", "Tax Living Area",
        "Basement SqFt", "Basement Type", "Year Built", "Stories", "Structure",
        "Construction", "Roof Cover Type", "Garage Spaces", "Garage", "Parking",
    )
    _non_address_label_re = re.compile(
        r"\b(?:" + "|".join(re.escape(lbl) for lbl in _NON_ADDRESS_LABELS) + r")\b",
        re.IGNORECASE,
    )
    mail_chunk_m = re.search(
        r"Mailing Address\s+(.*?)(?=\n\s*County\b)",
        full_text,
        re.IGNORECASE | re.DOTALL,
    )
    mailing_address_raw = ""
    if mail_chunk_m:
        kept_lines = []
        for line in mail_chunk_m.group(1).split("\n"):
            label_m = _non_address_label_re.search(line)
            piece = (line[: label_m.start()] if label_m else line).strip()
            if piece:
                kept_lines.append(piece)
        mailing_address_raw = _clean_ws(" ".join(kept_lines))

    def _norm_addr(s):
        return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

    if mailing_address_raw and _norm_addr(mailing_address_raw) != _norm_addr(data.get("full_address")):
        data["mailing_address"] = mailing_address_raw
        data["parse_warnings"].append(
            "Public county property records show a mailing address on file for this owner "
            "that's different from the property itself (common for rentals or absentee "
            "owners) -- pre-filled below as the mailing address, double-check it before "
            "mailing anything."
        )
    else:
        data["mailing_address"] = ""

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
        if not (data["loan_balance_est"] is None and data["percent_equity"] == 100):
            data["parse_warnings"].append("No active mortgage found on record (property may be paid off, or the loan simply isn't in public filings).")

    # ---- 100%-equity fallback for a missing Loan Balance figure ----
    # Remine's "Net Equity" panel is the CURRENT-STATE signal (Value / Loan
    # Balance / Net Equity / Percent Equity) -- when it shows 100% equity,
    # it simply OMITS the "Loan Balance" line entirely instead of printing
    # "$0", which used to leave loan_balance_est as None ("unknown") and
    # blank out the entire client-facing Equity Breakdown section
    # ("Estimated Mortgage Balance -- --") despite Remine's own math saying
    # the balance really is zero.
    #
    # This has to run AFTER (not just inside the "no Active Mortgage
    # record" branch above), because a property can have BOTH: a mortgage
    # document still on file from a past purchase (e.g. Orig. Amount
    # $405,000 from a 2022 purchase) AND 100% current equity, if that loan
    # has since been paid off/refinanced -- county filings don't
    # automatically disappear on payoff. That's a real case (6074 Mallard
    # Dr, Fennville), not a hypothetical: Percent Equity 100% with a still-
    # printed Active Mortgage block. Only checking this in the "no mortgage
    # found at all" branch missed exactly that case and left the report
    # showing blank equity figures for a property Remine itself considers
    # fully paid off.
    if data["loan_balance_est"] is None and data["percent_equity"] == 100:
        data["loan_balance_est"] = 0
        if mort_start != -1:
            data["parse_warnings"].append(
                "This property still shows a mortgage on file (from a past purchase), but "
                "Remine's own Percent Equity figure confirms it's fully paid off today -- the "
                "mortgage balance used for the equity math defaults to $0 rather than the "
                "original loan amount shown above."
            )
        else:
            data["parse_warnings"].append(
                "No active mortgage found on record -- Remine's own Percent Equity figure "
                "confirms this property is paid off, so the mortgage balance defaults to $0."
            )

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


def _amortized_balance(principal, annual_rate_pct, term_years, start_date, as_of_date):
    """Standard fixed-rate loan amortization. Returns the estimated
    remaining principal balance as of `as_of_date`, or None if any input
    is missing. This assumes the ORIGINATION rate held constant for the
    whole loan term -- exactly right for a fixed-rate loan, a real
    approximation for an adjustable-rate loan (this has no way to see
    the actual rate-reset schedule, only the starting rate), and in
    both cases blind to extra principal payments, a later refinance, or
    a HELOC that isn't reflected in this report -- the same category of
    caveat that's already on every PEAR report's Important Notes
    section for Remine-sourced reports. Verified by hand against a few
    worked examples (see the Cowork conversation this was built in)."""
    if principal is None or annual_rate_pct is None or not term_years or start_date is None or as_of_date is None:
        return None
    months_elapsed = (as_of_date.year - start_date.year) * 12 + (as_of_date.month - start_date.month)
    n = int(round(term_years * 12))
    if n <= 0:
        return None
    if months_elapsed <= 0:
        return int(round(principal))
    if months_elapsed >= n:
        return 0
    r = (annual_rate_pct / 100) / 12
    if r == 0:
        return int(round(principal * (1 - months_elapsed / n)))
    factor_n = (1 + r) ** n
    factor_k = (1 + r) ** months_elapsed
    return int(round(principal * (factor_n - factor_k) / (factor_n - 1)))


def _parse_date_mmddyyyy(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_mortgage_history_latest(full_text):
    """FALLBACK ONLY -- used when pdfplumber word-position data isn't
    available (see _parse_mortgage_history_grid below for the real,
    position-aware extractor parse_corelogic_pdf actually uses). This
    version reads plain extracted text, which loses each value's column
    position: when a loan's cell for a field is blank -- common here,
    e.g. older adjustable-rate loans with no interest rate on record --
    pdfplumber's text output just omits it rather than leaving a gap, so
    counting tokens left-to-right silently shifts every later loan's
    value into the wrong column. Confirmed against a real sample (5331 N
    Magnolia Ave, Chicago): the "Mortgage Int Rate" row had 3 values for
    5 loan columns, and the gaps were NOT only at the end, so even the
    first value on the line isn't safe to assume matches the first
    (most recent) column -- it can belong to a later one. This function
    only trusts a line at all when its value count matches the date
    count across the whole row, which is overly conservative (it would
    have blanked out a real, correct rate for the most recent loan in
    that sample) but never wrong. Lender/title-company fields are
    skipped entirely either way (free-text company names wrap across
    lines with no reliable per-column boundary, and nothing downstream
    displays them anyway -- see parser.py's CoreLogic docstring)."""
    mh_start = full_text.find("MORTGAGE HISTORY")
    if mh_start == -1:
        return None

    mh_chunk = full_text[mh_start:mh_start + 1500]

    def _line_tail(label):
        m = re.search(re.escape(label) + r"\s+([^\n]*)", mh_chunk)
        return m.group(1) if m else ""

    dates = re.findall(r"\d{2}/\d{2}/\d{4}", _line_tail("Mortgage Date"))
    if not dates:
        return None

    amounts = re.findall(r"\$([\d,]+)", _line_tail("Mortgage Amount"))
    terms = re.findall(r"\d+", _line_tail("Mortgage Term"))
    rates = re.findall(r"\d+\.?\d*", _line_tail("Mortgage Int Rate"))

    n = len(dates)
    result = {
        "date": _parse_date_mmddyyyy(dates[0]),
        "date_raw": dates[0],
        "amount": _to_int_money(amounts[0]) if len(amounts) == n and amounts else None,
        "term_years": _to_number(terms[0]) if len(terms) == n and terms else None,
        "rate": _to_number(rates[0]) if len(rates) == n and rates else None,
    }
    result["rate_reliable"] = len(rates) == n
    return result


def _words_by_line(words, tolerance=2):
    """Groups one pdfplumber page's extract_words() output into printed
    text lines (words sharing a baseline), each a list of word dicts
    sorted left-to-right by x0. `tolerance` merges tops within a couple
    points of each other into the same line, since words on the same
    printed row aren't always reported at the exact same `top`."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for line in lines:
            if abs(line[0]["top"] - w["top"]) <= tolerance:
                line.append(w)
                break
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    lines.sort(key=lambda line: line[0]["top"])
    return lines


def _match_label_row(line_words, label_tokens, exclude_next=None):
    """line_words: one line's word dicts, left-to-right. label_tokens:
    the exact leading words that identify this row (e.g. ["Mortgage",
    "Term"]). exclude_next, if given, rejects the line when the word
    right after the label matches it -- used to tell e.g. "Mortgage
    Term" apart from "Mortgage Term Code", or "Mortgage Int Rate" apart
    from "Mortgage Int Rate Type". Returns the remaining value words, or
    None if this line doesn't match."""
    if len(line_words) <= len(label_tokens):
        return None
    for tok, w in zip(label_tokens, line_words):
        if w["text"] != tok:
            return None
    rest = line_words[len(label_tokens):]
    if exclude_next and rest and rest[0]["text"] == exclude_next:
        return None
    return rest


def _assign_to_columns(anchors, value_words, tolerance=25):
    """Places each value word into whichever column anchor (an x0
    position, taken from that loan block's own "Mortgage Date" row) it's
    closest to, within `tolerance` points. This is the fix for the
    problem _parse_mortgage_history_latest can't solve from plain text:
    a blank cell in the middle of a row doesn't shift later values left
    here, because each value is placed by where it actually sits on the
    page, not by its position in a left-to-right count."""
    result = [None] * len(anchors)
    for w in value_words:
        best_i, best_d = None, None
        for i, a in enumerate(anchors):
            d = abs(w["x0"] - a)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        if best_d is not None and best_d <= tolerance:
            result[best_i] = w["text"]
    return result


def _parse_mortgage_history_grid(pages_words):
    """The real, position-aware mortgage-history extractor. CoreLogic's
    Mortgage History section is a grid -- one column per recorded
    mortgage, each with its own "Mortgage Date" entry used here as that
    loan's column anchor -- but a report can also print a single
    mortgage as its own one-column block further down (confirmed in the
    real sample this was built against: a grid of 5 loans, then one more
    standalone mortgage block below it). This walks every page's words
    line by line from "MORTGAGE HISTORY" to "PROPERTY MAP", starting a
    new block at each "Mortgage Date" row (using its value positions as
    that block's column anchors) and assigning each subsequent Amount /
    Int Rate / Term row's values to those anchors by x-position via
    _assign_to_columns.

    Returns a list of loan dicts ({date, date_raw, amount, rate,
    term_years}), one per recorded mortgage found across every block, in
    no particular order -- parse_corelogic_pdf picks the most recent of
    these itself, by parsed date, rather than assuming column order.
    Returns an empty list if no Mortgage History section is found."""
    stream = []
    started = False
    for words in pages_words:
        if not words:
            continue
        stopped = False
        for line in _words_by_line(words):
            text = " ".join(w["text"] for w in line)
            if not started:
                if text == "MORTGAGE HISTORY":
                    started = True
                continue
            if text.startswith("PROPERTY MAP"):
                stopped = True
                break
            stream.append(line)
        if stopped:
            break

    blocks = []
    current = None
    for line in stream:
        date_vals = _match_label_row(line, ["Mortgage", "Date"])
        if date_vals is not None:
            current = {
                "anchors": [w["x0"] for w in date_vals],
                "dates": [w["text"] for w in date_vals],
                "amounts": None,
                "rates": None,
                "terms": None,
            }
            current["amounts"] = [None] * len(date_vals)
            current["rates"] = [None] * len(date_vals)
            current["terms"] = [None] * len(date_vals)
            blocks.append(current)
            continue
        if current is None:
            continue
        amt_vals = _match_label_row(line, ["Mortgage", "Amount"])
        if amt_vals is not None:
            current["amounts"] = _assign_to_columns(current["anchors"], amt_vals)
            continue
        rate_vals = _match_label_row(line, ["Mortgage", "Int", "Rate"], exclude_next="Type")
        if rate_vals is not None:
            current["rates"] = _assign_to_columns(current["anchors"], rate_vals)
            continue
        term_vals = _match_label_row(line, ["Mortgage", "Term"], exclude_next="Code")
        if term_vals is not None:
            current["terms"] = _assign_to_columns(current["anchors"], term_vals)
            continue

    loans = []
    for b in blocks:
        for i, date_raw in enumerate(b["dates"]):
            loans.append({
                "date": _parse_date_mmddyyyy(date_raw),
                "date_raw": date_raw,
                "amount": _to_int_money(b["amounts"][i]),
                "rate": _to_number(b["rates"][i]),
                "term_years": _to_number(b["terms"][i]),
            })
    return loans


def parse_corelogic_pdf(pages_text, full_text=None, pages_words=None):
    """Extracts property/equity data from a CoreLogic "Property Details"
    report (the format pulled through Home Platform / Compass, courtesy-
    of-branded, 3-page export). A second, parallel source to
    parse_remine_pdf -- see that function's docstring, and the module
    docstring above, for why Remine was the only source originally
    supported. This format doesn't have Remine's computed current loan
    balance: it only lists every mortgage ever recorded against the
    property (amount/rate/term AT ORIGINATION, not a current balance),
    so loan_balance_est here is either a best-effort amortization
    estimate off the most recent recorded loan (see
    _amortized_balance / _parse_mortgage_history_latest above), or None
    when the data to do that reliably isn't there -- in which case the
    agent fills it in manually on the review screen, same as any Remine
    report with no mortgage on file. Unlike Remine, this format also has
    no independent "Percent Equity" figure, so a missing/unreliable loan
    balance here can NEVER be defaulted to $0 the way Remine's 100%-
    equity fallback does -- there's nothing to confirm a payoff against.

    Returns the SAME dict shape as parse_remine_pdf (every key present,
    missing fields None/"") so app.py and the template don't need to
    know which source produced a given report.

    Built and tested against one real sample (5331 N Magnolia Ave,
    Chicago -- see the Cowork conversation this was built in). Like
    parser.py's Remine support, expect this to need adjustment against
    the next few real CoreLogic pulls that break the template in some
    new spot -- it's the same lesson learned there, just not re-learned
    yet here.
    """
    if full_text is None:
        full_text = "\n".join(pages_text)

    data = {
        "source_format": "corelogic",
        "parse_warnings": [],
    }

    # ---- Header: address (strip the trailing ", <County> County") ----
    first_line = pages_text[0].split("\n")[0] if pages_text and pages_text[0] else ""
    addr_m = re.match(r"^(.*?),\s*[A-Za-z .]+\s+County\s*$", first_line)
    data["full_address"] = _clean_ws(addr_m.group(1)) if addr_m else _clean_ws(first_line)
    data["status"] = ""  # this report format has no Remine-style status badge; unused downstream either way
    if not addr_m:
        data["parse_warnings"].append("Could not cleanly split the property address from the county name on the header line -- please double-check it.")

    # ---- Characteristics block (beds/baths/sqft/year/lot/type) ----
    char_start = full_text.find("CHARACTERISTICS")
    char_chunk = full_text[char_start:char_start + 1200] if char_start != -1 else ""

    data["beds"] = _to_int_money(_first(r"Bedrooms\s+(\d+)", char_chunk))
    data["baths"] = _to_int_money(_first(r"Total Baths\s+(\d+)", char_chunk))
    data["sqft"] = _to_int_money(_first(r"Building Sq Ft\s+([\d,]+)", char_chunk))
    data["year_built"] = _to_int_money(_first(r"Year Built\s+(\d{4})", char_chunk))
    data["lot_acres"] = _to_number(_first(r"Lot Acres\s+([\d.]+)", char_chunk))
    data["property_type"] = _clean_ws(_first(r"Land Use\s+(\S+)", char_chunk) or "")
    if not char_chunk:
        data["parse_warnings"].append("Could not find the Characteristics section (beds/baths/sqft/year built) -- please fill these in manually.")

    # ---- Owner name / mailing address (OWNER INFORMATION block) ----
    owner_m = re.search(r"Owner Name\s+(.+?)\s+Owner Occupied\b", full_text)
    data["owner_names_raw"] = _clean_ws(owner_m.group(1)) if owner_m else ""
    # CoreLogic prints owner names in normal "First Last" / entity-name
    # order already (unlike Remine's public-record "LAST FIRST" order),
    # so -- unlike the Remine path above -- this is NOT run through
    # _format_owner_names()'s last/first swap, which would wrongly
    # reverse an already-correct name. Only confirmed against one sample
    # (a trust name, where order doesn't matter either way); the agent
    # reviews/overwrites client_name before anything is generated
    # regardless.
    data["owner_names_display"] = data["owner_names_raw"]
    data["owned_years"] = None
    if not owner_m:
        data["parse_warnings"].append("Could not find the owner name on record -- please fill it in manually.")

    mail_m = re.search(r"Mailing Address\s+(.+?)\s+Ownership Right Vesting\b", full_text)
    mailing_address_raw = _clean_ws(mail_m.group(1)) if mail_m else ""

    def _norm_addr(s):
        return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

    if mailing_address_raw and _norm_addr(mailing_address_raw) != _norm_addr(data.get("full_address")):
        data["mailing_address"] = mailing_address_raw
        data["parse_warnings"].append(
            "Public county property records show a mailing address on file for this owner "
            "that's different from the property itself (common for rentals or absentee "
            "owners) -- pre-filled below as the mailing address, double-check it before "
            "mailing anything."
        )
    else:
        data["mailing_address"] = ""

    # ---- RealAVM valuation (this report's one AVM source, vs Remine's three) ----
    est_m = re.search(r"RealAVM\S*\s+\$([\d,]+)", full_text)
    range_m = re.search(r"RealAVM\S*\s*Range\s+\$([\d,]+)\s*-\s*\$([\d,]+)", full_text)
    data["valuations"] = {}
    if est_m:
        entry = {"est": _to_int_money(est_m.group(1)), "low": None, "high": None}
        if range_m:
            entry["low"] = _to_int_money(range_m.group(1))
            entry["high"] = _to_int_money(range_m.group(2))
        data["valuations"]["corelogic_realavm"] = entry
        data["value_est"] = entry["est"]
    else:
        data["value_est"] = None
        data["parse_warnings"].append("Could not find the RealAVM estimated value -- please fill in the market value manually.")

    ests = [v["est"] for v in data["valuations"].values() if v.get("est") is not None]
    data["value_est_avg"] = round(sum(ests) / len(ests)) if ests else None

    # ---- Last sale: "LAST MARKET SALE & SALES HISTORY" section's clean
    # Settle Date / Sale Price pair -- the top stat block has the same two
    # facts under "Sale Date"/"Sale Price" labels, but as a labels-row-
    # then-values-row layout (like Beds/Baths up there), not a clean
    # adjacent pair, so pulling from it directly misses. ----
    sale_start = full_text.find("LAST MARKET SALE")
    sale_chunk = full_text[sale_start:sale_start + 800] if sale_start != -1 else ""
    data["last_sale_price"] = _to_int_money(_first(r"Sale Price\s+\$([\d,]+)", sale_chunk))
    data["last_sale_date"] = _first(r"Settle Date\s+(\d{2}/\d{2}/\d{4})", sale_chunk)
    if not sale_chunk:
        data["parse_warnings"].append("Could not find the Last Market Sale section -- last purchase price/date left blank, please fill in manually if relevant.")

    # ---- Mortgage history -> estimated current balance ----
    report_date_m = re.search(r"Generated on:\s*(\d{2})/(\d{2})/(\d{2})", full_text)
    report_date = None
    if report_date_m:
        mm, dd, yy = (int(x) for x in report_date_m.groups())
        report_date = date(2000 + yy if yy < 50 else 1900 + yy, mm, dd)

    # Prefer the position-aware grid parser (accurate even when a loan's
    # cell for a field is blank) whenever pdfplumber word-position data
    # was handed in; fall back to the plain-text version otherwise -- see
    # both functions' docstrings for why they can disagree.
    latest = None
    if pages_words:
        loans = _parse_mortgage_history_grid(pages_words)
        dated_loans = [l for l in loans if l["date"] is not None]
        if dated_loans:
            best = max(dated_loans, key=lambda l: l["date"])
            latest = {
                "date": best["date"],
                "date_raw": best["date_raw"],
                "amount": best["amount"],
                "term_years": best["term_years"],
                "rate": best["rate"],
                "rate_reliable": best["rate"] is not None,
            }
    if latest is None:
        latest = _parse_mortgage_history_latest(full_text)
    data["mortgage_orig_amount"] = latest["amount"] if latest else None
    data["mortgage_term_years"] = latest["term_years"] if latest else None
    data["mortgage_rate"] = latest["rate"] if (latest and latest["rate_reliable"]) else None
    # Lender/type aren't attempted for this format -- see
    # _parse_mortgage_history_latest's docstring -- and nothing
    # downstream displays them for either format anyway.
    data["mortgage_type"] = ""
    data["mortgage_lender"] = ""

    if latest and latest["rate_reliable"] and latest["amount"] and latest["term_years"] and latest["date"] and report_date:
        data["loan_balance_est"] = _amortized_balance(
            latest["amount"], latest["rate"], latest["term_years"], latest["date"], report_date
        )
        data["parse_warnings"].append(
            "This report doesn't provide a current loan balance the way Remine does -- the "
            f"mortgage balance shown is an amortization estimate off the most recent recorded "
            f"loan (${latest['amount']:,} at {latest['rate']}% from {latest['date_raw']}), "
            "assuming that rate held constant and no extra principal payments, refinance, or "
            "HELOC since. Treat it as rougher than the usual estimate and double-check before "
            "sending."
        )
    elif latest:
        data["loan_balance_est"] = None
        if latest.get("amount") and latest.get("date_raw"):
            which = f"the most recent mortgage on record (${latest['amount']:,} from {latest['date_raw']})"
        else:
            which = "a mortgage history on record"
        data["parse_warnings"].append(
            f"Found {which}, but this report doesn't list its interest rate -- mortgage "
            "balance must be filled in manually."
        )
    else:
        data["loan_balance_est"] = None
        data["parse_warnings"].append(
            "No mortgage history found on record for this property -- fill in the mortgage "
            "balance manually, or confirm with the owner whether it's paid off. (Unlike "
            "Remine, this report has no separate equity/percent-equity figure to confirm a "
            "payoff against, so this can't default to $0 the way a Remine report with no "
            "mortgage on file does.)"
        )

    data["percent_equity"] = None
    data["net_equity_est"] = None
    if data["value_est"] is not None and data["loan_balance_est"] is not None:
        data["net_equity_est"] = data["value_est"] - data["loan_balance_est"]
        data["percent_equity"] = round(data["net_equity_est"] / data["value_est"] * 100) if data["value_est"] else None

    data["contact_name"] = ""
    data["contact_phone"] = ""
    data["contact_email"] = ""

    return data


def detect_and_parse(file_bytes, filename=""):
    """Entry point app.py calls. Sniffs the report format from its text
    content and routes to the matching parser -- Remine (parse_remine_pdf)
    or CoreLogic/Compass "Property Details" (parse_corelogic_pdf), added
    Nov 2026 once Home Platform made that format easier for Brian to pull
    than Remine. "RealAVM" is CoreLogic's own trademarked valuation
    product name and doesn't appear anywhere in a Remine report, so its
    presence is a reliable signal either way. If neither format is
    recognized, this still attempts the Remine parse (harmless -- it'll
    just come back mostly empty with warnings) so the agent lands on a
    normal, editable review screen instead of a hard error, and can fill
    in every field by hand."""
    try:
        with pdfplumber.open(__import__("io").BytesIO(file_bytes)) as pdf:
            pages_text = [p.extract_text() or "" for p in pdf.pages]
            full_text = "\n".join(pages_text)
            # Word-level position data (for the Mortgage History grid --
            # see _parse_mortgage_history_grid) has to be pulled while the
            # pdfplumber file is still open, so this only happens here,
            # once we already know it's worth the extra extraction pass.
            pages_words = [p.extract_words() for p in pdf.pages] if "RealAVM" in full_text else None
    except Exception:
        pages_text, full_text, pages_words = [], "", None

    if "RealAVM" in full_text:
        data = parse_corelogic_pdf(pages_text, full_text, pages_words=pages_words)
    else:
        data = parse_remine_pdf(file_bytes)

    if not data.get("full_address") and not data.get("value_est"):
        data["parse_warnings"].append(
            "This doesn't look like a Remine public-records report or a CoreLogic Property "
            "Details report -- little or nothing could be extracted. You can still fill in "
            "every field manually below."
        )
    return data

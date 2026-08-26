"""
Renders the reviewed/edited PEAR field set into the branded, printable
1-page PDF. Mirrors the WeasyPrint pattern used by jlg-listing-flyer and
jlg-showing-packet (Jinja2 template + local @font-face files, no network
fonts needed inside the Render container).
"""
import datetime
import os

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
FONT_DIR = os.path.join(STATIC_DIR, "fonts")

JLG_BLOCK = os.path.join(STATIC_DIR, "logo", "JLG-COMBO-BLUE.png")
BROKERAGE_LOCKUP = os.path.join(STATIC_DIR, "logo", "at-properties-christies-color.png")
BROKERAGE_LOCKUP_BW = os.path.join(STATIC_DIR, "logo", "at-properties-christies-blackonly.png")
# Equal Housing Opportunity + REALTOR bugs, side by side -- small and
# subtle in the footer, but present on every report. These are being
# mailed to people, which puts them squarely in "real estate advertising
# material" territory under the Fair Housing Act, so the EHO mark needs to
# be there regardless of how polished the rest of the design is.
FAIR_HOUSING_BUGS = os.path.join(STATIC_DIR, "logo", "fair-housing-realtor-bugs.png")


def money(n):
    """None-safe currency formatter -- a field the parser couldn't find
    and the agent hasn't filled in yet should render as an em dash, never
    as '$0' (which reads as a real, and wrong, data point on a client-
    facing financial document)."""
    if n is None or n == "":
        return "—"
    try:
        return f"${float(n):,.0f}"
    except (TypeError, ValueError):
        return "—"


def pct(n):
    if n is None or n == "":
        return "—"
    try:
        return f"{float(n):.0f}%"
    except (TypeError, ValueError):
        return "—"


def render_pear(fields, computed, output_path, agent_name="Brian Elmore", agent_phone="", agent_email="brian@justinlucasgroup.com", print_safe_logo=False):
    """
    fields: dict of the reviewed/edited raw inputs (client name, address,
        value, loan balance, last purchase price/date, target price, etc.)
    computed: dict returned by calc.compute_pear() for that same field set
    """
    env = Environment(loader=FileSystemLoader(os.path.join(BASE_DIR, "templates")))
    env.filters["money"] = money
    env.filters["pct"] = pct
    template = env.get_template("pear.html")

    agent_name = agent_name or "Brian Elmore"
    html_str = template.render(
        f=fields,
        c=computed,
        font_dir=FONT_DIR,
        logo_jlg=JLG_BLOCK,
        logo_brokerage=BROKERAGE_LOCKUP_BW if print_safe_logo else BROKERAGE_LOCKUP,
        logo_fair_housing=FAIR_HOUSING_BUGS,
        agent_name=agent_name,
        agent_phone=agent_phone,
        agent_email=agent_email or "brian@justinlucasgroup.com",
        prepared_date=datetime.date.today().strftime("%B %-d, %Y"),
        current_year=datetime.date.today().year,
    )
    HTML(string=html_str, base_url=BASE_DIR).write_pdf(output_path)
    return output_path

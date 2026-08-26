"""
JLG PEAR Report Generator
-------------------------
Upload a Remine public-records report (the "Courtesy of [Agent]" branded
4-page export pulled through MLS), auto-populate a Professional Equity
Assessment Report, review/edit every field, then generate a branded,
1-page, printable PDF for the client.

Run with:  python3 app.py
Then open: http://localhost:5000
"""
import json
import os
import re
import traceback
import uuid

from flask import Flask, request, jsonify, send_file, render_template_string

from parser import detect_and_parse
from calc import compute_pear, suggested_target_price
from render import render_pear

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB upload cap


def load_config():
    default = {
        "agent_name": "Brian Elmore",
        "agent_phone": "",
        "agent_email": "brian@justinlucasgroup.com",
        "print_safe_logo": False,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                default.update(json.load(f))
        except Exception:
            pass
    return default


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _num(v):
    """Form fields arrive as strings, possibly with $ and commas (an agent
    hand-editing a value in the review form) -- same sanitization bug
    class fixed elsewhere in Brian's tools (see social-post-generator.html
    money() hardening), applied here too so a typed '$1,243,000' doesn't
    silently become 0."""
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JLG PEAR Report Generator</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --blue: #032b42; --blue-dk: #021e30; --blue-md: #04395a;
    --slate: #f2f2f2; --red: #780000; --red-hv: #8f0000; --white: #ffffff;
    --text: #1a1a1a; --muted: #6b6b6b; --border: rgba(0,0,0,.08); --border-b: rgba(3,43,66,.12);
    --r: 4px; --rl: 8px; --d: .28s; --ease: cubic-bezier(.4,0,.2,1);
    --sh: 0 4px 20px rgba(0,0,0,.08); --sh-l: 0 12px 40px rgba(0,0,0,.14);
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: 'Plus Jakarta Sans', sans-serif; background: var(--slate); color: var(--text); }
  h1, h2 { font-family: 'DM Serif Display', serif; font-weight: 400; margin: 0; }
  header.top { background: var(--blue); padding: 22px 0; }
  .top-in { max-width: 760px; margin: 0 auto; padding: 0 24px; display: flex; align-items: center; gap: 18px; }
  .top-in img { height: 44px; width: auto; display: block; }
  .top-title { color: rgba(255,255,255,.55); font-size: .82rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; border-left: 1px solid rgba(255,255,255,.25); padding-left: 18px; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 40px 24px 100px; }
  .hero { margin-bottom: 32px; }
  .hero h1 { font-size: 1.7rem; color: var(--blue); }
  .hero p { color: var(--muted); margin-top: 8px; font-size: .95rem; max-width: 560px; }
  .card { background: #fff; border-radius: var(--rl); box-shadow: var(--sh); padding: 28px; margin-bottom: 24px; }
  .card.hidden { display: none; }
  .step-num { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 50%; background: var(--red); color: #fff; font-size: .78rem; font-weight: 700; margin-right: 10px; flex-shrink: 0; }
  .card h2 { font-size: 1.15rem; color: var(--blue); font-family: 'Plus Jakarta Sans'; font-weight: 700; display: flex; align-items: center; margin-bottom: 16px; }
  #dropzone { border: 2px dashed var(--border-b); border-radius: var(--rl); padding: 32px 20px; text-align:center; color: var(--blue); cursor:pointer; transition: border-color var(--d) var(--ease), background var(--d) var(--ease); }
  #dropzone:hover, #dropzone.drag { border-color: var(--blue); background: var(--slate); }
  #dropzone p { margin: 6px 0; font-size: .92rem; }
  #dropzone .hint { font-size:.8rem; color:var(--muted); }
  input[type=file] { display:none; }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .field { flex: 1; min-width: 200px; }
  .field label { font-size:.78rem; font-weight: 700; color: var(--blue); text-transform: uppercase; letter-spacing: .03em; display:block; margin-bottom:6px; }
  .field .helper { font-size: .74rem; color: var(--muted); margin-top: 4px; }
  input[type=text], input[type=number] { padding:11px 13px; border:1.5px solid var(--border-b); border-radius: var(--r); font-size:.92rem; font-family: inherit; width:100%; }
  input[type=text]:focus, input[type=number]:focus { outline: none; border-color: var(--blue); }
  input[type=checkbox] { accent-color: var(--red); }
  button.primary { display: inline-flex; align-items: center; gap: 10px; background: var(--red); color: #fff; border: none; padding: 13px 24px; border-radius: var(--r); font-family: inherit; font-size:.88rem; font-weight: 700; letter-spacing: .01em; cursor:pointer; margin-top:14px; transition: background var(--d) var(--ease); }
  button.primary:hover { background: var(--red-hv); }
  button.primary:disabled { background:#c9c9c9; cursor:not-allowed; }
  button.secondary { display:inline-flex; align-items:center; gap:8px; background:#fff; color:var(--blue); border:1.5px solid var(--border-b); padding:11px 20px; border-radius:var(--r); font-family:inherit; font-size:.85rem; font-weight:700; cursor:pointer; margin-top:14px; }
  #status { font-size:.85rem; color:var(--muted); margin-top:10px; }
  .warn-box { background: #fff7ed; border: 1px solid #f0c98a; border-radius: var(--r); padding: 12px 14px; font-size: .82rem; color: #7a5200; margin-bottom: 16px; }
  .warn-box ul { margin: 6px 0 0; padding-left: 18px; }
  .section-divider { font-size: .78rem; font-weight: 700; color: var(--blue); text-transform: uppercase; letter-spacing: .03em; margin: 18px 0 10px; padding-top: 14px; border-top: 1px solid var(--border); }
  .section-divider:first-child { margin-top: 0; padding-top: 0; border-top: none; }
  .build-credit { text-align: center; margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--border); font-size: .74rem; color: var(--muted); }
  @media (max-width: 640px) {
    .top-in { padding: 0 16px; gap: 12px; } .top-in img { height: 36px; } .top-title { font-size: .7rem; padding-left: 12px; }
    .wrap { padding: 24px 16px 64px; } .hero h1 { font-size: 1.4rem; } .card { padding: 18px; border-radius: var(--r); }
    .row { flex-direction: column; gap: 14px; } button.primary { width: 100%; justify-content: center; }
  }
</style>
</head>
<body>

<header class="top">
  <div class="top-in">
    <img src="/static/logo/JLG-COMBO-BLUE.png" alt="Justin Lucas Group">
    <span class="top-title">Internal Tool</span>
  </div>
</header>

<div class="wrap">
  <div class="hero">
    <h1>PEAR Report Generator</h1>
    <p>Upload a Remine public-records report for a client's property, review and customize every figure, then generate a branded 1-page Professional Equity Assessment Report.</p>
  </div>

  <div class="card">
    <h2><span class="step-num">1</span>Agent &amp; client details</h2>
    <div class="row">
      <div class="field">
        <label>Agent name</label>
        <input type="text" id="agentName" value="{{ cfg.agent_name }}" placeholder="Brian Elmore">
      </div>
      <div class="field">
        <label>Agent phone</label>
        <input type="text" id="agentPhone" value="{{ cfg.agent_phone }}" placeholder="312.555.0100">
      </div>
      <div class="field">
        <label>Agent email</label>
        <input type="text" id="agentEmail" value="{{ cfg.agent_email }}">
      </div>
    </div>
    <div class="field helper" style="margin-top:6px;">Generating for someone else on the team? Just change the name/phone/email above &mdash; e.g. Justin, Eric, or Camille.</div>
    <label style="display:flex;align-items:center;gap:7px;margin-top:16px;font-size:.82rem;color:var(--text);cursor:pointer;">
      <input type="checkbox" id="printSafeLogo" {{ 'checked' if cfg.print_safe_logo else '' }} style="margin:0;">
      Print-safe logo (black &amp; white)
    </label>
    <div class="section-divider">Client</div>
    <div class="row">
      <div class="field">
        <label>Client name (shown on report)</label>
        <input type="text" id="clientName" placeholder="e.g. Nathan Jenks and Mark Umiel">
      </div>
    </div>
    <div class="field helper">This defaults from the report's public-record owner name once you upload &mdash; edit it to whatever you'd like shown (nickname, "the Jenks Family," etc).</div>
  </div>

  <div class="card">
    <h2><span class="step-num">2</span>Upload Remine report</h2>
    <div id="dropzone">
      <p><strong>Drag &amp; drop the Remine PDF here</strong></p>
      <p class="hint">or click to browse &mdash; one property at a time</p>
      <input type="file" id="fileInput" accept="application/pdf">
    </div>
    <div class="field helper" style="margin-top:10px;">
      This needs the 4-page Remine public-records report (the one branded "Courtesy of [Agent Name]," with Active Mortgage, Net Equity, and a 3-source Valuation table) &mdash; not an RPR report, which doesn't carry loan/equity data.
    </div>
    <div id="status"></div>
  </div>

  <div class="card hidden" id="reviewCard">
    <h2><span class="step-num">3</span>Review &amp; customize</h2>
    <div id="warnBox"></div>
    <div class="section-divider">Property</div>
    <div class="row">
      <div class="field"><label>Full address</label><input type="text" id="f_full_address"></div>
    </div>
    <div class="row">
      <div class="field"><label>Beds</label><input type="text" id="f_beds"></div>
      <div class="field"><label>Baths</label><input type="text" id="f_baths"></div>
      <div class="field"><label>SF</label><input type="text" id="f_sqft"></div>
      <div class="field"><label>Year built</label><input type="text" id="f_year_built"></div>
    </div>
    <div class="row">
      <div class="field"><label>Property type</label><input type="text" id="f_property_type"></div>
      <div class="field"><label>Status badge text</label><input type="text" id="f_status_text" placeholder="Off-Market"></div>
    </div>

    <div class="section-divider">Valuation &amp; Equity</div>
    <div class="row">
      <div class="field"><label>Estimated market value</label><input type="text" id="f_value"></div>
      <div class="field"><label>Estimated mortgage balance</label><input type="text" id="f_loan_balance"></div>
    </div>
    <div class="row">
      <div class="field"><label>Last purchase price</label><input type="text" id="f_last_purchase_price"></div>
      <div class="field"><label>Last purchase year</label><input type="text" id="f_last_purchase_year"></div>
    </div>

    <div class="section-divider">Purchasing power</div>
    <div class="row">
      <div class="field">
        <label>Move-up target home price</label>
        <input type="text" id="f_target_price">
        <div class="helper">Defaults to ~25% above current value &mdash; change this to whatever price range the client is actually considering.</div>
      </div>
    </div>

    <div id="livePreview" style="margin-top:16px;font-size:.85rem;color:var(--muted);"></div>

    <button class="primary" id="generateBtn">Generate PDF</button>
    <button class="secondary" id="reparseBtn">Start over with a different file</button>
  </div>

  <p class="build-credit">&copy; 2026 Brian Elmore. All rights reserved. This tool may not be reproduced or redistributed without permission.</p>
</div>

<script>
const dz = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const statusEl = document.getElementById('status');
const reviewCard = document.getElementById('reviewCard');
const warnBox = document.getElementById('warnBox');
const livePreview = document.getElementById('livePreview');
const generateBtn = document.getElementById('generateBtn');
const reparseBtn = document.getElementById('reparseBtn');

const FIELD_IDS = ['full_address','beds','baths','sqft','year_built','property_type','status_text',
                    'value','loan_balance','last_purchase_price','last_purchase_year','target_price'];

dz.addEventListener('click', () => fileInput.click());
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('drag'); if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]); });
fileInput.addEventListener('change', () => { if (fileInput.files.length) handleFile(fileInput.files[0]); fileInput.value=''; });
reparseBtn.addEventListener('click', () => { reviewCard.classList.add('hidden'); statusEl.textContent=''; });

function handleFile(file) {
  const form = new FormData();
  form.append('file', file);
  statusEl.textContent = 'Reading report...';
  fetch('/parse', { method: 'POST', body: form })
    .then(r => r.json())
    .then(data => {
      if (data.error) { statusEl.textContent = 'Error: ' + data.error; return; }
      statusEl.textContent = 'Parsed ' + (data.full_address || 'report') + '. Review below before generating.';
      populateReview(data);
      reviewCard.classList.remove('hidden');
      reviewCard.scrollIntoView({behavior:'smooth'});
      updatePreview();
    })
    .catch(err => { statusEl.textContent = 'Error: ' + err; });
}

function populateReview(data) {
  document.getElementById('clientName').value = data.owner_names_display || '';
  document.getElementById('f_full_address').value = data.full_address || '';
  document.getElementById('f_beds').value = data.beds ?? '';
  document.getElementById('f_baths').value = data.baths ?? '';
  document.getElementById('f_sqft').value = data.sqft ?? '';
  document.getElementById('f_year_built').value = data.year_built ?? '';
  document.getElementById('f_property_type').value = data.property_type || '';
  document.getElementById('f_status_text').value = data.status || 'Off-Market';
  document.getElementById('f_value').value = data.value_est ?? '';
  document.getElementById('f_loan_balance').value = data.loan_balance_est ?? '';
  document.getElementById('f_last_purchase_price').value = data.last_sale_price ?? '';
  let year = '';
  if (data.last_sale_date) {
    const parts = data.last_sale_date.split('/');
    if (parts.length === 3) { let yy = parseInt(parts[2],10); year = (yy < 50 ? 2000+yy : 1900+yy); }
  }
  document.getElementById('f_last_purchase_year').value = year;
  document.getElementById('f_target_price').value = data.suggested_target_price ?? '';

  warnBox.innerHTML = '';
  if (data.parse_warnings && data.parse_warnings.length) {
    const box = document.createElement('div');
    box.className = 'warn-box';
    box.innerHTML = '<strong>Heads up:</strong><ul>' + data.parse_warnings.map(w => '<li>' + w + '</li>').join('') + '</ul>';
    warnBox.appendChild(box);
  }

  FIELD_IDS.forEach(id => {
    const el = document.getElementById('f_' + id);
    if (el) el.addEventListener('input', updatePreview);
  });
}

function updatePreview() {
  const value = parseFloat((document.getElementById('f_value').value || '').replace(/[^0-9.-]/g,'')) || 0;
  const bal = parseFloat((document.getElementById('f_loan_balance').value || '').replace(/[^0-9.-]/g,'')) || 0;
  const equity = value - bal;
  const pct = value ? Math.round(equity / value * 100) : 0;
  const power = Math.round(equity * 0.8);
  livePreview.textContent = 'Estimated equity: $' + equity.toLocaleString() + ' (' + pct + '%) · Down payment power: $' + power.toLocaleString();
}

generateBtn.addEventListener('click', () => {
  const form = new FormData();
  form.append('agent_name', document.getElementById('agentName').value);
  form.append('agent_phone', document.getElementById('agentPhone').value);
  form.append('agent_email', document.getElementById('agentEmail').value);
  form.append('print_safe_logo', document.getElementById('printSafeLogo').checked ? '1' : '');
  form.append('client_name', document.getElementById('clientName').value);
  FIELD_IDS.forEach(id => form.append(id, document.getElementById('f_' + id).value));

  generateBtn.disabled = true;
  generateBtn.textContent = 'Generating...';
  fetch('/generate', { method: 'POST', body: form })
    .then(r => {
      if (!r.ok) return r.json().then(e => { throw new Error(e.error || 'Failed'); });
      return r.blob();
    })
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'PEAR_Report.pdf';
      document.body.appendChild(a);
      a.click();
      a.remove();
      generateBtn.disabled = false;
      generateBtn.textContent = 'Generate PDF';
    })
    .catch(err => {
      alert('Error: ' + err.message);
      generateBtn.disabled = false;
      generateBtn.textContent = 'Generate PDF';
    });
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, cfg=load_config())


@app.route("/parse", methods=["POST"])
def parse():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        data = detect_and_parse(f.read(), f.filename or "report.pdf")
        value = data.get("value_est")
        data["suggested_target_price"] = suggested_target_price(value)
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/generate", methods=["POST"])
def generate():
    agent_name = request.form.get("agent_name", "").strip() or "Brian Elmore"
    agent_phone = request.form.get("agent_phone", "").strip()
    agent_email = request.form.get("agent_email", "").strip() or "brian@justinlucasgroup.com"
    print_safe_logo = bool(request.form.get("print_safe_logo", "").strip())
    save_config({
        "agent_name": agent_name,
        "agent_phone": agent_phone,
        "agent_email": agent_email,
        "print_safe_logo": print_safe_logo,
    })

    try:
        value = _num(request.form.get("value"))
        loan_balance = _num(request.form.get("loan_balance"))
        last_purchase_price = _num(request.form.get("last_purchase_price"))
        target_price = _num(request.form.get("target_price"))

        computed = compute_pear(
            value=value,
            loan_balance=loan_balance,
            last_purchase_price=last_purchase_price,
            target_price=target_price,
        )

        fields = {
            "client_name": request.form.get("client_name", "").strip(),
            "full_address": request.form.get("full_address", "").strip(),
            "beds": request.form.get("beds", "").strip(),
            "baths": request.form.get("baths", "").strip(),
            "sqft": request.form.get("sqft", "").strip(),
            "year_built": request.form.get("year_built", "").strip(),
            "property_type": request.form.get("property_type", "").strip(),
            "status_text": request.form.get("status_text", "").strip(),
            "value": value,
            "loan_balance": loan_balance,
            "last_purchase_price": last_purchase_price,
            "last_purchase_year": request.form.get("last_purchase_year", "").strip(),
        }

        out_name = f"PEAR_{uuid.uuid4().hex[:8]}.pdf"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        render_pear(
            fields, computed, out_path,
            agent_name=agent_name, agent_phone=agent_phone, agent_email=agent_email,
            print_safe_logo=print_safe_logo,
        )
        safe_client = re.sub(r"[^A-Za-z0-9]+", "_", fields["client_name"] or "Client").strip("_")
        download_name = f"PEAR_Report_{safe_client}.pdf"
        return send_file(out_path, as_attachment=True, download_name=download_name)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n  JLG PEAR Report Generator is running.")
    print("  Open this in your browser:  http://localhost:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False)

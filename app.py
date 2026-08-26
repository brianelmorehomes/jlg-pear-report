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
import io
import os
import re
import traceback
import uuid
import zipfile

from flask import Flask, request, jsonify, send_file, render_template_string

from parser import detect_and_parse
from calc import compute_pear, suggested_target_price
from render import render_pear

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB upload cap

# Agent name/phone/email/print-safe-logo used to persist server-side in a
# config.json shared by every visitor -- fine for Brian alone, but wrong
# the moment a second agent (Justin, Eric, Camille) opens this same
# deployed app on their own computer: whoever generated a report last
# would silently overwrite the default the NEXT person sees, regardless
# of whose machine it was. That's now handled entirely client-side via
# localStorage instead (see the PAGE template's JS below) -- each
# person's own browser remembers their own info, independent of anyone
# else's. These are just the first-ever-visit fallback, shown only
# until a browser's localStorage has something saved.
DEFAULT_AGENT = {
    "agent_name": "Brian Elmore",
    "agent_phone": "",
    "agent_email": "brian@justinlucasgroup.com",
    "print_safe_logo": False,
}


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


def _safe_filename_part(s, fallback):
    return re.sub(r"[^A-Za-z0-9]+", "_", s or fallback).strip("_")


def _default_fields_from_parsed(data):
    """Batch mode has no human review step -- there's nobody to look at the
    review form and confirm/edit each value before a PDF gets generated.
    So this mirrors, in Python, exactly what populateReview()/smartSet() do
    in the browser for a single upload: same value (avg of the 3 AVMs,
    falling back to Remine's own headline figure), same loan balance, same
    owner-name-as-client-name default, same ~25%-above-value target price.
    A batch-generated report is exactly what a single-report upload would
    produce if the agent hit Generate without touching anything -- which is
    the whole point (Brian's own words: he can always redo any one
    property individually through the normal flow if something looks off)."""
    value = data.get("value_est_avg") or data.get("value_est")
    loan_balance = data.get("loan_balance_est")
    last_purchase_price = data.get("last_sale_price")

    last_purchase_year = ""
    last_sale_date = data.get("last_sale_date")
    if last_sale_date:
        parts = last_sale_date.split("/")
        if len(parts) == 3:
            try:
                yy = int(parts[2])
                last_purchase_year = str(2000 + yy if yy < 50 else 1900 + yy)
            except ValueError:
                pass

    target_price = data.get("suggested_target_price") or suggested_target_price(value)

    avms = []
    valuations = data.get("valuations") or {}
    for label, key in (("First American", "first_american"), ("Zillow", "zillow"), ("Remine", "remine")):
        v = valuations.get(key) or {}
        if v.get("est") is not None:
            avms.append({"label": label, "est": v.get("est"), "low": v.get("low"), "high": v.get("high")})

    fields = {
        "client_name": data.get("owner_names_display") or "",
        "full_address": data.get("full_address") or "",
        "beds": data.get("beds"),
        "baths": data.get("baths"),
        "sqft": data.get("sqft"),
        "year_built": data.get("year_built"),
        "property_type": data.get("property_type") or "",
        "value": value,
        "loan_balance": loan_balance,
        "last_purchase_price": last_purchase_price,
        "last_purchase_year": last_purchase_year,
        "avms": avms,
    }
    computed = compute_pear(
        value=value,
        loan_balance=loan_balance,
        last_purchase_price=last_purchase_price,
        target_price=target_price,
    )
    return fields, computed


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
  #dropzone.locked { cursor: default; background: var(--slate); border-style: solid; border-color: var(--border); color: var(--text); }
  #dropzone.locked:hover { border-color: var(--border); background: var(--slate); }
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
    <p style="margin-top:10px;"><a href="/batch" style="color:var(--blue);font-weight:700;font-size:.88rem;">Generating several reports at once? Try batch mode &rarr;</a></p>
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
        <input type="text" id="clientName" placeholder="e.g. John and Jane Doe" autocomplete="off">
      </div>
    </div>
    <div class="field helper">This defaults from the report's public-record owner name once you upload &mdash; edit it to whatever you'd like shown (nickname, "the Jenks Family," etc).</div>
  </div>

  <div class="card">
    <h2><span class="step-num">2</span>Upload Remine report</h2>
    <details style="margin-bottom:16px;border:1.5px solid var(--border-b);border-radius:var(--r);padding:12px 14px;">
      <summary style="cursor:pointer;font-weight:700;color:var(--blue);font-size:.85rem;">How to pull the right report from Remine</summary>
      <ol style="font-size:.82rem;color:var(--text);line-height:1.6;margin:10px 0 4px;padding-left:20px;">
        <li>In MRED, open Remine and search the property address.</li>
        <li>Open the property record, then click the print icon.</li>
        <li>Under <strong>Choose Print View</strong>, select <strong>Public Record Full</strong> if it's available.</li>
        <li>Under <strong>Select which sections you would like to appear</strong>, check: <strong>Public Record</strong>, <strong>Valuation</strong>, <strong>Property History</strong>, and <strong>Associated People</strong>. Leave Schools and Demographics unchecked.</li>
        <li>Under <strong>and of the valuations, which ones do you want to show?</strong>, check all three: <strong>First American</strong>, <strong>Remine</strong>, and <strong>Zestimate</strong>.</li>
        <li>Click <strong>Print</strong> and save as a PDF, then upload that file below.</li>
      </ol>
      <div class="helper" style="margin-top:8px;">
        <strong>Public Record Full isn't available on every property.</strong> If it's missing from the dropdown, pick <strong>Agent Full</strong> instead — it works fine here too, it just can't limit itself to only those four sections (Listing Details, Property Images, Schools, and Demographics will get pulled in too, and can't be unchecked). That's harmless, just a few extra pages; make sure Valuation, Property History, and Associated People are still checked, and all three valuation sources (First American, Remine, Zestimate) are checked.
      </div>
    </details>
    <div id="dropzone">
      <p><strong>Drag &amp; drop the Remine PDF here</strong></p>
      <p class="hint">or click to browse &mdash; one property at a time</p>
      <input type="file" id="fileInput" accept="application/pdf">
    </div>
    <div class="field helper" style="margin-top:10px;">
      This needs the Remine public-records report (branded "Courtesy of [Agent Name]," with Active Mortgage, Net Equity, and a 3-source Valuation table) &mdash; not an RPR report, which doesn't carry loan/equity data.
    </div>
    <div id="status"></div>
  </div>

  <div class="card hidden" id="reviewCard">
    <h2><span class="step-num">3</span>Review &amp; customize</h2>
    <div id="warnBox"></div>
    <div class="section-divider">Property</div>
    <div class="row">
      <div class="field"><label>Full address</label><input type="text" id="f_full_address" autocomplete="off"></div>
    </div>
    <div class="row">
      <div class="field"><label>Beds</label><input type="text" id="f_beds"></div>
      <div class="field"><label>Baths</label><input type="text" id="f_baths"></div>
      <div class="field"><label>SF</label><input type="text" id="f_sqft"></div>
      <div class="field"><label>Year built</label><input type="text" id="f_year_built"></div>
    </div>
    <div class="row">
      <div class="field"><label>Property type</label><input type="text" id="f_property_type"></div>
    </div>

    <div class="section-divider">Valuation &amp; Equity</div>
    <div class="row">
      <div class="field">
        <label>Estimated market value</label>
        <input type="text" id="f_value">
        <div class="helper" id="avmHelper"></div>
      </div>
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

    <input type="hidden" id="f_avm_fa_est">
    <input type="hidden" id="f_avm_fa_low">
    <input type="hidden" id="f_avm_fa_high">
    <input type="hidden" id="f_avm_zillow_est">
    <input type="hidden" id="f_avm_zillow_low">
    <input type="hidden" id="f_avm_zillow_high">
    <input type="hidden" id="f_avm_remine_est">
    <input type="hidden" id="f_avm_remine_low">
    <input type="hidden" id="f_avm_remine_high">

    <div id="livePreview" style="margin-top:16px;font-size:.85rem;color:var(--muted);"></div>

    <button class="primary" id="generateBtn">Generate PDF</button>
    <button class="secondary" id="reparseBtn">Start over with a different file</button>
  </div>

  <p class="build-credit">&copy; 2026 Brian Elmore. All rights reserved. This tool may not be reproduced or redistributed without permission.</p>
</div>

<script>
// Remembers agent name/phone/email/print-safe-logo on THIS device only,
// via localStorage -- once someone fills these in once on their own
// computer, they stay put on future visits from that same browser
// until changed, without affecting what anyone else sees on theirs.
// (A real HTTP cookie would work too, but localStorage needs no
// server round-trip and is the more standard tool for "remember a
// setting on this device.") First-ever visit on a given browser falls
// back to whatever the server rendered (see DEFAULT_AGENT in app.py).
const AGENT_STORAGE_KEY = 'pear_agent_settings_v1';

function loadAgentSettings() {
  try {
    const raw = localStorage.getItem(AGENT_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved.agent_name != null) document.getElementById('agentName').value = saved.agent_name;
    if (saved.agent_phone != null) document.getElementById('agentPhone').value = saved.agent_phone;
    if (saved.agent_email != null) document.getElementById('agentEmail').value = saved.agent_email;
    if (saved.print_safe_logo != null) document.getElementById('printSafeLogo').checked = !!saved.print_safe_logo;
  } catch (e) { /* localStorage unavailable (private browsing, etc) -- fall back to server defaults silently */ }
}

function saveAgentSettings() {
  try {
    localStorage.setItem(AGENT_STORAGE_KEY, JSON.stringify({
      agent_name: document.getElementById('agentName').value,
      agent_phone: document.getElementById('agentPhone').value,
      agent_email: document.getElementById('agentEmail').value,
      print_safe_logo: document.getElementById('printSafeLogo').checked,
    }));
  } catch (e) { /* ignore -- worst case it just doesn't persist this time */ }
}

loadAgentSettings();
['agentName', 'agentPhone', 'agentEmail', 'printSafeLogo'].forEach(id => {
  document.getElementById(id).addEventListener('change', saveAgentSettings);
});

const dz = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const statusEl = document.getElementById('status');
const reviewCard = document.getElementById('reviewCard');
const warnBox = document.getElementById('warnBox');
const livePreview = document.getElementById('livePreview');
const generateBtn = document.getElementById('generateBtn');
const reparseBtn = document.getElementById('reparseBtn');

const FIELD_IDS = ['full_address','beds','baths','sqft','year_built','property_type',
                    'value','loan_balance','last_purchase_price','last_purchase_year','target_price',
                    'avm_fa_est','avm_fa_low','avm_fa_high','avm_zillow_est','avm_zillow_low','avm_zillow_high',
                    'avm_remine_est','avm_remine_low','avm_remine_high'];

// Tracks the last value THIS SCRIPT auto-filled into each field, so a
// second /parse response (e.g. an accidental second click on the
// dropzone, or a stray drag/drop) can be told apart from a value the
// agent deliberately typed in. Only overwrites a field on re-parse if
// its current value still matches what was auto-filled last time --
// otherwise an edit like correcting a misspelled client name would get
// silently wiped out the moment a second parse fires. See the Cowork
// conversation this was fixed in for the real report that surfaced it.
let lastAutoFill = {};

let hasUploaded = false;

dz.addEventListener('click', () => { if (!hasUploaded) fileInput.click(); });
dz.addEventListener('dragover', e => { if (hasUploaded) return; e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag');
  if (hasUploaded) return; // dropzone is locked once a file's been parsed -- use "Start over" instead
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) handleFile(fileInput.files[0]); fileInput.value=''; });
reparseBtn.addEventListener('click', () => {
  hasUploaded = false;
  lastAutoFill = {};
  dz.classList.remove('locked');
  dz.innerHTML = '<p><strong>Drag &amp; drop the Remine PDF here</strong></p><p class="hint">or click to browse &mdash; one property at a time</p>';
  reviewCard.classList.add('hidden');
  statusEl.textContent = '';
  // Clear every review field, not just the tracking -- otherwise a
  // leftover value from the PREVIOUS property (e.g. an address you'd
  // manually corrected) can silently survive into the NEXT report,
  // because smartSet() treats any non-blank field as a deliberate edit
  // it shouldn't overwrite. This bit Brian for real: an old office
  // address ("2 N Whittaker St") carried over into an unrelated
  // client's report because Start Over hid the card without wiping the
  // inputs underneath it.
  document.getElementById('clientName').value = '';
  FIELD_IDS.forEach(id => { const el = document.getElementById('f_' + id); if (el) el.value = ''; });
  document.getElementById('avmHelper').textContent = '';
  warnBox.innerHTML = '';
  livePreview.textContent = '';
});

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
      hasUploaded = true;
      dz.classList.add('locked');
      dz.innerHTML = '<p><strong>Uploaded:</strong> ' + file.name + '</p><p class="hint">Use "Start over" below to upload a different report</p>';
      reviewCard.classList.remove('hidden');
      reviewCard.scrollIntoView({behavior:'smooth'});
      updatePreview();
    })
    .catch(err => { statusEl.textContent = 'Error: ' + err; });
}

// Sets a review-form field to a new auto-filled default UNLESS the agent
// has already changed it away from the previous auto-fill (see
// lastAutoFill comment above).
function smartSet(id, newValue) {
  const el = document.getElementById(id);
  if (!el) return;
  const current = el.value ?? '';
  const previousAuto = lastAutoFill[id] ?? '';
  if (current === '' || current === previousAuto) {
    el.value = newValue ?? '';
  }
  lastAutoFill[id] = el.value;
}

function populateReview(data) {
  smartSet('clientName', data.owner_names_display || '');
  smartSet('f_full_address', data.full_address || '');
  smartSet('f_beds', data.beds ?? '');
  smartSet('f_baths', data.baths ?? '');
  smartSet('f_sqft', data.sqft ?? '');
  smartSet('f_year_built', data.year_built ?? '');
  smartSet('f_property_type', data.property_type || '');
  smartSet('f_value', (data.value_est_avg ?? data.value_est) ?? '');
  smartSet('f_loan_balance', data.loan_balance_est ?? '');
  smartSet('f_last_purchase_price', data.last_sale_price ?? '');
  let year = '';
  if (data.last_sale_date) {
    const parts = data.last_sale_date.split('/');
    if (parts.length === 3) { let yy = parseInt(parts[2],10); year = (yy < 50 ? 2000+yy : 1900+yy); }
  }
  smartSet('f_last_purchase_year', year);
  smartSet('f_target_price', data.suggested_target_price ?? '');

  const avm = data.valuations || {};
  const fa = avm.first_american || {}, zi = avm.zillow || {}, re = avm.remine || {};
  smartSet('f_avm_fa_est', fa.est ?? ''); smartSet('f_avm_fa_low', fa.low ?? ''); smartSet('f_avm_fa_high', fa.high ?? '');
  smartSet('f_avm_zillow_est', zi.est ?? ''); smartSet('f_avm_zillow_low', zi.low ?? ''); smartSet('f_avm_zillow_high', zi.high ?? '');
  smartSet('f_avm_remine_est', re.est ?? ''); smartSet('f_avm_remine_low', re.low ?? ''); smartSet('f_avm_remine_high', re.high ?? '');

  const avmHelper = document.getElementById('avmHelper');
  const parts2 = [];
  if (fa.est) parts2.push('First American ' + fa.est.toLocaleString());
  if (zi.est) parts2.push('Zillow ' + zi.est.toLocaleString());
  if (re.est) parts2.push('Remine ' + re.est.toLocaleString());
  avmHelper.textContent = parts2.length ? 'Defaulted to the average of: ' + parts2.map(p => '$' + p).join(', ') + '. All three will show on the PDF.' : '';

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
  saveAgentSettings(); // safety net in case 'change' didn't fire for some reason
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
      // Pull the real filename (address + client name) out of the
      // Content-Disposition header the server sends -- a blob download
      // via JS ignores that header entirely unless we read it ourselves
      // and set it as the <a download> attribute below. Hardcoding
      // 'PEAR_Report.pdf' here previously meant the backend's filename
      // fix (adding the property address) never actually showed up in
      // the downloaded file, even though the server was sending it correctly.
      const cd = r.headers.get('Content-Disposition') || '';
      const match = cd.match(/filename="?([^";]+)"?/);
      const filename = match ? match[1] : 'PEAR_Report.pdf';
      return r.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
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


# Batch mode intentionally skips the whole review step -- there's no human
# in the loop per file, so every report gets generated straight from
# _default_fields_from_parsed() with no chance to fix a bad parse before
# the PDF is made. That's a real tradeoff (see Brian's own framing when he
# asked for this): it trades per-file accuracy for throughput, on the
# assumption that any one report that looks wrong afterward gets
# regenerated individually through the normal single-report flow, where it
# CAN be reviewed and corrected. Reusing the same page chrome/CSS as PAGE
# above (duplicated rather than shared, matching how the rest of this
# codebase inlines everything per-tool) keeps it visually consistent.
BATCH_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JLG PEAR Report Generator &ndash; Batch Mode</title>
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
  .hero a { color: var(--blue); }
  .card { background: #fff; border-radius: var(--rl); box-shadow: var(--sh); padding: 28px; margin-bottom: 24px; }
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
  input[type=text] { padding:11px 13px; border:1.5px solid var(--border-b); border-radius: var(--r); font-size:.92rem; font-family: inherit; width:100%; }
  input[type=text]:focus { outline: none; border-color: var(--blue); }
  input[type=checkbox] { accent-color: var(--red); }
  button.primary { display: inline-flex; align-items: center; gap: 10px; background: var(--red); color: #fff; border: none; padding: 13px 24px; border-radius: var(--r); font-family: inherit; font-size:.88rem; font-weight: 700; letter-spacing: .01em; cursor:pointer; margin-top:14px; transition: background var(--d) var(--ease); }
  button.primary:hover { background: var(--red-hv); }
  button.primary:disabled { background:#c9c9c9; cursor:not-allowed; }
  button.secondary { display:inline-flex; align-items:center; gap:8px; background:#fff; color:var(--blue); border:1.5px solid var(--border-b); padding:9px 16px; border-radius:var(--r); font-family:inherit; font-size:.8rem; font-weight:700; cursor:pointer; }
  #status { font-size:.85rem; color:var(--muted); margin-top:10px; }
  .file-row { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:9px 12px; border:1px solid var(--border); border-radius:var(--r); margin-bottom:8px; font-size:.85rem; }
  .file-row .name { overflow-wrap:anywhere; }
  .file-row button { background:none; border:none; color:var(--red); cursor:pointer; font-size:.8rem; font-weight:700; padding:2px 6px; flex-shrink:0; }
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
    <h1>PEAR Report Generator: Batch Mode</h1>
    <p>Drag and drop several Remine reports at once to generate a PDF for each, using the same defaults a single upload would auto-fill (no per-file review). If one property needs a correction, regenerate just that one through the normal single-report flow.</p>
    <p><a href="/">&larr; Back to single-report mode</a></p>
  </div>

  <div class="card">
    <h2><span class="step-num">1</span>Agent details</h2>
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
    <label style="display:flex;align-items:center;gap:7px;margin-top:16px;font-size:.82rem;color:var(--text);cursor:pointer;">
      <input type="checkbox" id="printSafeLogo" {{ 'checked' if cfg.print_safe_logo else '' }} style="margin:0;">
      Print-safe logo (black &amp; white)
    </label>
  </div>

  <div class="card">
    <h2><span class="step-num">2</span>Upload Remine reports</h2>
    <div class="field helper" style="margin-bottom:14px;">
      Each report auto-fills the same way a single upload would: value defaults to the average of the 3 AVMs, mortgage balance and client name come straight from the parsed record, and the move-up target price defaults to ~25% above value. None of that is editable per file here.
    </div>
    <div id="dropzone">
      <p><strong>Drag &amp; drop multiple Remine PDFs here</strong></p>
      <p class="hint">or click to browse and select several at once</p>
      <input type="file" id="fileInput" accept="application/pdf" multiple>
    </div>
    <div id="fileList" style="margin-top:14px;"></div>
    <div id="status"></div>
    <button class="primary" id="generateBtn" disabled>Generate all PDFs</button>
  </div>

  <p class="build-credit">&copy; 2026 Brian Elmore. All rights reserved. This tool may not be reproduced or redistributed without permission.</p>
</div>

<script>
const AGENT_STORAGE_KEY = 'pear_agent_settings_v1';

function loadAgentSettings() {
  try {
    const raw = localStorage.getItem(AGENT_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved.agent_name != null) document.getElementById('agentName').value = saved.agent_name;
    if (saved.agent_phone != null) document.getElementById('agentPhone').value = saved.agent_phone;
    if (saved.agent_email != null) document.getElementById('agentEmail').value = saved.agent_email;
    if (saved.print_safe_logo != null) document.getElementById('printSafeLogo').checked = !!saved.print_safe_logo;
  } catch (e) { /* localStorage unavailable -- fall back to server defaults silently */ }
}

function saveAgentSettings() {
  try {
    localStorage.setItem(AGENT_STORAGE_KEY, JSON.stringify({
      agent_name: document.getElementById('agentName').value,
      agent_phone: document.getElementById('agentPhone').value,
      agent_email: document.getElementById('agentEmail').value,
      print_safe_logo: document.getElementById('printSafeLogo').checked,
    }));
  } catch (e) { /* ignore */ }
}

loadAgentSettings();
['agentName', 'agentPhone', 'agentEmail', 'printSafeLogo'].forEach(id => {
  document.getElementById(id).addEventListener('change', saveAgentSettings);
});

const dz = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const fileListEl = document.getElementById('fileList');
const statusEl = document.getElementById('status');
const generateBtn = document.getElementById('generateBtn');

// Keyed by name+size so dropping the same folder twice, or a drag then a
// browse of overlapping files, doesn't silently duplicate an entry.
let selectedFiles = [];

function fileKey(f) { return f.name + '::' + f.size; }

function addFiles(fileArray) {
  for (const f of fileArray) {
    if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) continue;
    if (selectedFiles.some(existing => fileKey(existing) === fileKey(f))) continue;
    selectedFiles.push(f);
  }
  renderFileList();
}

function removeFile(key) {
  selectedFiles = selectedFiles.filter(f => fileKey(f) !== key);
  renderFileList();
}

function renderFileList() {
  fileListEl.innerHTML = '';
  selectedFiles.forEach(f => {
    const row = document.createElement('div');
    row.className = 'file-row';
    const key = fileKey(f);
    row.innerHTML = '<span class="name">' + f.name + '</span>';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Remove';
    btn.addEventListener('click', () => removeFile(key));
    row.appendChild(btn);
    fileListEl.appendChild(row);
  });
  generateBtn.disabled = selectedFiles.length === 0;
  statusEl.textContent = selectedFiles.length ? selectedFiles.length + ' file(s) ready.' : '';
}

dz.addEventListener('click', () => fileInput.click());
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag');
  if (e.dataTransfer.files.length) addFiles(Array.from(e.dataTransfer.files));
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) addFiles(Array.from(fileInput.files));
  fileInput.value = '';
});

generateBtn.addEventListener('click', () => {
  saveAgentSettings();
  if (!selectedFiles.length) return;

  const form = new FormData();
  form.append('agent_name', document.getElementById('agentName').value);
  form.append('agent_phone', document.getElementById('agentPhone').value);
  form.append('agent_email', document.getElementById('agentEmail').value);
  form.append('print_safe_logo', document.getElementById('printSafeLogo').checked ? '1' : '');
  selectedFiles.forEach(f => form.append('files', f));

  generateBtn.disabled = true;
  generateBtn.textContent = 'Generating ' + selectedFiles.length + ' report(s)...';
  statusEl.textContent = 'This can take a little while for a large batch -- please leave this tab open.';

  fetch('/generate_batch', { method: 'POST', body: form })
    .then(r => {
      if (!r.ok) return r.json().then(e => { throw new Error(e.error || 'Failed'); });
      const cd = r.headers.get('Content-Disposition') || '';
      const match = cd.match(/filename="?([^";]+)"?/);
      const filename = match ? match[1] : 'PEAR_Reports_Batch.zip';
      return r.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      statusEl.textContent = 'Done -- check the zip\\'s _batch_summary.txt for any warnings or files that failed to parse.';
      generateBtn.disabled = false;
      generateBtn.textContent = 'Generate all PDFs';
    })
    .catch(err => {
      alert('Error: ' + err.message);
      statusEl.textContent = '';
      generateBtn.disabled = false;
      generateBtn.textContent = 'Generate all PDFs';
    });
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, cfg=DEFAULT_AGENT)


@app.route("/batch")
def batch_page():
    return render_template_string(BATCH_PAGE, cfg=DEFAULT_AGENT)


@app.route("/parse", methods=["POST"])
def parse():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        data = detect_and_parse(f.read(), f.filename or "report.pdf")
        # Default the report's headline value to the average of whatever AVM
        # estimates were found (Brian's call -- see the Cowork conversation:
        # Remine's own "$X Est Value" stat-line figure is NOT an average, it's
        # just whichever single source Remine chose to feature, which in
        # testing exactly mirrored the First American AVM alone). Falls back
        # to that headline figure only if no AVM table was found at all.
        value = data.get("value_est_avg") or data.get("value_est")
        data["suggested_target_price"] = suggested_target_price(value)
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/generate", methods=["POST"])
def generate():
    # Agent name/phone/email/print-safe-logo are no longer persisted here --
    # that's now the browser's job via localStorage (see the PAGE template's
    # JS). This route just uses whatever the form submitted for this one
    # PDF, same as any other field.
    agent_name = request.form.get("agent_name", "").strip() or "Brian Elmore"
    agent_phone = request.form.get("agent_phone", "").strip()
    agent_email = request.form.get("agent_email", "").strip() or "brian@justinlucasgroup.com"
    print_safe_logo = bool(request.form.get("print_safe_logo", "").strip())

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

        avms = []
        for label, prefix in (("First American", "avm_fa"), ("Zillow", "avm_zillow"), ("Remine", "avm_remine")):
            est = _num(request.form.get(f"{prefix}_est"))
            if est is not None:
                avms.append({
                    "label": label,
                    "est": est,
                    "low": _num(request.form.get(f"{prefix}_low")),
                    "high": _num(request.form.get(f"{prefix}_high")),
                })

        fields = {
            "client_name": request.form.get("client_name", "").strip(),
            "full_address": request.form.get("full_address", "").strip(),
            "beds": request.form.get("beds", "").strip(),
            "baths": request.form.get("baths", "").strip(),
            "sqft": request.form.get("sqft", "").strip(),
            "year_built": request.form.get("year_built", "").strip(),
            "property_type": request.form.get("property_type", "").strip(),
            "value": value,
            "loan_balance": loan_balance,
            "last_purchase_price": last_purchase_price,
            "last_purchase_year": request.form.get("last_purchase_year", "").strip(),
            "avms": avms,
        }

        out_name = f"PEAR_{uuid.uuid4().hex[:8]}.pdf"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        render_pear(
            fields, computed, out_path,
            agent_name=agent_name, agent_phone=agent_phone, agent_email=agent_email,
            print_safe_logo=print_safe_logo,
        )
        # Filename includes the property address (Brian's ask -- makes a
        # folder of reports scannable/searchable by property, since one
        # client can have multiple properties and one address can get
        # multiple reports over time) plus the client name for clarity
        # when several files land in the same downloads folder.
        safe_address = _safe_filename_part(fields["full_address"], "Property")
        safe_client = _safe_filename_part(fields["client_name"], "Client")
        download_name = f"PEAR_Report_{safe_address}_{safe_client}.pdf"
        return send_file(out_path, as_attachment=True, download_name=download_name)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/generate_batch", methods=["POST"])
def generate_batch():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    agent_name = request.form.get("agent_name", "").strip() or "Brian Elmore"
    agent_phone = request.form.get("agent_phone", "").strip()
    agent_email = request.form.get("agent_email", "").strip() or "brian@justinlucasgroup.com"
    print_safe_logo = bool(request.form.get("print_safe_logo", "").strip())

    # One PDF per uploaded file, all zipped together for a single
    # download. A single bad file (unreadable PDF, wrong report type,
    # totally unrecognizable format) shouldn't sink the other N-1 that
    # parsed fine -- so each file gets its own try/except, and failures
    # are recorded in the summary instead of aborting the batch.
    results = []
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for f in files:
            original_name = f.filename or "report.pdf"
            try:
                data = detect_and_parse(f.read(), original_name)
                fields, computed = _default_fields_from_parsed(data)

                out_name = f"PEAR_{uuid.uuid4().hex[:8]}.pdf"
                out_path = os.path.join(OUTPUT_DIR, out_name)
                render_pear(
                    fields, computed, out_path,
                    agent_name=agent_name, agent_phone=agent_phone, agent_email=agent_email,
                    print_safe_logo=print_safe_logo,
                )

                safe_address = _safe_filename_part(fields["full_address"], "Property")
                safe_client = _safe_filename_part(fields["client_name"], "Client")
                pdf_name = f"PEAR_Report_{safe_address}_{safe_client}.pdf"
                # Two different source files can land on the same
                # address/client combo (e.g. re-uploading a corrected
                # report) -- de-dupe inside the zip rather than silently
                # overwrite one with the other.
                base_name = pdf_name[:-4]
                n = 2
                while pdf_name in used_names:
                    pdf_name = f"{base_name}_{n}.pdf"
                    n += 1
                used_names.add(pdf_name)

                with open(out_path, "rb") as pf:
                    zf.writestr(pdf_name, pf.read())
                try:
                    os.remove(out_path)
                except OSError:
                    # Best-effort cleanup of the scratch temp file only --
                    # the report is already safely written into the zip
                    # above, so a failure here (locked file, odd
                    # filesystem/permissions) must never be treated as a
                    # failed generation.
                    pass

                results.append({
                    "source_file": original_name,
                    "output_file": pdf_name,
                    "status": "Generated",
                    "warnings": data.get("parse_warnings") or [],
                })
            except Exception as e:
                traceback.print_exc()
                results.append({
                    "source_file": original_name,
                    "output_file": None,
                    "status": f"FAILED to generate: {e}",
                    "warnings": [],
                })

        summary_lines = [
            "PEAR Report Batch -- Summary",
            f"{len(results)} file(s) processed, {sum(1 for r in results if r['output_file'])} PDF(s) generated.",
            "",
            "Every report here used default values (average of the 3 AVMs, parsed",
            "mortgage balance, public-record owner name, ~25%-above-value target",
            "price) with no per-file review. If a property below shows a warning,",
            "or looks off once you open the PDF, regenerate just that one through",
            "the normal single-report flow so you can review/correct it first.",
            "",
        ]
        for r in results:
            summary_lines.append(f"- {r['source_file']}  ->  {r['output_file'] or '(not generated)'}")
            summary_lines.append(f"    Status: {r['status']}")
            for w in r["warnings"]:
                summary_lines.append(f"    Heads up: {w}")
            summary_lines.append("")
        zf.writestr("_batch_summary.txt", "\n".join(summary_lines))

    zip_buf.seek(0)
    return send_file(
        zip_buf,
        as_attachment=True,
        download_name="PEAR_Reports_Batch.zip",
        mimetype="application/zip",
    )


if __name__ == "__main__":
    print("\n  JLG PEAR Report Generator is running.")
    print("  Open this in your browser:  http://localhost:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False)

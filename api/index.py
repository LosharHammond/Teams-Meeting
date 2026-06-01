"""
Teams Meeting Summarizer — Vercel Flask App
===========================================
Routes:
  GET  /              — Live dashboard
  GET  /api/poll      — Run one poll cycle (Vercel cron target, every minute)
  GET  /api/status    — JSON list of all tracked meetings
  POST /api/reset     — Reset meetings  { "target": "failed"|"all"|"done" }
"""
import logging
import os
import sys

# Make the project root importable when running inside /api
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, jsonify, request

from core import db
from core.config import CRON_SECRET
from core.pipeline import run_poll_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("summarizer.app")

app = Flask(__name__)

# Initialise the Postgres schema on first import
try:
    db.init_db()
except Exception as exc:
    log.error("DB init failed: %s", exc)


# ===========================================================================
# Security helpers
# ===========================================================================

def _is_cron_request() -> bool:
    """
    Vercel sends Authorization: Bearer <CRON_SECRET> on every scheduled invocation.
    Also accept requests from localhost (for manual testing).
    If CRON_SECRET is not configured, allow all (useful during initial setup).
    """
    if not CRON_SECRET:
        return True
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {CRON_SECRET}":
        return True
    # Allow direct browser/curl calls from Vercel preview URLs for testing
    host = request.headers.get("Host", "")
    if "localhost" in host or "127.0.0.1" in host:
        return True
    return False


# ===========================================================================
# Routes
# ===========================================================================

@app.route("/api/poll", methods=["GET", "POST"])
def poll():
    if not _is_cron_request():
        return jsonify({"error": "Unauthorized"}), 401

    log.info("Poll cycle triggered")
    try:
        stats = run_poll_cycle()
        log.info("Poll complete: %s", stats)
        return jsonify({"ok": True, "stats": stats}), 200
    except Exception as exc:
        log.exception("Poll cycle error")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/status")
def status():
    try:
        rows = db.all_rows()
        return jsonify({"ok": True, "meetings": rows}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reset", methods=["POST"])
def reset():
    if not _is_cron_request():
        return jsonify({"error": "Unauthorized"}), 401
    target = (request.get_json(silent=True) or {}).get("target", "failed")
    count  = db.reset_meetings(target)
    return jsonify({"ok": True, "reset": count, "target": target}), 200


@app.route("/")
def dashboard():
    try:
        rows = db.all_rows()
    except Exception:
        rows = []

    state_colour = {
        "DONE":               ("#d4edda", "#155724"),
        "TRANSCRIPT_PENDING": ("#fff3cd", "#856404"),
        "FAILED":             ("#f8d7da", "#721c24"),
        "NEW":                ("#d1ecf1", "#0c5460"),
    }

    rows_html = ""
    for r in rows:
        bg, fg = state_colour.get(r.get("state", ""), ("#f5f5f5", "#333"))
        subj = (r.get("subject") or "—")[:55]
        state = r.get("state", "—")
        reason = r.get("fail_reason") or ""
        updated = (r.get("updated_at") or "")[:16].replace("T", " ")
        rows_html += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #e8f5e9;">{subj}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #e8f5e9;">
            <span style="background:{bg};color:{fg};padding:3px 9px;border-radius:12px;
                         font-size:11px;font-weight:700;">{state}</span>
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #e8f5e9;color:#666;font-size:12px;">{updated}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #e8f5e9;color:#888;font-size:11px;">{reason}</td>
        </tr>"""

    if not rows_html:
        rows_html = """<tr><td colspan="4" style="text-align:center;padding:30px;color:#aaa;">
            No meetings tracked yet. Run a poll cycle to begin.</td></tr>"""

    total    = len(rows)
    done     = sum(1 for r in rows if r.get("state") == "DONE")
    pending  = sum(1 for r in rows if r.get("state") == "TRANSCRIPT_PENDING")
    failed   = sum(1 for r in rows if r.get("state") == "FAILED")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Teams Summarizer — Procus Ghana Ltd.</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f0f7f0; color: #212121; }}

    /* Header */
    .header {{
      background: linear-gradient(135deg, #1b5e20 0%, #2e7d32 60%, #388e3c 100%);
      padding: 0;
    }}
    .header-inner {{
      max-width: 1000px; margin: 0 auto; padding: 28px 32px;
      display: flex; align-items: center; justify-content: space-between;
    }}
    .logo-text {{ color: #fff; }}
    .logo-text h1 {{ font-size: 26px; font-weight: 800; letter-spacing: 0.5px; }}
    .logo-text p  {{ font-size: 12px; color: #a5d6a7; letter-spacing: 1.5px;
                     text-transform: uppercase; margin-top: 4px; }}
    .logo-badge {{
      width: 56px; height: 56px; border-radius: 50%;
      background: linear-gradient(135deg, #ff8c00, #ffa726);
      display: flex; align-items: center; justify-content: center;
      font-size: 20px; font-weight: 900; color: #fff;
    }}
    .accent-bar {{
      height: 4px;
      background: linear-gradient(90deg, #ff8c00 0%, #ffa726 40%, #2e7d32 100%);
    }}

    /* Layout */
    .container {{ max-width: 1000px; margin: 32px auto; padding: 0 24px; }}

    /* Stat cards */
    .stats {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 16px; margin-bottom: 28px; }}
    .stat-card {{
      background: #fff; border-radius: 10px; padding: 20px 24px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07);
      border-top: 3px solid #ccc;
    }}
    .stat-card.green  {{ border-color: #2e7d32; }}
    .stat-card.orange {{ border-color: #ff8c00; }}
    .stat-card.red    {{ border-color: #e53935; }}
    .stat-card.blue   {{ border-color: #1976d2; }}
    .stat-card .num  {{ font-size: 36px; font-weight: 800; line-height: 1; }}
    .stat-card .lbl  {{ font-size: 12px; color: #777; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
    .stat-card.green  .num {{ color: #2e7d32; }}
    .stat-card.orange .num {{ color: #ff8c00; }}
    .stat-card.red    .num {{ color: #e53935; }}
    .stat-card.blue   .num {{ color: #1976d2; }}

    /* Actions */
    .actions {{ display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }}
    .btn {{
      padding: 10px 20px; border-radius: 8px; border: none; cursor: pointer;
      font-size: 13px; font-weight: 700; letter-spacing: 0.3px; text-decoration: none;
      display: inline-flex; align-items: center; gap: 6px;
    }}
    .btn-green  {{ background: #2e7d32; color: #fff; }}
    .btn-green:hover  {{ background: #1b5e20; }}
    .btn-orange {{ background: #ff8c00; color: #fff; }}
    .btn-orange:hover {{ background: #e65100; }}
    .btn-outline {{ background: #fff; color: #2e7d32; border: 2px solid #2e7d32; }}
    .btn-outline:hover {{ background: #e8f5e9; }}

    /* Table */
    .card {{
      background: #fff; border-radius: 10px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07); overflow: hidden;
    }}
    .card-header {{
      background: #f1f8e9; padding: 14px 20px;
      border-bottom: 2px solid #c8e6c9;
      display: flex; align-items: center; justify-content: space-between;
    }}
    .card-header h2 {{ font-size: 15px; color: #2e7d32; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      background: #f9fbe7; padding: 11px 14px; text-align: left;
      font-size: 11px; font-weight: 700; color: #558b2f;
      text-transform: uppercase; letter-spacing: 0.5px;
      border-bottom: 2px solid #e8f5e9;
    }}

    /* Footer */
    .footer {{
      text-align: center; padding: 24px; color: #aaa; font-size: 11px; margin-top: 32px;
    }}

    /* Status dot */
    .dot {{ width: 8px; height: 8px; border-radius: 50%;
            background: #4caf50; display: inline-block; margin-right: 6px;
            box-shadow: 0 0 0 2px #a5d6a7; animation: pulse 2s infinite; }}
    @keyframes pulse {{
      0%,100% {{ box-shadow: 0 0 0 2px #a5d6a7; }}
      50%      {{ box-shadow: 0 0 0 5px #c8e6c9; }}
    }}
  </style>
</head>
<body>

<div class="header">
  <div class="header-inner">
    <div class="logo-text">
      <h1>PROCUS GHANA LTD.</h1>
      <p>Teams Meeting Summarizer &nbsp;&#8212;&nbsp; AI-Powered Recaps</p>
    </div>
    <div class="logo-badge">PG</div>
  </div>
</div>
<div class="accent-bar"></div>

<div class="container">

  <!-- Stats -->
  <div class="stats">
    <div class="stat-card blue">
      <div class="num">{total}</div>
      <div class="lbl">Total Tracked</div>
    </div>
    <div class="stat-card green">
      <div class="num">{done}</div>
      <div class="lbl">Recaps Sent</div>
    </div>
    <div class="stat-card orange">
      <div class="num">{pending}</div>
      <div class="lbl">Awaiting Transcript</div>
    </div>
    <div class="stat-card red">
      <div class="num">{failed}</div>
      <div class="lbl">Failed</div>
    </div>
  </div>

  <!-- Actions -->
  <div class="actions">
    <button class="btn btn-green" onclick="runPoll()">&#9654;&nbsp; Run Poll Now</button>
    <button class="btn btn-orange" onclick="resetFailed()">&#8635;&nbsp; Reset Failed</button>
    <button class="btn btn-outline" onclick="location.reload()">&#8617;&nbsp; Refresh</button>
    <a href="/api/status" class="btn btn-outline" target="_blank">&#123;&#125;&nbsp; JSON Status</a>
  </div>

  <!-- Meeting table -->
  <div class="card">
    <div class="card-header">
      <h2><span class="dot"></span> Meeting State Tracker</h2>
      <span style="font-size:11px;color:#888;">Auto-updates every 60 seconds via cron</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Meeting Subject</th>
          <th>State</th>
          <th>Last Updated</th>
          <th>Fail Reason</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>

  <div class="footer">
    &copy; {__import__('datetime').datetime.now().year} Procus Ghana Ltd.
    &nbsp;&#183;&nbsp; Designed by the IT Department
    &nbsp;&#183;&nbsp; Confidential — Internal Use Only
  </div>

</div>

<script>
  async function runPoll() {{
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = 'Running...';
    try {{
      const r = await fetch('/api/poll', {{ method: 'POST' }});
      const d = await r.json();
      alert(d.ok
        ? 'Poll complete! ' + JSON.stringify(d.stats)
        : 'Error: ' + d.error);
      location.reload();
    }} catch(e) {{
      alert('Request failed: ' + e);
    }} finally {{
      btn.disabled = false;
      btn.innerHTML = '&#9654;&nbsp; Run Poll Now';
    }}
  }}

  async function resetFailed() {{
    if (!confirm('Reset all FAILED meetings to NEW so they will be retried?')) return;
    const r = await fetch('/api/reset', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{target:'failed'}})
    }});
    const d = await r.json();
    alert(d.ok ? `Reset ${{d.reset}} meeting(s)` : 'Error: ' + d.error);
    location.reload();
  }}
</script>
</body>
</html>"""

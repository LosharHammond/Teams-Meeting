"""
Teams Meeting Summarizer — Flask App (Railway)
===============================================
Routes:
  GET  /              — Live dashboard
  GET  /api/poll      — Trigger a poll cycle manually (or via cron.py)
  GET  /api/status    — JSON list of all tracked meetings
  POST /api/reset     — Reset meetings  { "target": "failed"|"all"|"done" }

Deployment: Railway (gunicorn via Procfile / railway.json)
Cron:       Railway Cron Service → python cron.py  [* * * * *  — every minute]
"""
import logging
import os
import sys

# Make the project root importable when running inside /api
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("summarizer.app")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Lazy-load all config-dependent modules so a missing env var shows a helpful
# setup page instead of an unhandled 500 on every route.
# ---------------------------------------------------------------------------
_SETUP_ERROR: str = ""   # non-empty = show setup page
_CRON_SECRET: str = ""

_REQUIRED_VARS = [
    "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
    "OPENAI_API_KEY", "ORGANIZER_EMAIL", "DATABASE_URL",
]

def _bootstrap():
    """Import heavy modules once; set _SETUP_ERROR on any failure."""
    global _SETUP_ERROR, _CRON_SECRET
    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v, "").strip()]
    if missing:
        _SETUP_ERROR = (
            "Missing required environment variables: "
            + ", ".join(f"<code>{v}</code>" for v in missing)
            + "<br><br>Add them in <b>Vercel → Project → Settings → Environment Variables</b>, "
            "then redeploy."
        )
        return

    try:
        from core.config import CRON_SECRET
        _CRON_SECRET = CRON_SECRET
    except Exception as exc:
        _SETUP_ERROR = f"Config error: {exc}"
        return

    try:
        from core import db as _db
        _db.init_db()
    except Exception as exc:
        log.error("DB init failed: %s", exc)
        _SETUP_ERROR = (
            f"Database connection failed: <code>{exc}</code><br><br>"
            "Check that <b>DATABASE_URL</b> is a valid PostgreSQL connection string "
            "(Neon, Supabase, etc.) in Vercel Environment Variables."
        )

_bootstrap()


# ===========================================================================
# Security helpers
# ===========================================================================

def _is_cron_request() -> bool:
    """
    Accept the request if ANY of the following is true:
      1. CRON_SECRET is not configured (initial setup / open mode).
      2. Authorization: Bearer <CRON_SECRET> header matches (Vercel cron / curl).
      3. Request comes from localhost (local testing).
      4. Request originates from the same Vercel deployment — Referer or Origin
         header shares the same host.  This lets the dashboard "Run Poll Now"
         button work without embedding the secret in the page HTML.
    """
    if not _CRON_SECRET:
        return True

    # Bearer token — Vercel cron scheduler and authorised curl calls
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {_CRON_SECRET}":
        return True

    host = request.headers.get("Host", "")

    # Localhost — developer testing
    if "localhost" in host or "127.0.0.1" in host:
        return True

    # Same-origin dashboard button (Referer / Origin contains the same host)
    if host:
        for header in ("Referer", "Origin"):
            if host in request.headers.get(header, ""):
                return True

    return False


# ===========================================================================
# Routes
# ===========================================================================

@app.route("/api/poll", methods=["GET", "POST"])
def poll():
    if _SETUP_ERROR:
        return jsonify({"error": "App not configured", "detail": _SETUP_ERROR}), 503
    if not _is_cron_request():
        return jsonify({"error": "Unauthorized"}), 401

    from core.pipeline import run_poll_cycle
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
    if _SETUP_ERROR:
        return jsonify({"error": "App not configured", "detail": _SETUP_ERROR}), 503
    from core import db
    try:
        rows = db.all_rows()
        return jsonify({"ok": True, "meetings": rows}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reset", methods=["POST"])
def reset():
    if _SETUP_ERROR:
        return jsonify({"error": "App not configured", "detail": _SETUP_ERROR}), 503
    if not _is_cron_request():
        return jsonify({"error": "Unauthorized"}), 401
    from core import db
    target = (request.get_json(silent=True) or {}).get("target", "failed")
    count  = db.reset_meetings(target)
    return jsonify({"ok": True, "reset": count, "target": target}), 200


@app.route("/")
def dashboard():
    # Show a clear setup page if env vars are missing — no 500
    if _SETUP_ERROR:
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Teams Summarizer — Setup Required</title>
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#fff8e1;display:flex;
       align-items:center;justify-content:center;min-height:100vh;margin:0;}}
  .box{{background:#fff;border-radius:12px;padding:40px 48px;max-width:560px;
        box-shadow:0 4px 24px rgba(0,0,0,.10);border-top:5px solid #ff8c00;}}
  h2{{color:#e65100;margin:0 0 16px;}}
  p{{color:#555;line-height:1.7;}}
  .vars{{background:#f5f5f5;border-radius:8px;padding:16px 20px;margin:20px 0;}}
  .vars li{{font-family:monospace;font-size:13px;margin:4px 0;color:#333;}}
  a{{color:#2e7d32;font-weight:700;}}
</style></head>
<body><div class="box">
  <h2>&#9888; Setup Required</h2>
  <p>{_SETUP_ERROR}</p>
  <div class="vars">
    <b>Required variables:</b>
    <ul>{"".join(f"<li>{v}</li>" for v in _REQUIRED_VARS)}</ul>
  </div>
  <p>After adding the variables, trigger a new Vercel deployment (push a commit
  or click <b>Redeploy</b> in the Vercel dashboard).</p>
</div></body></html>""", 503

    from core import db
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
        _upd = r.get("updated_at") or ""
        updated = (str(_upd).replace("+00:00", "")[:16]).replace("T", " ")
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
      <span style="font-size:11px;color:#888;">&#9679; Live — polls every minute, recaps delivered within 2–5 min of meeting end</span>
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

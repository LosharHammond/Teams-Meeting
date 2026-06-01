"""Procus Ghana Ltd. branded email HTML template."""

EMAIL_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<body style="margin:0;padding:20px 0;background-color:#f0f7f0;">

<div style="font-family:'Segoe UI',Arial,sans-serif;max-width:680px;margin:0 auto;
            border-radius:12px;overflow:hidden;
            box-shadow:0 4px 24px rgba(0,0,0,0.10);">

  <!-- TOP ACCENT BAR -->
  <div style="height:5px;background:linear-gradient(90deg,#2e7d32 0%,#66bb6a 50%,#ff8c00 100%);"></div>

  <!-- HEADER -->
  <div style="background:#ffffff;padding:28px 36px 20px 36px;border-bottom:1px solid #e8f5e9;">
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="vertical-align:middle;">
          <div style="font-size:22px;font-weight:800;color:#1b5e20;letter-spacing:0.4px;line-height:1.2;">
            PROCUS GHANA LTD.
          </div>
          <div style="margin-top:5px;">
            <span style="display:inline-block;
                         background:linear-gradient(90deg,#2e7d32,#388e3c);
                         color:#ffffff;font-size:10px;font-weight:700;
                         letter-spacing:1.8px;text-transform:uppercase;
                         padding:3px 10px;border-radius:20px;">
              &#9679;&nbsp;AI Meeting Recap
            </span>
          </div>
        </td>
        <td style="text-align:right;vertical-align:middle;">
          <div style="width:52px;height:52px;border-radius:50%;
                      background:linear-gradient(135deg,#ff8c00 0%,#ffa726 100%);
                      display:inline-block;line-height:52px;text-align:center;
                      font-size:22px;font-weight:900;color:#ffffff;">
            PG
          </div>
        </td>
      </tr>
    </table>
  </div>

  <!-- DIVIDER LABEL -->
  <div style="background:#f1f8e9;padding:10px 36px;border-left:4px solid #ff8c00;">
    <p style="margin:0;font-size:12px;color:#558b2f;font-weight:600;letter-spacing:0.5px;">
      AUTOMATED MEETING SUMMARY &nbsp;&#8212;&nbsp; CONFIDENTIAL
    </p>
  </div>

  <!-- BODY CONTENT -->
  <div style="background:#ffffff;padding:30px 36px;">
    <div style="color:#212121;font-size:14px;line-height:1.75;">
      {body}
    </div>
  </div>

  <!-- FOOTER -->
  <div style="background:#f9fbe7;border-top:2px solid #e8f5e9;padding:20px 36px;">
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="vertical-align:top;padding-right:20px;">
          <p style="margin:0 0 4px 0;font-size:12px;font-weight:700;color:#2e7d32;">
            Procus Ghana Ltd. &mdash; IT Department
          </p>
          <p style="margin:0;font-size:11px;color:#666;line-height:1.6;">
            This recap was automatically generated from the<br>
            meeting transcript using AI. Please review carefully<br>
            before acting on any information contained herein.
          </p>
        </td>
        <td style="text-align:right;vertical-align:top;white-space:nowrap;">
          <p style="margin:0 0 4px 0;font-size:11px;color:#999;">Experiencing an issue?</p>
          <p style="margin:0;font-size:12px;font-weight:700;color:#ff8c00;">
            Contact the IT Department
          </p>
        </td>
      </tr>
    </table>
    <div style="margin-top:16px;border-top:1px solid #dcedc8;padding-top:12px;text-align:center;">
      <p style="margin:0;font-size:10px;color:#aaa;letter-spacing:0.5px;">
        &copy; {year}&nbsp;Procus Ghana Ltd.&ensp;&#183;&ensp;Confidential&ensp;&#183;&ensp;For Internal Use Only
      </p>
    </div>
  </div>

  <!-- BOTTOM ACCENT BAR -->
  <div style="height:4px;background:linear-gradient(90deg,#ff8c00 0%,#ffa726 50%,#2e7d32 100%);"></div>

</div>
</body>
</html>
"""


def render(body_html: str) -> str:
    from datetime import datetime, timezone
    return EMAIL_TEMPLATE.format(
        body=body_html,
        year=datetime.now(timezone.utc).year,
    )

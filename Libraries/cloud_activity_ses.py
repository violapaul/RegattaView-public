"""
SES delivery for AWS usage reports.

Sends the Markdown report rendered to inline-styled HTML. Body is also
included as text/plain so MUAs that strip HTML still show something.

The sender identity must be verified in SES once before this works.
In sandbox mode, SES will only send to verified recipients — using
sender == recipient (your inbox) avoids needing production access.
"""

from __future__ import annotations

import re
from html import escape

import boto3
from botocore.exceptions import ClientError

from Libraries.log_helper import LOG


# --- Markdown → HTML (small, fixed-shape) ------------------------------------
# We control the report shape (Libraries/cloud_activity.render_markdown), so we
# don't need a general-purpose Markdown parser. This handles: # / ## headings,
# `code`, **bold**, GFM tables, bullet lists, --- horizontal rule, paragraphs.

_BACKTICK = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> str:
    out = escape(text)
    out = _BACKTICK.sub(r'<code>\1</code>', out)
    out = _BOLD.sub(r'<strong>\1</strong>', out)
    return out


def markdown_to_html(md: str) -> str:
    css = (
        "body{font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "color:#222;max-width:920px;margin:24px auto;padding:0 16px}"
        "h1{font-size:22px;border-bottom:1px solid #ddd;padding-bottom:6px}"
        "h2{font-size:17px;margin-top:24px;color:#333}"
        "table{border-collapse:collapse;margin:8px 0;font-size:13px}"
        "th,td{border:1px solid #d8d8d8;padding:4px 10px;text-align:left}"
        "th{background:#f6f8fa}"
        "code{background:#f0f0f0;padding:1px 5px;border-radius:3px;font-size:12px}"
        "li{margin:2px 0}"
        ".sev-high{color:#b00020;font-weight:bold}"
        "hr{border:0;border-top:1px solid #ddd;margin:24px 0}"
        "footer{font-size:12px;color:#888}"
    )
    body: list[str] = []
    in_table = False
    table_rows: list[list[str]] = []
    in_list = False

    def _close_list() -> None:
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    def _close_table() -> None:
        nonlocal in_table
        if in_table:
            html_rows = []
            for i, row in enumerate(table_rows):
                tag = "th" if i == 0 else "td"
                cells = "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in row)
                html_rows.append(f"<tr>{cells}</tr>")
            body.append("<table>" + "".join(html_rows) + "</table>")
            table_rows.clear()
            in_table = False

    for raw in md.splitlines():
        line = raw.rstrip()
        # Skip pipe-table separator lines |---|---|
        if re.match(r"^\|\s*-+\s*(\|\s*-+\s*)+\|?\s*$", line):
            continue
        if line.startswith("|") and line.endswith("|"):
            _close_list()
            cells = [c.strip() for c in line.strip("|").split("|")]
            in_table = True
            table_rows.append(cells)
            continue
        else:
            _close_table()

        if not line.strip():
            _close_list()
            continue

        if line.startswith("# "):
            _close_list()
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            _close_list()
            body.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line == "---":
            _close_list()
            body.append("<hr/>")
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            content = line[2:]
            # Highlight HIGH-severity anomaly lines
            if content.startswith("⚠ HIGH"):
                body.append(f'<li class="sev-high">{_inline(content)}</li>')
            else:
                body.append(f"<li>{_inline(content)}</li>")
        else:
            _close_list()
            # Plain paragraph
            body.append(f"<p>{_inline(line)}</p>")

    _close_list()
    _close_table()

    return f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>{''.join(body)}</body></html>"


# --- SES send ----------------------------------------------------------------

def _check_identity(ses, address: str, region: str, *, role: str) -> bool:
    """Return True if address is verified, else log a clear instruction."""
    try:
        idn = ses.get_email_identity(EmailIdentity=address)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "NotFoundException":
            LOG.logger.error(
                f"SES {role} '{address}' not registered. Run once: "
                f"aws sesv2 create-email-identity --email-identity {address} "
                f"--region {region} (and click the verification link)."
            )
            return False
        raise
    if not idn.get("VerifiedForSendingStatus"):
        LOG.logger.error(
            f"SES {role} '{address}' is not yet verified — check that inbox "
            f"for an Amazon SES verification link and click it."
        )
        return False
    return True


def send_report_email(
    *,
    sender: str,
    recipients: list[str],
    subject: str,
    markdown_body: str,
    region: str = "us-west-2",
    dry_run: bool = False,
) -> bool:
    """
    Returns True on send (or dry-run print), False if SES rejected or any
    identity is unverified. Raises only on credential / unexpected errors.
    """
    if not recipients:
        LOG.logger.error("send_report_email: no recipients")
        return False

    html = markdown_to_html(markdown_body)
    if dry_run:
        print(f"[dry-run] From: {sender}")
        print(f"[dry-run] To: {', '.join(recipients)}")
        print(f"[dry-run] Subject: {subject}")
        print(f"[dry-run] HTML body: {len(html)} chars; first 800 below")
        print(html[:800])
        return True

    ses = boto3.client("sesv2", region_name=region)

    # Sender must be verified. In sandbox mode each recipient must be verified
    # too — pre-flight all of them so the user gets one clear error per missing
    # link, not a cryptic SES exception on the first send.
    if not _check_identity(ses, sender, region, role="sender"):
        return False
    all_recipients_ok = True
    for r in recipients:
        if not _check_identity(ses, r, region, role="recipient"):
            all_recipients_ok = False
    if not all_recipients_ok:
        return False

    try:
        ses.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": recipients},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": markdown_body, "Charset": "UTF-8"},
                        "Html": {"Data": html, "Charset": "UTF-8"},
                    },
                }
            },
        )
        LOG.logger.info(f"SES: sent '{subject}' to {', '.join(recipients)}")
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        msg = e.response.get("Error", {}).get("Message", "")
        if code in ("MessageRejected", "MailFromDomainNotVerifiedException"):
            LOG.logger.error(f"SES rejected: {code}: {msg}")
            return False
        raise

"""
Email delivery for The PillBox Pharmacy.

Sends transactional emails (order + appointment confirmations) via Gmail SMTP
using an App Password. All configuration comes from environment variables so no
secrets are ever committed:

    MAIL_ENABLED     "true" to actually send (default: off — emails are logged only)
    MAIL_USERNAME    your Gmail address, e.g. pillbox.pharmacy@gmail.com
    MAIL_PASSWORD    a Gmail *App Password* (16 chars, NOT your normal password)
    MAIL_FROM_NAME   display name shown to customers (default: "The PillBox Pharmacy")
    MAIL_REPLY_TO    optional reply-to address (defaults to MAIL_USERNAME)

If email isn't configured, send functions log a message and return False instead
of raising — so checkout and booking never break during a demo.

Setup guide for a Gmail App Password:
    1. Turn on 2-Step Verification: https://myaccount.google.com/security
    2. Create an App Password: https://myaccount.google.com/apppasswords
    3. Put the 16-char password in MAIL_PASSWORD (spaces optional).
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _config():
    return {
        "enabled":   os.environ.get("MAIL_ENABLED", "").lower() == "true",
        "username":  os.environ.get("MAIL_USERNAME", "").strip(),
        "password":  os.environ.get("MAIL_PASSWORD", "").strip(),
        "from_name": os.environ.get("MAIL_FROM_NAME", "The PillBox Pharmacy").strip(),
        "reply_to":  os.environ.get("MAIL_REPLY_TO", "").strip(),
    }


def is_configured():
    """True if email is turned on and credentials are present."""
    c = _config()
    return c["enabled"] and bool(c["username"]) and bool(c["password"])


def _send(to_email, subject, html_body, text_body):
    """
    Low-level send. Returns True on success, False otherwise. Never raises —
    a broken mail server must not take down checkout.
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return False

    c = _config()

    if not is_configured():
        # Not configured — log so the developer can see it would have sent.
        print(f"[email] (disabled) would send to {to_email!r}: {subject!r}")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((c["from_name"], c["username"]))
    msg["To"] = to_email
    msg["Reply-To"] = c["reply_to"] or c["username"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls(context=context)
            server.login(c["username"], c["password"])
            server.send_message(msg)
        print(f"[email] sent to {to_email!r}: {subject!r}")
        return True
    except Exception as e:
        print(f"[email] FAILED to {to_email!r}: {e}")
        return False


# ── Shared HTML shell ─────────────────────────────────────────────────────────

def _shell(title, inner_html):
    """Wrap content in a branded, email-client-safe HTML shell (inline styles)."""
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3e8ff;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3e8ff;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:14px;overflow:hidden;max-width:600px;width:100%;box-shadow:0 8px 30px rgba(90,30,150,0.12);">
        <tr>
          <td style="background:#561F87;padding:22px 28px;">
            <span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:0.3px;">The PillBox Pharmacy</span>
          </td>
        </tr>
        <tr>
          <td style="padding:28px;color:#1a1230;font-size:15px;line-height:1.55;">
            <h1 style="margin:0 0 16px;font-size:22px;color:#561F87;">{title}</h1>
            {inner_html}
          </td>
        </tr>
        <tr>
          <td style="background:#faf9fc;padding:18px 28px;color:#5b5170;font-size:12px;line-height:1.5;border-top:1px solid #e4daf0;">
            The PillBox Pharmacy &mdash; 87 Great George Street &amp; 17 Lewis Street<br>
            This is an automated message. Please do not reply directly to this email.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _money(v):
    return f"JMD {v:,.2f}"


# ── Staff invitation ──────────────────────────────────────────────────────────

def send_staff_invite(member, accept_url):
    """Email a new staff member an 'Accept Invitation' link. Returns True if sent."""
    to = member.get("email", "")
    if not to:
        return False

    role_label = member.get("role", "staff").title()

    inner = f"""
      <p style="margin:0 0 16px;">Hi {member.get("name","there")}, you've been invited to join
      <strong>The PillBox Pharmacy</strong> team as a <strong>{role_label}</strong>.</p>

      <p style="margin:0 0 20px;">Click the button below to accept your invitation and set your own password.
      Until you do, you won't be able to sign in.</p>

      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px;">
        <tr><td style="border-radius:8px;background:#7B2FBE;">
          <a href="{accept_url}" style="display:inline-block;padding:13px 28px;color:#ffffff;
             font-size:15px;font-weight:700;text-decoration:none;border-radius:8px;">Accept Invitation</a>
        </td></tr>
      </table>

      <p style="margin:0 0 6px;color:#5b5170;font-size:13px;">Or paste this link into your browser:</p>
      <p style="margin:0;word-break:break-all;font-size:13px;color:#561F87;">{accept_url}</p>

      <p style="margin:20px 0 0;color:#5b5170;font-size:13px;">If you weren't expecting this invitation, you can safely ignore this email.</p>
    """

    text = (
        f"Hi {member.get('name','there')},\n\n"
        f"You've been invited to join The PillBox Pharmacy team as a {role_label}.\n"
        f"Accept your invitation and set your password here:\n{accept_url}\n\n"
        f"Until you accept, you won't be able to sign in.\n\n"
        f"— The PillBox Pharmacy"
    )

    subject = "You're invited to The PillBox Pharmacy team"
    return _send(to, subject, _shell("Team Invitation ✉️", inner), text)


# ── Order confirmation ────────────────────────────────────────────────────────

def send_order_confirmation(order):
    """Email a customer their order confirmation. Returns True if sent."""
    to = order.get("customer_email", "")
    if not to:
        return False

    is_pickup = order.get("fulfillment") == "pickup"

    # Line items
    rows = ""
    for item in order.get("items", []):
        line_total = item["price"] * item["qty"]
        rows += (
            f'<tr>'
            f'<td style="padding:8px 0;border-bottom:1px solid #eee;">{item["name"]}'
            f' <span style="color:#5b5170;">&times;{item["qty"]}</span></td>'
            f'<td style="padding:8px 0;border-bottom:1px solid #eee;text-align:right;white-space:nowrap;">{_money(line_total)}</td>'
            f'</tr>'
        )

    fulfil_block = (
        f'<p style="margin:0 0 6px;"><strong>Pickup at store</strong></p>'
        f'<p style="margin:0 0 4px;">Show this code at the counter:</p>'
        f'<p style="margin:0;font-size:26px;font-weight:800;letter-spacing:3px;color:#561F87;">{order.get("pickup_code","")}</p>'
        if is_pickup else
        f'<p style="margin:0 0 6px;"><strong>Delivery to</strong></p>'
        f'<p style="margin:0 0 4px;">{order.get("address","")}'
        f'{(", " + order["parish"]) if order.get("parish") else ""}</p>'
        f'<p style="margin:8px 0 0;">Give the courier this code on arrival:</p>'
        f'<p style="margin:0;font-size:26px;font-weight:800;letter-spacing:3px;color:#561F87;">{order.get("pickup_code","")}</p>'
    )

    inner = f"""
      <p style="margin:0 0 16px;">Hi {order.get("customer_name","there")}, thanks for your order!
      We've received it and it's now <strong>{order.get("status","processing")}</strong>.</p>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:#f3e8ff;border-radius:10px;padding:16px;margin:0 0 20px;">
        <tr><td style="padding:16px;">
          <p style="margin:0 0 4px;color:#5b5170;font-size:13px;">ORDER REFERENCE</p>
          <p style="margin:0;font-size:18px;font-weight:700;color:#561F87;">{order.get("id","")}</p>
        </td></tr>
      </table>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 8px;">
        {rows}
      </table>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 20px;font-size:14px;">
        <tr><td style="padding:3px 0;color:#5b5170;">Subtotal</td>
            <td style="padding:3px 0;text-align:right;">{_money(order.get("subtotal",0))}</td></tr>
        <tr><td style="padding:3px 0;color:#5b5170;">Shipping</td>
            <td style="padding:3px 0;text-align:right;">{_money(order.get("shipping",0))}</td></tr>
        <tr><td style="padding:3px 0;color:#5b5170;">Tax (15%)</td>
            <td style="padding:3px 0;text-align:right;">{_money(order.get("tax",0))}</td></tr>
        <tr><td style="padding:8px 0 0;font-weight:700;font-size:16px;color:#561F87;border-top:2px solid #e4daf0;">Total</td>
            <td style="padding:8px 0 0;text-align:right;font-weight:700;font-size:16px;color:#561F87;border-top:2px solid #e4daf0;">{_money(order.get("total",0))}</td></tr>
      </table>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid #e4daf0;border-radius:10px;margin:0 0 8px;">
        <tr><td style="padding:16px;">{fulfil_block}</td></tr>
      </table>
    """

    text = (
        f"Hi {order.get('customer_name','there')},\n\n"
        f"Thanks for your order! Reference: {order.get('id','')}\n"
        f"Status: {order.get('status','processing')}\n\n"
        f"Total: {_money(order.get('total',0))}\n"
        f"{'Pickup' if is_pickup else 'Delivery'} code: {order.get('pickup_code','')}\n\n"
        f"— The PillBox Pharmacy"
    )

    subject = f"Order Confirmed — {order.get('id','')} | The PillBox Pharmacy"
    return _send(to, subject, _shell("Order Confirmed 🎉", inner), text)


# ── Order status update ───────────────────────────────────────────────────────

# Friendly copy per status: (email headline, sentence shown to the customer)
_ORDER_STATUS_COPY = {
    "processing":       ("Order Being Prepared 📦", "We're preparing your order for {fulfil}."),
    "out_for_delivery": ("Out for Delivery 🚚",     "Good news — your order is on its way to you!"),
    "delivered":        ("Order Delivered ✅",       "Your order has been delivered. Thank you for shopping with us!"),
    "ready_for_pickup": ("Ready for Pickup 🏪",      "Your order is ready to collect at our pharmacy."),
    "cancelled":        ("Order Cancelled",          "Your order has been cancelled. If this is a mistake, please contact us."),
    "refunded":         ("Order Refunded",           "Your order has been refunded."),
    "returned":         ("Order Returned",           "Your order has been marked as returned."),
}


def send_order_status(order):
    """
    Email a customer when their order's status changes. Returns True if sent.
    Only sends for statuses we have friendly copy for.
    """
    to = order.get("customer_email", "")
    status = order.get("status", "")
    if not to or status not in _ORDER_STATUS_COPY:
        return False

    headline, sentence = _ORDER_STATUS_COPY[status]
    is_pickup = order.get("fulfillment") == "pickup"
    fulfil = "pickup at the store" if is_pickup else "delivery"
    sentence = sentence.format(fulfil=fulfil)

    # Show the pickup/delivery code again for statuses where it's still needed.
    code_block = ""
    if status in ("out_for_delivery", "ready_for_pickup", "processing") and order.get("pickup_code"):
        label = "Show this code at the counter:" if is_pickup else "Give the courier this code on arrival:"
        code_block = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #e4daf0;border-radius:10px;margin:16px 0 0;">'
            f'<tr><td style="padding:16px;"><p style="margin:0 0 6px;">{label}</p>'
            f'<p style="margin:0;font-size:26px;font-weight:800;letter-spacing:3px;color:#561F87;">'
            f'{order["pickup_code"]}</p></td></tr></table>'
        )

    inner = f"""
      <p style="margin:0 0 16px;">Hi {order.get("customer_name","there")}, there's an update on your order
      <strong>{order.get("id","")}</strong>.</p>

      <p style="margin:0 0 16px;">{sentence}</p>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:#f3e8ff;border-radius:10px;margin:0 0 8px;">
        <tr><td style="padding:16px;">
          <p style="margin:0 0 4px;color:#5b5170;font-size:13px;">ORDER TOTAL</p>
          <p style="margin:0;font-size:18px;font-weight:700;color:#561F87;">{_money(order.get("total",0))}</p>
        </td></tr>
      </table>
      {code_block}
    """

    text = (
        f"Hi {order.get('customer_name','there')},\n\n"
        f"Update on order {order.get('id','')}: {status.replace('_',' ').title()}\n"
        f"{sentence}\n"
        f"Total: {_money(order.get('total',0))}\n\n"
        f"— The PillBox Pharmacy"
    )

    subject = f"{headline.rsplit(' ',1)[0]} — {order.get('id','')} | The PillBox Pharmacy"
    return _send(to, subject, _shell(headline, inner), text)


# ── Appointment confirmation ──────────────────────────────────────────────────

def send_appointment_confirmation(appt):
    """Email a customer their appointment booking confirmation. Returns True if sent."""
    to = appt.get("email", "")
    if not to:
        return False

    inner = f"""
      <p style="margin:0 0 16px;">Hi {appt.get("name","there")}, your appointment request has been received.
      We'll confirm shortly. Please keep your reference below.</p>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:#f3e8ff;border-radius:10px;margin:0 0 20px;">
        <tr><td style="padding:16px;">
          <p style="margin:0 0 4px;color:#5b5170;font-size:13px;">REFERENCE</p>
          <p style="margin:0;font-size:18px;font-weight:700;color:#561F87;">{appt.get("id","")}</p>
        </td></tr>
      </table>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;">
        <tr><td style="padding:5px 0;color:#5b5170;width:120px;">Date</td>
            <td style="padding:5px 0;font-weight:600;">{appt.get("date","")}</td></tr>
        <tr><td style="padding:5px 0;color:#5b5170;">Time</td>
            <td style="padding:5px 0;font-weight:600;">{appt.get("time","")}</td></tr>
        <tr><td style="padding:5px 0;color:#5b5170;">Location</td>
            <td style="padding:5px 0;font-weight:600;">{appt.get("branch_label","")}</td></tr>
        <tr><td style="padding:5px 0;color:#5b5170;">Reason</td>
            <td style="padding:5px 0;font-weight:600;">{appt.get("reason","")}</td></tr>
      </table>
    """

    text = (
        f"Hi {appt.get('name','there')},\n\n"
        f"Your appointment request is received. Reference: {appt.get('id','')}\n"
        f"Date: {appt.get('date','')} at {appt.get('time','')}\n"
        f"Location: {appt.get('branch_label','')}\n"
        f"Reason: {appt.get('reason','')}\n\n"
        f"— The PillBox Pharmacy"
    )

    subject = f"Appointment Received — {appt.get('id','')} | The PillBox Pharmacy"
    return _send(to, subject, _shell("Appointment Received ✅", inner), text)


def send_appointment_status(appt):
    """Email a customer when an admin updates their appointment status. Returns True if sent."""
    to = appt.get("email", "")
    if not to:
        return False

    status = appt.get("status", "")
    headline = {
        "confirmed": "Appointment Confirmed ✅",
        "completed": "Appointment Completed",
        "missed":    "Appointment Missed",
        "cancelled": "Appointment Cancelled",
    }.get(status, "Appointment Update")

    feedback = appt.get("feedback", "")
    feedback_block = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#faf9fc;border-radius:10px;margin:16px 0 0;">'
        f'<tr><td style="padding:16px;"><p style="margin:0 0 4px;color:#5b5170;font-size:13px;">A NOTE FROM OUR TEAM</p>'
        f'<p style="margin:0;">{feedback}</p></td></tr></table>'
        if feedback else ""
    )

    inner = f"""
      <p style="margin:0 0 16px;">Hi {appt.get("name","there")}, there's an update on your appointment
      <strong>{appt.get("id","")}</strong>.</p>

      <p style="margin:0 0 16px;">Status: <strong style="color:#561F87;">{status.title()}</strong></p>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;">
        <tr><td style="padding:5px 0;color:#5b5170;width:120px;">Date</td>
            <td style="padding:5px 0;font-weight:600;">{appt.get("date","")}</td></tr>
        <tr><td style="padding:5px 0;color:#5b5170;">Time</td>
            <td style="padding:5px 0;font-weight:600;">{appt.get("time","")}</td></tr>
        <tr><td style="padding:5px 0;color:#5b5170;">Location</td>
            <td style="padding:5px 0;font-weight:600;">{appt.get("branch_label","")}</td></tr>
      </table>
      {feedback_block}
    """

    text = (
        f"Hi {appt.get('name','there')},\n\n"
        f"Update on appointment {appt.get('id','')}: {status.title()}\n"
        f"Date: {appt.get('date','')} at {appt.get('time','')}\n"
        f"{('Note: ' + feedback) if feedback else ''}\n\n"
        f"— The PillBox Pharmacy"
    )

    subject = f"Appointment {status.title()} — {appt.get('id','')} | The PillBox Pharmacy"
    return _send(to, subject, _shell(headline, inner), text)
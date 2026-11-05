"""Outgoing email over SMTP — invites, order alerts and confirmations, and
contact-form notifications.

Works with any SMTP provider (Resend, Brevo, Postmark, Google Workspace,
Microsoft 365...). Configure with environment variables:

    SMTP_HOST       e.g. smtp.resend.com
    SMTP_PORT       587 (STARTTLS, default) or 465 (SSL)
    SMTP_USERNAME   e.g. "resend" for Resend
    SMTP_PASSWORD   the provider's password / API key
    MAIL_FROM       e.g. TriClean Supply <triclean@sheilmarketing.com>
    MAIL_REPLY_TO   optional; where customer replies go (defaults to COMPANY_EMAIL)
    NOTIFY_EMAILS   optional; comma-separated staff alert recipients
                    (defaults to every active staff login)

With SMTP_HOST or MAIL_FROM unset, nothing is sent and send() returns False,
so the site works exactly as before (staff copy invite links by hand).
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from flask import current_app, url_for

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_FROM"))


def _reply_to() -> str | None:
    return os.environ.get("MAIL_REPLY_TO") or current_app.config.get("COMPANY_EMAIL") or None


def _deliver(msg: EmailMessage) -> bool:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USERNAME"), os.environ.get("SMTP_PASSWORD")
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls(context=ssl.create_default_context())
        with server:
            if user:
                server.login(user, password or "")
            server.send_message(msg)
        return True
    except Exception:
        log.exception("Email to %s failed (%s)", msg["To"], msg["Subject"])
        return False


def send(to: str | list[str], subject: str, body: str, reply_to: str | None = None,
         background: bool = False) -> bool:
    """Sends a plain-text email. Returns True if it was handed to the mail
    server (or queued, with background=True). Never raises."""
    recipients = [to] if isinstance(to, str) else [t for t in to if t]
    if not configured() or not recipients:
        return False
    msg = EmailMessage()
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=os.environ["MAIL_FROM"].rsplit("@", 1)[-1].strip("> "))
    if reply_to or _reply_to():
        msg["Reply-To"] = reply_to or _reply_to()
    msg.set_content(body)
    if background:
        # For notifications that shouldn't make a customer wait on checkout.
        threading.Thread(target=_deliver, args=(msg,), daemon=True).start()
        return True
    return _deliver(msg)


def _signature() -> str:
    cfg = current_app.config
    return f"\n\n— {cfg['COMPANY_NAME']}\n{url_for('public.home', _external=True)}"


def _staff_recipients() -> list[str]:
    from models import User
    listed = [e.strip() for e in os.environ.get("NOTIFY_EMAILS", "").split(",") if e.strip()]
    return listed or [u.email for u in User.query.filter_by(role="admin", active=True).all()]


# --- Specific emails ------------------------------------------------------------

def send_invite(user, link: str, reset: bool = False) -> bool:
    company = current_app.config["COMPANY_NAME"]
    who = f"for {user.customer.name} " if user.customer else ""
    greeting = f"Hi {user.name.split()[0]}," if user.name else "Hi,"
    if reset:
        subject = f"Reset your {company} password"
        intro = f"Here's a link to set a new password for your {company} login {who}({user.email})."
    elif user.is_admin:
        subject = f"Your {company} staff login"
        intro = f"You've been given a staff login to the {company} website backend ({user.email})."
    else:
        subject = f"Your {company} ordering account"
        intro = (f"Your {company} ordering account {who}is ready. You'll see your account's "
                 f"pricing and can order online any time. Your login is {user.email}.")
    body = (f"{greeting}\n\n{intro}\n\nSet your password here (the link works once and expires in 7 days):\n"
            f"{link}\n\nAfter that, log in at {url_for('public.login', _external=True)}"
            f"{_signature()}")
    return send(user.email, subject, body)


def _order_lines(order) -> str:
    lines = [f"  {i.quantity} x {i.name} ({i.sku}) @ ${i.unit_price:,.2f} = ${i.line_total:,.2f}" for i in order.items]
    totals = [f"  Subtotal: ${order.subtotal:,.2f}"]
    if order.discount:
        totals.append(f"  Discount: -${order.discount:,.2f}")
    if order.tax:
        totals.append(f"  Tax: ${order.tax:,.2f}")
    totals.append(f"  Total: ${order.total:,.2f}")
    return "\n".join(lines + [""] + totals)


def notify_new_order(order) -> bool:
    body = (f"New order {order.number} from {order.customer.name}"
            f"{' (PO ' + order.po_number + ')' if order.po_number else ''}, placed by "
            f"{order.placed_by.email if order.placed_by else 'unknown'}.\n\n{_order_lines(order)}\n\n"
            f"Deliver to:\n{order.shipping_address}\n"
            f"{chr(10) + 'Notes: ' + order.notes + chr(10) if order.notes else ''}\n"
            f"Open it: {url_for('admin.order_detail', order_id=order.id, _external=True)}")
    return send(_staff_recipients(), f"New order {order.number} — {order.customer.name} — ${order.total:,.2f}",
                body, reply_to=order.placed_by.email if order.placed_by else None, background=True)


def send_order_confirmation(order) -> bool:
    if not order.placed_by:
        return False
    body = (f"Thanks for your order. We've received {order.number}"
            f"{' (your PO ' + order.po_number + ')' if order.po_number else ''} and will confirm it shortly.\n\n"
            f"{_order_lines(order)}\n\nDeliver to:\n{order.shipping_address}\n\n"
            f"View it any time: {url_for('portal.order_detail', order_id=order.id, _external=True)}"
            f"{_signature()}")
    return send(order.placed_by.email, f"Order {order.number} received", body, background=True)


def send_shipped_notice(order) -> bool:
    if not order.placed_by:
        return False
    body = (f"Your order {order.number}{' (PO ' + order.po_number + ')' if order.po_number else ''} has shipped.\n\n"
            f"{_order_lines(order)}\n\n"
            f"Invoice: {url_for('portal.invoice', order_id=order.id, _external=True)}{_signature()}")
    return send(order.placed_by.email, f"Order {order.number} has shipped", body, background=True)


def notify_contact_message(msg) -> bool:
    body = (f"New message from the website's Contact us page.\n\n"
            f"From: {msg.name}{' — ' + msg.organization if msg.organization else ''}\n"
            f"Email: {msg.email}\n{'Phone: ' + msg.phone + chr(10) if msg.phone else ''}"
            f"{'Industry: ' + msg.industry + chr(10) if msg.industry else ''}\n{msg.message}\n\n"
            f"Reply to this email to answer them, or open: {url_for('admin.messages_list', _external=True)}")
    return send(_staff_recipients(), f"Website message from {msg.name}"
                f"{' (' + msg.organization + ')' if msg.organization else ''}",
                body, reply_to=msg.email, background=True)

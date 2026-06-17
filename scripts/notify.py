from __future__ import annotations
import smtplib
from email.message import EmailMessage
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO, TEMPLATES_DIR
from .utils import log


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_email(template: str, ctx: dict) -> str:
    return _env.get_template(template).render(**ctx)


def send_email(subject: str, html: str, text_fallback: str = "") -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = MAIL_TO
    msg.set_content(text_fallback or "請以 HTML 模式查看本信。")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)
    log.info(f"Email sent to {MAIL_TO}: {subject}")

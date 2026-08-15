"""Outbound mail: Jinja2 templates per locale, delivered over aiosmtplib.

SMTP lives in the settings table rather than env, so it is read fresh on every send and an
admin can fix a typo'd host without a restart. Nothing here retries — the queue is not
involved in mail; callers decide whether a failure is fatal (it never is for invites, which
always return a shareable URL).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError, MailNotConfigured
from app.services.settings import SettingsService, SmtpConfig
from app.utils.time import utcnow

log = logging.getLogger(__name__)

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "i18n" / "emails"
FALLBACK_LOCALE = "en"
SEND_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class RenderedMail:
    subject: str
    text: str
    html: str


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        autoescape=select_autoescape(enabled_extensions=("html",), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=False,
    )


def available_locales() -> list[str]:
    if not TEMPLATE_ROOT.is_dir():
        return []
    return sorted(p.name for p in TEMPLATE_ROOT.iterdir() if p.is_dir())


def _locale_candidates(locale: str | None) -> list[str]:
    """`pt-BR` → try `pt-BR`, then `pt`, then `en`. Region variants share a base translation."""
    candidates: list[str] = []
    if locale:
        cleaned = locale.strip().replace("_", "-")
        if cleaned:
            candidates.append(cleaned)
            base = cleaned.split("-", 1)[0]
            if base != cleaned:
                candidates.append(base)
    if FALLBACK_LOCALE not in candidates:
        candidates.append(FALLBACK_LOCALE)
    return candidates


def _render_sync(template: str, locale: str | None, context: dict[str, Any]) -> RenderedMail:
    env = _env()
    for candidate in _locale_candidates(locale):
        try:
            subject = env.get_template(f"{candidate}/{template}.subject.txt")
            text = env.get_template(f"{candidate}/{template}.txt")
            html = env.get_template(f"{candidate}/{template}.html")
        except TemplateNotFound:
            continue
        return RenderedMail(
            subject=" ".join(subject.render(**context).split()),
            text=text.render(**context).strip() + "\n",
            html=html.render(**context),
        )
    raise AppError(
        "MAIL_TEMPLATE_MISSING",
        f"No mail template '{template}' for locale '{locale}' or '{FALLBACK_LOCALE}'",
        500,
    )


async def render_mail(template: str, locale: str | None, context: dict[str, Any]) -> RenderedMail:
    """Rendering touches the filesystem on first use; keep it off the event loop."""
    return await asyncio.to_thread(_render_sync, template, locale, dict(context))


async def base_context(session: AsyncSession) -> dict[str, Any]:
    """Values every template may reference, whatever the caller passed."""
    settings = get_settings()
    svc = SettingsService(session)
    public_url = settings.public_url.rstrip("/")
    return {
        "site_name": await svc.get_str("site.name", "dramatarr"),
        "public_url": public_url,
        "login_url": f"{public_url}/login",
        "year": utcnow().year,
    }


async def is_mail_configured(session: AsyncSession) -> bool:
    return (await SettingsService(session).smtp()).configured


def _msgid_domain(from_email: str) -> str | None:
    """`make_msgid` otherwise stamps the container's hostname, which leaks nothing useful and
    looks forged next to the From address."""
    _, _, domain = from_email.partition("@")
    return domain or None


def _build_message(smtp: SmtpConfig, to: str, mail: RenderedMail) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = mail.subject
    message["From"] = formataddr((smtp.from_name or "", smtp.from_email))
    message["To"] = to
    # `Date` is mandatory (RFC 5322 §3.6) and neither aiosmtplib nor EmailMessage adds it;
    # without it — and without a Message-ID — spam filters score the mail as machine-forged
    # and some MTAs refuse it outright.
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=_msgid_domain(smtp.from_email))
    message.set_content(mail.text)
    message.add_alternative(mail.html, subtype="html")
    return message


async def send_mail(
    session: AsyncSession,
    to: str,
    template: str,
    locale: str | None,
    context: dict[str, Any],
) -> None:
    """Render `template` in `locale` and deliver it.

    Raises `MailNotConfigured` when SMTP has not been set up, and `MAIL_SEND_FAILED` when the
    server rejects the message — both are recoverable states the UI surfaces, not bugs.
    """
    smtp = await SettingsService(session).smtp()
    if not smtp.configured:
        raise MailNotConfigured()

    merged = {**await base_context(session), "locale": locale, **context}
    mail = await render_mail(template, locale, merged)
    try:
        message = _build_message(smtp, to, mail)
    except ValueError as exc:
        # A CR/LF in the address (header injection) or an unencodable header. Surfaced as a
        # coded error so callers see a 4xx they can explain, not an opaque 500.
        raise AppError("MAIL_INVALID_RECIPIENT", f"Cannot address mail to {to!r}: {exc}") from exc

    # aiosmtplib's `use_tls` means implicit TLS (465); `start_tls` means STARTTLS (587). The
    # two are mutually exclusive, and an admin who turned both off gets a plaintext session
    # rather than aiosmtplib's opportunistic upgrade.
    start_tls = smtp.use_tls and not smtp.use_ssl

    try:
        await aiosmtplib.send(
            message,
            hostname=smtp.host,
            port=smtp.port,
            username=smtp.username or None,
            password=smtp.password or None,
            use_tls=smtp.use_ssl,
            start_tls=start_tls if not smtp.use_ssl else None,
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except (aiosmtplib.SMTPException, OSError) as exc:
        log.warning("Mail send failed (template=%s to=%s): %s", template, to, exc)
        raise AppError("MAIL_SEND_FAILED", f"Could not send mail: {exc}", 502) from exc

    log.info("Sent '%s' mail to %s", template, to)


__all__ = [
    "FALLBACK_LOCALE",
    "TEMPLATE_ROOT",
    "RenderedMail",
    "available_locales",
    "base_context",
    "is_mail_configured",
    "render_mail",
    "send_mail",
]

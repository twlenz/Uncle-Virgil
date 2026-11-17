"""Notification channels. Each channel is a function (title, body) -> None that
raises on failure. Plugins can add channels to `Notifier.channels`.

    desktop  a toast on this computer; nothing leaves the machine
    email    through your own SMTP account
    ntfy     push to your phone through an ntfy server (ntfy.sh, or self-hosted)
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Callable

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings, get_secret: Callable[[str], str]):
        self.settings = settings
        self.get_secret = get_secret
        self.channels: dict[str, Callable[[str, str], None]] = {"desktop": self._desktop, "email": self._email, "ntfy": self._ntfy}

    def send(self, title: str, body: str, channels: list[str]) -> dict[str, str]:
        """Send on each channel. Returns {channel: "ok" or the error message}."""
        results = {}
        for name in channels:
            try:
                if name not in self.channels:
                    raise ValueError("unknown channel")
                self.channels[name](title, body)
                results[name] = "ok"
            except Exception as e:
                log.warning("notification via %s failed: %s", name, e)
                results[name] = f"{type(e).__name__}: {e}"
        return results

    def _desktop(self, title: str, body: str) -> None:
        from desktop_notifier import DesktopNotifier

        asyncio.run(DesktopNotifier(app_name="Uncle Virgil").send(title=title, message=body))

    def _email(self, title: str, body: str) -> None:
        n = self.settings.notify
        if not (n.smtp_host and n.email_to):
            raise ValueError("Email is not set up (SMTP server and recipient are required)")
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, n.email_from or n.smtp_user or n.email_to, n.email_to
        msg.set_content(body)
        smtp_class = smtplib.SMTP_SSL if n.smtp_port == 465 else smtplib.SMTP
        with smtp_class(n.smtp_host, n.smtp_port, timeout=20) as smtp:
            if n.smtp_port != 465:
                smtp.starttls()
            if n.smtp_user:
                smtp.login(n.smtp_user, self.get_secret("smtp_password"))
            smtp.send_message(msg)

    def _ntfy(self, title: str, body: str) -> None:
        n = self.settings.notify
        if not n.ntfy_topic:
            raise ValueError("ntfy is not set up (a topic is required)")
        token = self.get_secret("ntfy_token")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = httpx.post(n.ntfy_server.rstrip("/"), json={"topic": n.ntfy_topic, "title": title, "message": body}, headers=headers, timeout=20)
        r.raise_for_status()

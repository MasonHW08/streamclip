from typing import Protocol

import httpx


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, html_body: str) -> str: ...


class ResendEmailSender:
    def __init__(self, api_key: str, from_address: str):
        self._api_key = api_key
        self._from_address = from_address

    def send(self, *, to: str, subject: str, html_body: str) -> str:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"from": self._from_address, "to": [to], "subject": subject, "html": html_body},
            timeout=10.0,
        )
        response.raise_for_status()
        return str(response.json()["id"])


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, html_body: str) -> str:
        message_id = f"fake-{len(self.sent) + 1}"
        self.sent.append({"to": to, "subject": subject, "html_body": html_body, "id": message_id})
        return message_id

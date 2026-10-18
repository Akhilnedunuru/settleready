"""Tool interfaces and implementations."""
from .email_client import EmailClient, MockEmailClient, EmailRecord

__all__ = ["EmailClient", "MockEmailClient", "EmailRecord"]

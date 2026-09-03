"""Encryption at rest for the few values that are genuinely secrets.

Introduced by PR-PUBLISH-01 because a YouTube refresh token is the first thing this system
stores that is both long-lived and directly usable by anyone who reads the database. Every
other secret it handles is either short-lived (an OTP), a hash (a password), or supplied by
configuration and never persisted (the JWT secret).

**Not homemade cryptography.** This is a thin wrapper over ``cryptography.fernet``, which is
AES-128-CBC with an HMAC-SHA256 authentication tag and a timestamp, in a versioned token
format. The wrapper exists to give the application one place to construct a key, one error
type, and one obvious name to grep for — not to invent a scheme.

**The key comes from configuration only.** ``PUBLISH_SECRET_KEY``, never from the database:
a key stored beside the ciphertext it protects protects nothing. Without it, connecting a
publish target fails loudly at the boundary instead of storing a token in the clear.

**Rotation is not implemented.** Fernet supports MultiFernet for key rotation; there is one
key today. Rotating means re-encrypting every stored token, which needs an operator command
this PR does not have. Documented as debt rather than half-built.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken

from app.core.settings import settings


class SecretUnavailableError(RuntimeError):
    """No usable encryption key is configured.

    Deliberately distinct from a decryption failure: "this deployment cannot hold secrets"
    and "this particular ciphertext is not readable" need different operator responses.
    """


class SecretDecryptionError(RuntimeError):
    """The ciphertext could not be decrypted with the configured key.

    Either the key changed or the value was corrupted. Never carries the ciphertext or any
    part of the plaintext in its message.
    """


class SecretBox:
    """Encrypts and decrypts secrets with the configured key."""

    def __init__(self, key: str | None = None) -> None:
        # Held as None rather than resolved here: the module-level singleton is built at
        # import time, and capturing the setting then would freeze whatever the environment
        # looked like before configuration was complete.
        self._key = key

    @property
    def available(self) -> bool:
        """Whether this deployment can store secrets at all.

        Checked before starting an OAuth flow, so an operator learns the key is missing
        before authorizing at Google rather than after — at which point the authorization
        code is spent and the consent has to be repeated.
        """
        try:
            self._fernet()
        except SecretUnavailableError:
            return False
        return True

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            raise ValueError("refusing to encrypt None")
        return self._fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            # The message deliberately says nothing about the value: an exception string
            # travels into logs and API errors, and this one is raised while handling a
            # credential.
            raise SecretDecryptionError(
                "stored secret could not be decrypted with the configured key"
            ) from exc

    def _fernet(self) -> Fernet:
        configured = self._key if self._key is not None else settings.publish_secret_key
        raw = (configured or "").strip()
        if not raw:
            raise SecretUnavailableError(
                "PUBLISH_SECRET_KEY is not set; this deployment cannot store publish "
                "credentials. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        try:
            return Fernet(raw.encode("ascii"))
        except (ValueError, TypeError, base64.binascii.Error) as exc:
            raise SecretUnavailableError(
                "PUBLISH_SECRET_KEY is not a valid Fernet key (expected 32 url-safe "
                "base64-encoded bytes)"
            ) from exc


secret_box = SecretBox()

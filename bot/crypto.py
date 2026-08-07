from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptionError(RuntimeError):
    """Raised when encrypted credentials cannot be safely processed."""


class TokenCipher:
    def __init__(self, key: str) -> None:
        if not key.strip():
            raise TokenEncryptionError("THREADS_TOKEN_ENCRYPTION_KEY is not configured")
        try:
            self._fernet = Fernet(key.strip().encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise TokenEncryptionError(
                "THREADS_TOKEN_ENCRYPTION_KEY must be a valid Fernet key"
            ) from exc

    def encrypt(self, value: str) -> bytes:
        if not value:
            raise TokenEncryptionError("Refusing to encrypt an empty token")
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise TokenEncryptionError("Stored Threads token cannot be decrypted") from exc

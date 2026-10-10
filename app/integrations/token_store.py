from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.errors import AppError


class TokenStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(Fernet.generate_key())
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except FileExistsError:
            pass
        self.cipher = Fernet(path.read_bytes())

    def encrypt(self, token: str) -> str:
        return self.cipher.encrypt(token.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self.cipher.decrypt(token.encode()).decode()
        except InvalidToken:
            raise AppError(
                "token_unreadable", "Stored Gmail token cannot be decrypted. Reconnect.", 401
            ) from None

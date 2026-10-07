"""Per-account local storage for Naver credentials, with legacy vault migration."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from core.storage import account_data_dir

SERVICE_NAME = "AutoSNS Naver Account"
_ALLOWED_BACKEND_MODULES = (
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
)


class CredentialVaultUnavailable(RuntimeError):
    """The operating system does not provide a supported secure credential vault."""


def _keyring_module():
    try:
        import keyring
    except ImportError as exc:
        raise CredentialVaultUnavailable("안전한 계정 저장소를 사용할 수 없어요. keyring 패키지를 설치해 주세요.") from exc
    backend = keyring.get_keyring()
    module = type(backend).__module__
    if getattr(backend, "priority", 0) <= 0 or not module.startswith(_ALLOWED_BACKEND_MODULES):
        raise CredentialVaultUnavailable("운영체제의 안전한 자격 증명 저장소를 사용할 수 없어요.")
    return keyring


def save_naver_credentials(account_id: str, username: str, password: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", account_id):
        raise ValueError("블로그 계정 ID를 확인해 주세요.")
    if not username or not password:
        raise ValueError("네이버 아이디와 비밀번호를 확인해 주세요.")
    path = account_data_dir(account_id) / ".naver_credentials.json"
    value = json.dumps({"username": username[:128], "password": password[:256]}, ensure_ascii=False)
    _write_private_json(path, value)
    # Keep the OS vault in sync for compatibility with older app versions.
    # The per-account local file remains the source of truth for this app.
    try:
        _keyring_module().set_password(SERVICE_NAME, account_id, value)
    except Exception:
        pass


def load_naver_credentials(account_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", account_id):
        return None
    path = account_data_dir(account_id) / ".naver_credentials.json"
    value = None
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8")
    except OSError:
        value = None
    if not value:
        try:
            value = _keyring_module().get_password(SERVICE_NAME, account_id)
        except CredentialVaultUnavailable:
            value = None
    if not value:
        return None
    try:
        credentials = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(credentials, dict):
        return None
    username = str(credentials.get("username", ""))[:128]
    password = str(credentials.get("password", ""))[:256]
    if not username or not password:
        return None
    # Migrate credentials saved by older versions from the OS vault to the
    # account-local file so publishing can use the same predictable path.
    if not path.exists():
        try:
            _write_private_json(path, json.dumps({"username": username, "password": password}, ensure_ascii=False))
        except OSError:
            pass
    return {"username": username, "password": password}


def delete_naver_credentials(account_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", account_id):
        return
    path = account_data_dir(account_id) / ".naver_credentials.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        keyring = _keyring_module()
    except CredentialVaultUnavailable:
        return
    try:
        keyring.delete_password(SERVICE_NAME, account_id)
    except keyring.errors.PasswordDeleteError:
        pass


def _write_private_json(path: Path, value: str) -> None:
    """Atomically write a user-only credential file where POSIX supports it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise

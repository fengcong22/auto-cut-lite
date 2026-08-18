from __future__ import annotations

import re
from pathlib import PurePosixPath

_SENSITIVE_STEMS = frozenset(
    {
        "auth",
        "authorization",
        "credential",
        "credentials",
        "secret",
        "secrets",
        "session",
        "token",
        "tokens",
    }
)
_SENSITIVE_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".docker", ".gnupg", ".kube"}
    | _SENSITIVE_STEMS
)
_SENSITIVE_CONFIG_SUFFIXES = frozenset(
    {
        "",
        ".cfg",
        ".conf",
        ".db",
        ".ini",
        ".json",
        ".sqlite",
        ".sqlite3",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_CREDENTIAL_DOTFILES = frozenset({".git-credentials", ".netrc", ".npmrc", ".pypirc"})
_PRIVATE_KEY_FILENAMES = frozenset({"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"})
_PRIVATE_KEY_SUFFIXES = frozenset({".jks", ".key", ".keystore", ".p12", ".p8", ".pem", ".pfx"})


def is_sensitive_draft_artifact(relative_path: str, *, is_directory: bool) -> bool:
    path = PurePosixPath(str(relative_path).removeprefix("Draft/"))
    lowered_parts = tuple(part.casefold() for part in path.parts)
    if any(part in _SENSITIVE_DIRECTORIES for part in lowered_parts):
        return True
    if is_directory or not path.parts:
        return False
    name = path.name.casefold()
    if (
        name == ".env"
        or name.startswith(".env.")
        or name in _CREDENTIAL_DOTFILES
        or name in _PRIVATE_KEY_FILENAMES
    ):
        return True
    suffix = PurePosixPath(name).suffix
    stem = PurePosixPath(name).stem
    stem_tokens = frozenset(re.findall(r"[a-z0-9]+", stem))
    return suffix in _PRIVATE_KEY_SUFFIXES or (
        bool(stem_tokens & _SENSITIVE_STEMS) and suffix in _SENSITIVE_CONFIG_SUFFIXES
    )

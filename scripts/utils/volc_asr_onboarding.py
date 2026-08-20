from __future__ import annotations

import getpass
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from audio_sound.config import load_env_file
from audio_sound.volc_asr import DEFAULT_RESOURCE_ID

APP_ID_KEY = "VOLC_ASR_APP_ID"
ACCESS_TOKEN_KEY = "VOLC_ASR_ACCESS_TOKEN"
API_KEY_KEY = "VOLC_ASR_API_KEY"
RESOURCE_ID_KEY = "VOLC_ASR_RESOURCE_ID"
VOLC_CONFIG_KEYS = (APP_ID_KEY, ACCESS_TOKEN_KEY, API_KEY_KEY, RESOURCE_ID_KEY)
AUTHENTICATION_OPTIONS = {
    "legacy_app_id_access_token": [APP_ID_KEY, ACCESS_TOKEN_KEY],
    "new_console_api_key": [API_KEY_KEY],
}

InputFunction = Callable[[str], str]


def _resolved_value(file_values: Mapping[str, str], environ: Mapping[str, str], key: str) -> str:
    return str(environ.get(key) or file_values.get(key) or "").strip()


def inspect_volc_config(
    repo_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    root = Path(repo_root).resolve()
    values = load_env_file(root / ".env")
    environment = os.environ if environ is None else environ
    app_id = _resolved_value(values, environment, APP_ID_KEY)
    access_token = _resolved_value(values, environment, ACCESS_TOKEN_KEY)
    api_key = _resolved_value(values, environment, API_KEY_KEY)
    resource_id = _resolved_value(values, environment, RESOURCE_ID_KEY) or DEFAULT_RESOURCE_ID
    legacy_present = bool(app_id or access_token)
    if api_key and legacy_present:
        configured = False
        authentication_mode = "conflict"
        code = "conflicting_authentication_modes"
        missing_options: dict[str, list[str]] = {}
    elif api_key:
        configured = True
        authentication_mode = "new_console_api_key"
        code = "configuration_present_unverified"
        missing_options = {}
    elif app_id and access_token:
        configured = True
        authentication_mode = "legacy_app_id_access_token"
        code = "configuration_present_unverified"
        missing_options = {}
    else:
        configured = False
        authentication_mode = None
        code = "requires_user_authorization"
        missing_options = {
            "legacy_app_id_access_token": [
                key
                for key, value in ((APP_ID_KEY, app_id), (ACCESS_TOKEN_KEY, access_token))
                if not value
            ],
            "new_console_api_key": [API_KEY_KEY],
        }
    return {
        "status": "pending",
        "code": code,
        "configured": configured,
        "configuration_fields": list(VOLC_CONFIG_KEYS),
        "authentication_mode": authentication_mode,
        "authentication_options": AUTHENTICATION_OPTIONS,
        "missing_authentication_options": missing_options,
        "resource_id": resource_id,
        "service_probe": "not_run",
    }


def build_volc_guide() -> dict[str, object]:
    return {
        "status": "pending",
        "authorization_is_target_local": True,
        "credentials_bundled": False,
        "new_console_api_key_supported": True,
        "supported_authentication_modes": {
            "legacy_app_id_access_token": {
                "fields": [APP_ID_KEY, ACCESS_TOKEN_KEY],
                "headers": ["X-Api-App-Key", "X-Api-Access-Key"],
            },
            "new_console_api_key": {
                "fields": [API_KEY_KEY],
                "headers": ["X-Api-Key"],
            },
        },
        "credential_mode_boundary": (
            "Choose either the new-console API key or the legacy App ID and Access Token, not "
            "both. The adapter never mixes authentication headers."
        ),
        "official_resources": {
            "legacy_console": "https://console.volcengine.com/speech/app",
            "legacy_quickstart": "https://www.volcengine.com/docs/6561/163043",
            "credential_faq": "https://www.volcengine.com/docs/6561/196768",
            "http_api_reference": "https://www.volcengine.com/docs/6561/1354868",
            "new_api_key_console": (
                "https://console.volcengine.com/speech/new/setting/apikeys?projectName=default"
            ),
        },
        "resource_ids": {
            "recording_asr_model_1": "volc.bigasr.auc",
            "recording_asr_model_2": "volc.seedasr.auc",
        },
        "default_resource_requires_entitlement": True,
        "configuration_fields": list(VOLC_CONFIG_KEYS),
        "field_requirements": {
            APP_ID_KEY: "Volcengine speech application ID for the target user's account.",
            ACCESS_TOKEN_KEY: "Volcengine speech access token; entered with masked input.",
            API_KEY_KEY: "Volcengine new-console API key; entered with masked input.",
            RESOURCE_ID_KEY: ("Authorized ASR resource ID. Press Enter to use volc.bigasr.auc."),
        },
        "commands": [
            r".\.venv\Scripts\python.exe scripts\auto_cut_first_run.py volc-config",
            r".\.venv\Scripts\python.exe scripts\auto_cut_first_run.py volc-status --json",
        ],
        "boundary": (
            "Authorization and service charges belong to the target user's Volcengine account. "
            "Configuration presence is not proof that the selected recording-ASR model is "
            "enabled or that a real ASR request succeeded."
        ),
    }


def _validate_single_line(value: str, field_name: str) -> str:
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError(f"{field_name} must be a single line")
    return value.strip()


def _updated_env_text(existing_text: str, updates: Mapping[str, str]) -> str:
    lines = existing_text.splitlines()
    output: list[str] = []
    written: set[str] = set()
    assignment = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in lines:
        match = assignment.match(line)
        key = match.group(1) if match else None
        if key not in updates:
            output.append(line)
            continue
        if key not in written:
            output.append(f"{key}={updates[key]}")
            written.add(key)
    if output and output[-1] != "":
        output.append("")
    for key in VOLC_CONFIG_KEYS:
        if key not in written:
            output.append(f"{key}={updates[key]}")
    return "\n".join(output).rstrip("\n") + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".env.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def configure_volc(
    repo_root: str | Path,
    *,
    input_fn: InputFunction = input,
    secret_input_fn: InputFunction = getpass.getpass,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    root = Path(repo_root).resolve()
    env_path = root / ".env"
    existing_text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    file_values = load_env_file(env_path)
    environment = os.environ if environ is None else environ

    existing_app_id = _resolved_value(file_values, environment, APP_ID_KEY)
    existing_token = _resolved_value(file_values, environment, ACCESS_TOKEN_KEY)
    existing_api_key = _resolved_value(file_values, environment, API_KEY_KEY)
    existing_resource = (
        _resolved_value(file_values, environment, RESOURCE_ID_KEY) or DEFAULT_RESOURCE_ID
    )

    default_mode = (
        "api_key" if existing_api_key and not (existing_app_id or existing_token) else "legacy"
    )
    mode = _validate_single_line(
        input_fn(f"Volcengine credential mode [legacy/api_key] [{default_mode}]: "),
        "credential_mode",
    )
    mode = (mode or default_mode).casefold().replace("-", "_")
    mode_aliases = {
        "legacy": "legacy",
        "legacy_app_id_access_token": "legacy",
        "api_key": "api_key",
        "new": "api_key",
        "new_console_api_key": "api_key",
    }
    if mode not in mode_aliases:
        raise ValueError("Volcengine credential mode must be legacy or api_key")
    mode = mode_aliases[mode]

    if mode == "legacy":
        app_id = _validate_single_line(input_fn("Volcengine legacy APP ID: "), APP_ID_KEY)
        app_id = app_id or existing_app_id
    else:
        app_id = ""
    resource_id = _validate_single_line(
        input_fn(f"Volcengine resource ID [{existing_resource}]: "), RESOURCE_ID_KEY
    )
    resource_id = resource_id or existing_resource
    if mode == "legacy":
        access_token = _validate_single_line(
            secret_input_fn("Volcengine legacy access token (input hidden): "), ACCESS_TOKEN_KEY
        )
        access_token = access_token or existing_token
        api_key = ""
        required_values = ((APP_ID_KEY, app_id), (ACCESS_TOKEN_KEY, access_token))
    else:
        access_token = ""
        api_key = _validate_single_line(
            secret_input_fn("Volcengine new-console API key (input hidden): "), API_KEY_KEY
        )
        api_key = api_key or existing_api_key
        required_values = ((API_KEY_KEY, api_key),)

    missing = [key for key, value in required_values if not value]
    if missing:
        raise ValueError(f"Missing required Volcengine fields: {', '.join(missing)}")

    _atomic_write_text(
        env_path,
        _updated_env_text(
            existing_text,
            {
                APP_ID_KEY: app_id,
                ACCESS_TOKEN_KEY: access_token,
                API_KEY_KEY: api_key,
                RESOURCE_ID_KEY: resource_id,
            },
        ),
    )
    return inspect_volc_config(root, environ={})

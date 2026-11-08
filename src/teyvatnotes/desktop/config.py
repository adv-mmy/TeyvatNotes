"""桌面设置读取用户选定的配置，敏感内容只在本机处理。"""

import os
import re
from codecs import BOM_UTF8
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from stat import S_ISREG
from tempfile import NamedTemporaryFile

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from teyvatnotes.application.model_ports import ModelError
from teyvatnotes.models.responses import ResponsesSettings

MAX_ENV_BYTES = 262_144


class DesktopError(Exception):
    """只向窗口传递稳定错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    path: Path
    raw: bytes = field(repr=False)
    values: dict[str, str] = field(repr=False)

    @property
    def base_url(self) -> str:
        candidate = self.values.get("LLM_BASE_URL", "")
        try:
            ResponsesSettings("configuration-check", candidate, "configuration-check")
        except ModelError:
            return ""
        return candidate

    @property
    def model(self) -> str:
        return self.values.get("LLM_MODEL", "")

    @property
    def has_key(self) -> bool:
        return bool(self.values.get("LLM_API_KEY"))


def data_directory() -> Path:
    """配置和安装目录分开；验收可指定隔离的用户数据目录。"""
    override = os.getenv("TEYVAT_NOTES_DATA_DIR")
    if override:
        return Path(override).absolute()
    return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "TeyvatNotes"


def _read(path: Path) -> bytes:
    try:
        if not S_ISREG(path.lstat().st_mode):
            raise DesktopError("config_unavailable")
        with path.open("rb") as stream:
            raw = stream.read(MAX_ENV_BYTES + 1)
        if len(raw) > MAX_ENV_BYTES:
            raise DesktopError("config_unavailable")
        return raw
    except FileNotFoundError:
        return b""
    except OSError:
        raise DesktopError("config_unavailable") from None


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError:
        raise DesktopError("config_unavailable") from None


def _snapshot(path: Path, raw: bytes) -> ConfigSnapshot:
    parsed = dotenv_values(stream=StringIO(_decode(raw)))
    return ConfigSnapshot(path, raw, {key: value or "" for key, value in parsed.items()})


def load_config(path: Path) -> ConfigSnapshot:
    return _snapshot(path, _read(path))


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _replace_values(raw: bytes, updates: dict[str, str]) -> bytes:
    text = _decode(raw)
    newline = "\r\n" if "\r\n" in text else "\n"
    pending = updates.copy()
    output: list[str] = []
    for binding in parse_stream(StringIO(text)):
        original = binding.original.string
        if binding.key is None or binding.key not in updates:
            output.append(original)
            continue
        output.append(original[: len(original) - len(original.lstrip())])
        if binding.key in pending:
            export = "export " if re.match(r"export[ \t]+", original.lstrip()) else ""
            value = _quote(pending.pop(binding.key))
            output.append(f"{export}{binding.key}={value}{newline}")
    result = "".join(output)
    if pending and result and not result.endswith(("\r", "\n")):
        result += newline
    result += "".join(f"{key}={_quote(value)}{newline}" for key, value in pending.items())
    return (BOM_UTF8 if raw.startswith(BOM_UTF8) else b"") + result.encode("utf-8")


def save_model(snapshot: ConfigSnapshot, base_url: str, model: str, api_key: str) -> ConfigSnapshot:
    """提交前复核文件状态，一次替换且不把凭据写进界面偏好。"""
    path = snapshot.path
    if _read(path) != snapshot.raw:
        raise DesktopError("config_conflict")
    if not api_key and (
        not snapshot.has_key or base_url.rstrip("/") != snapshot.base_url.rstrip("/")
    ):
        raise DesktopError("config_key_required")
    changes = {"LLM_BASE_URL": base_url, "LLM_MODEL": model}
    if api_key:
        changes["LLM_API_KEY"] = api_key
    if any("${" in value for value in changes.values()):
        raise DesktopError("config_invalid_model")
    try:
        ResponsesSettings.from_env(snapshot.values | changes)
    except ModelError:
        raise DesktopError("config_invalid_model") from None
    updated = _replace_values(snapshot.raw, changes)
    if len(updated) > MAX_ENV_BYTES:
        raise DesktopError("config_unavailable")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".teyvat-env-", delete_on_close=False
        ) as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            if _read(path) != snapshot.raw:
                raise DesktopError("config_conflict")
            os.replace(stream.name, path)
    except OSError:
        raise DesktopError("config_unavailable") from None
    return _snapshot(path, updated)

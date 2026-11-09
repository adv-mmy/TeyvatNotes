"""安装配置只包含共享攻略服务的 HTTPS 地址；不读取旧 .env。"""

import json
import os
import re
import ssl
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

import httpx

MAX_CONFIG_BYTES = 4096
MAX_CA_BYTES = 131_072
CA_FILE = "service-ca.pem"
_BAD_ORIGIN_CHARS = re.compile(r"""[\x00-\x20\x7f'"\\%]""")
_CERTIFICATES = re.compile(
    rb"(?:\s*-----BEGIN CERTIFICATE-----\s*[A-Za-z0-9+/=\s]+"
    rb"-----END CERTIFICATE-----)+\s*"
)


class DesktopError(Exception):
    """只向窗口传递稳定错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ClientConfig:
    path: Path
    raw: bytes = field(repr=False)
    service_url: str = ""


def data_directory() -> Path:
    """配置、私有历史与安装目录分开；验收可指定隔离的数据目录。"""
    override = os.getenv("TEYVAT_NOTES_DATA_DIR")
    if override:
        return Path(override).absolute()
    return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "TeyvatNotes"


def normalize_service_url(value: str) -> str:
    """只接受 HTTPS origin，禁止用户信息、路径、片段和查询。"""
    if not value or len(value) > 2048 or _BAD_ORIGIN_CHARS.search(value):
        raise DesktopError("client_config_invalid")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or (parsed.port is not None and parsed.port == 0)
        ):
            raise ValueError
        origin = httpx.URL(value)
        if not origin.host or origin.raw_path != b"/":
            raise ValueError
        return str(origin).removesuffix("/")
    except (ValueError, httpx.InvalidURL):
        raise DesktopError("client_config_invalid") from None


def read_regular(path: Path, maximum: int) -> bytes:
    """缺失与空文件均按未配置处理；拒绝符号链接及非普通文件。"""
    try:
        if not S_ISREG(path.lstat().st_mode):
            raise DesktopError("client_config_unavailable")
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise DesktopError("client_config_unavailable")
        return raw
    except FileNotFoundError:
        return b""
    except OSError:
        raise DesktopError("client_config_unavailable") from None


def atomic_write(path: Path, value: bytes, *, expected: bytes | None = None) -> None:
    """替换当前组件自己的文件，不覆盖期间被修改的用户设置。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".teyvat-client-", delete_on_close=False
        ) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            if expected is not None and read_regular(path, MAX_CONFIG_BYTES) != expected:
                raise DesktopError("client_config_conflict")
            os.replace(stream.name, path)
    except OSError:
        raise DesktopError("client_config_unavailable") from None


def load_config(path: Path) -> ClientConfig:
    raw = read_regular(path, MAX_CONFIG_BYTES)
    if not raw:
        return ClientConfig(path, raw)
    try:
        parsed = tomllib.loads(raw.decode("utf-8-sig"))
        if set(parsed) != {"service_url"} or not isinstance(parsed["service_url"], str):
            raise ValueError
    except (ValueError, UnicodeError):
        raise DesktopError("client_config_invalid") from None
    return ClientConfig(path, raw, normalize_service_url(parsed["service_url"]))


def save_config(snapshot: ClientConfig, value: str) -> ClientConfig:
    service_url = normalize_service_url(value)
    if read_regular(snapshot.path, MAX_CONFIG_BYTES) != snapshot.raw:
        raise DesktopError("client_config_conflict")
    content = f"service_url = {json.dumps(service_url)}\n".encode()
    atomic_write(snapshot.path, content, expected=snapshot.raw)
    return ClientConfig(snapshot.path, content, service_url)


def validate_public_ca(path: Path) -> bytes:
    """只读入 PEM 公共证书；禁止将私钥当作构建参数打包。"""
    raw = read_regular(path, MAX_CA_BYTES)
    if not raw or _CERTIFICATES.fullmatch(raw) is None:
        raise DesktopError("client_ca_invalid")
    try:
        ssl.create_default_context(cadata=raw.decode("ascii"))
    except (ValueError, UnicodeError, ssl.SSLError):
        raise DesktopError("client_ca_invalid") from None
    return raw


def tls_context(directory: Path, origin: str) -> ssl.SSLContext:
    """保留系统证书信任，仅允许本机环境显式增加公开 CA，不关闭校验。"""
    ca = directory / CA_FILE
    context = ssl.create_default_context()
    if urlsplit(origin).hostname in {"127.0.0.1", "localhost", "::1"} and ca.exists():
        context.load_verify_locations(cadata=validate_public_ca(ca).decode("ascii"))
    return context

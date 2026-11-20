"""统一 JSON 配置文件存取。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypeVar


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_T = TypeVar("_T")


def config_path() -> Path:
    override = os.environ.get("UU_TUNNEL_CONFIG")
    return Path(override) if override else _PROJECT_ROOT / "uu_tunnel.json"


def load() -> dict[str, Any]:
    """加载配置；缺失或损坏时返回空配置。"""

    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict[str, Any]) -> None:
    """原子保存配置。"""

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _restrict_user_permissions(temp_path)
        os.replace(temp_path, path)
        temp_path = None
        _restrict_user_permissions(path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def get_setting(data: dict[str, Any], key: str, default: _T) -> Any | _T:
    return data.get(key, default)


def set_setting(data: dict[str, Any], key: str, value: Any) -> None:
    data[key] = value


def _restrict_user_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

"""ABI-2 sing-tun ``uutun`` 原生库的 ctypes 封装：一块 /32 地址、路由整个隧道网段的 TUN。"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import platform
import sys
import threading
import zlib
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Final

from . import firewall
from .config import (
    TUNNEL_MTU,
    TUNNEL_SUPERNET,
    TunnelConfigError,
    TunnelDeviceError,
    TunnelPlatformError,
    TunnelStateError,
)


logger = logging.getLogger(__name__)
ADAPTER_NAME: Final[str] = "uutun"


class NativeTunError(TunnelDeviceError):
    """调用原生 TUN 库失败。"""


class DeviceState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    CLOSED = "closed"


_ABI_VERSION: Final[int] = 2
_OK: Final[int] = 0
_ERROR: Final[int] = -1
_CLOSED: Final[int] = -2
_INVALID_ARGUMENT: Final[int] = -3
_MAX_NATIVE_PACKET: Final[int] = 65_535
_MAX_ERROR_TEXT: Final[int] = 4096

_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
# 每个平台按优先级列出候选文件名；{arch} 为归一化的 CPU 架构（macOS 优先通用二进制）。
_PLATFORM_LIBRARY_LAYOUT: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "win32": ("windows", ("uutun.dll",)),
    "linux": ("linux", ("libuutun-{arch}.so", "libuutun.so")),
    "darwin": ("macos", ("libuutun.dylib", "libuutun-{arch}.dylib")),
}
_ARCH_ALIASES: Final[dict[str, str]] = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}
_CharPointer = ctypes.POINTER(ctypes.c_char)


class _NativeApi:
    """绑定 ABI-2 入口；首次使用时才加载。"""

    def __init__(self, library: Any) -> None:
        self._library = library
        self.abi_version = library.UutunAbiVersion
        self.abi_version.argtypes = []
        self.abi_version.restype = ctypes.c_uint32

        self.open = library.UutunOpen
        self.open.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        self.open.restype = ctypes.c_int64

        self.read = library.UutunRead
        self.read.argtypes = [ctypes.c_int64, _CharPointer, ctypes.c_int32]
        self.read.restype = ctypes.c_int32

        self.write = library.UutunWrite
        self.write.argtypes = [ctypes.c_int64, _CharPointer, ctypes.c_int32]
        self.write.restype = ctypes.c_int32

        self.close = library.UutunClose
        self.close.argtypes = [ctypes.c_int64]
        self.close.restype = ctypes.c_int32

        self.last_error = library.UutunLastError
        self.last_error.argtypes = []
        self.last_error.restype = ctypes.c_char_p


_api_lock = threading.Lock()
_api: _NativeApi | None = None


def _machine_arch() -> str:
    """归一化 CPU 架构名：x86_64 / arm64；Linux 的 arm64 库按 GNU 习惯命名为 aarch64。"""

    machine = platform.machine().lower()
    arch = _ARCH_ALIASES.get(machine, machine)
    if sys.platform == "linux" and arch == "arm64":
        return "aarch64"
    return arch


def _native_library_path() -> Path:
    """按平台与架构选择本机库；都不存在时返回首选路径以便报错指明位置。"""

    try:
        platform_directory, templates = _PLATFORM_LIBRARY_LAYOUT[sys.platform]
    except KeyError as exc:
        raise TunnelPlatformError(f"native TUN is not supported on {sys.platform}") from exc
    directory = (_PROJECT_ROOT / "native" / "bin" / platform_directory).resolve()
    candidates = [directory / template.format(arch=_machine_arch()) for template in templates]
    return next((path for path in candidates if path.is_file()), candidates[0])


def _load_native() -> _NativeApi:
    """延迟加载并校验 ABI；模块导入期间绝不加载。"""

    global _api
    library_path = _native_library_path()
    if not library_path.is_file():
        raise NativeTunError(f"native TUN library is missing: {library_path}")
    with _api_lock:
        if _api is not None:
            return _api
        try:
            library = ctypes.CDLL(str(library_path))
        except OSError as exc:
            raise NativeTunError(f"native TUN library could not be loaded: {library_path}") from exc
        api = _NativeApi(library)
        if int(api.abi_version()) != _ABI_VERSION:
            raise NativeTunError("native TUN ABI is incompatible")
        _api = api
        return api


def _last_error(api: _NativeApi) -> str:
    message = api.last_error()
    if not message:
        return ""
    return bytes(message[: _MAX_ERROR_TEXT - 1]).decode("utf-8", "replace")


def _raise_failure(api: _NativeApi, status: int, operation: str) -> None:
    if status == _CLOSED:
        raise TunnelStateError("TUN device is closed")
    message = _last_error(api)
    if message:
        raise NativeTunError(f"{operation} failed: {message}")
    if status == _INVALID_ARGUMENT:
        raise NativeTunError(f"{operation} failed: invalid argument")
    if status == _ERROR:
        raise NativeTunError(f"{operation} failed")
    raise NativeTunError(f"{operation} failed: native status {status}")


@dataclass(frozen=True, slots=True)
class _OpenedNative:
    api: _NativeApi
    handle: int
    name: str
    if_index: int


class NativeTunDevice:
    """一块原生 TUN：本机 /32 地址，路由整个 100.64.0.0/10；只收发原始 IPv4 包。"""

    def __init__(self, local_ip: IPv4Address, mtu: int = TUNNEL_MTU) -> None:
        if not isinstance(local_ip, IPv4Address) or local_ip not in TUNNEL_SUPERNET:
            raise TunnelConfigError("tunnel local address is invalid")
        if type(mtu) is not int or not 576 <= mtu <= TUNNEL_MTU:
            raise TunnelConfigError("tunnel MTU must be 576..1280")
        self.local = local_ip
        self.mtu = mtu
        self._state = DeviceState.NEW
        self._api: _NativeApi | None = None
        self._handle: int | None = None
        self._name: str | None = None
        self._if_index: int | None = None
        self._open_task: asyncio.Task[_OpenedNative] | None = None
        self._read_task: asyncio.Task[bytes] | None = None
        # 读取互斥（_read_task 独占），单个常驻缓冲区即可复用。
        self._read_buffer = (ctypes.c_uint8 * _MAX_NATIVE_PACKET)()
        self._write_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str | None:
        return self._name

    @staticmethod
    def preflight() -> None:
        """加载并校验原生库，不打开网卡。"""

        _load_native()

    async def start(self) -> NativeTunDevice:
        """在线程里打开原生网卡并记下名称。"""

        if self._state is not DeviceState.NEW:
            raise TunnelStateError("TUN device is not new")
        if self._open_task is not None:
            raise TunnelStateError("TUN device is already opening")
        api = _load_native()
        task = asyncio.create_task(asyncio.to_thread(self._open_blocking, api), name="uu-tunnel-open")
        self._open_task = task
        try:
            opened = await asyncio.shield(task)
        except asyncio.CancelledError:
            self._state = DeviceState.CLOSED
            await self._wait_for_task(task)
            opened = self._completed_open(task)
            if opened is not None and self._close_task is None:
                self._close_native_handle(opened.api, opened.handle, suppress_failure=True)
                if self._open_task is task:
                    self._open_task = None
            raise
        except BaseException:
            self._state = DeviceState.CLOSED
            if self._open_task is task:
                self._open_task = None
            raise

        if self._state is not DeviceState.NEW:
            if self._close_task is None:
                self._close_native_handle(opened.api, opened.handle, suppress_failure=True)
                if self._open_task is task:
                    self._open_task = None
            raise TunnelStateError("TUN device is closed")

        self._api = opened.api
        self._handle = opened.handle
        self._name = opened.name
        self._if_index = opened.if_index
        if self._open_task is task:
            self._open_task = None
        self._state = DeviceState.RUNNING
        try:
            await firewall.allow_tunnel_adapter(opened.if_index, opened.name, TUNNEL_SUPERNET)
        except Exception:
            logger.warning("failed to configure TUN adapter firewall", exc_info=True)
        return self

    async def read_packet(self) -> bytes:
        """读取一个原始包。"""

        handle = self._require_running()
        if self._read_task is not None:
            raise TunnelStateError("a TUN read is already active")
        task = asyncio.create_task(asyncio.to_thread(self._read_blocking, handle), name="uu-tunnel-read")
        self._read_task = task
        task.add_done_callback(self._release_read_task)
        try:
            # ABI-2 没有独立的中断原语；取消时 worker 保持占位，直到 Close 唤醒阻塞的 Read。
            packet = await asyncio.shield(task)
        finally:
            if self._read_task is task and task.done():
                self._read_task = None
        if self._state is not DeviceState.RUNNING:
            raise TunnelStateError("TUN device is closed")
        return packet

    async def write_packet(self, packet: bytes) -> None:
        """在线程里写入一个原始包。"""

        async with self._write_lock:
            handle = self._require_running()
            if self._write_task is not None:
                raise TunnelStateError("a TUN write is already active")
            if not isinstance(packet, bytes) or not 20 <= len(packet) <= self.mtu:
                raise TunnelConfigError("IPv4 packet size is invalid")
            task = asyncio.create_task(
                asyncio.to_thread(self._write_blocking, handle, packet), name="uu-tunnel-write"
            )
            self._write_task = task
            task.add_done_callback(self._release_write_task)
            try:
                await asyncio.shield(task)
            finally:
                if self._write_task is task and task.done():
                    self._write_task = None
            if self._state is not DeviceState.RUNNING:
                raise TunnelStateError("TUN device is closed")

    async def close(self) -> None:
        """只关闭一次；sing-tun 的 Close 会唤醒阻塞中的原生读取。"""

        if self._close_task is None:
            self._state = DeviceState.CLOSED
            self._close_task = asyncio.create_task(self._close_once(), name="uu-tunnel-close")
        await asyncio.shield(self._close_task)

    def _open_blocking(self, api: _NativeApi) -> _OpenedNative:
        handle = int(
            api.open(
                ADAPTER_NAME.encode("utf-8"),
                f"{self.local}/32".encode("utf-8"),
                str(TUNNEL_SUPERNET).encode("utf-8"),
                ctypes.c_uint32(self.mtu),
            )
        )
        if handle <= 0:
            _raise_failure(api, handle, "TUN open")
        # ABI-2 不导出接口索引；防火墙规则名只需一个稳定 ID。
        return _OpenedNative(api, handle, ADAPTER_NAME, zlib.crc32(ADAPTER_NAME.encode("utf-8")) or 1)

    def _read_blocking(self, handle_value: int) -> bytes:
        api = self._require_api()
        buffer = self._read_buffer
        size = int(api.read(ctypes.c_int64(handle_value), ctypes.cast(buffer, _CharPointer), ctypes.c_int32(len(buffer))))
        if size > 0:
            if not 20 <= size <= _MAX_NATIVE_PACKET:
                raise NativeTunError("TUN read failed: native packet length is invalid")
            return ctypes.string_at(buffer, size)
        _raise_failure(api, size, "TUN read")
        raise AssertionError("unreachable native status")

    def _write_blocking(self, handle_value: int, payload: bytes) -> None:
        api = self._require_api()
        # POINTER(c_char) 直接接受 bytes，免去一次整包拷贝。
        written = int(api.write(ctypes.c_int64(handle_value), payload, ctypes.c_int32(len(payload))))
        if written == len(payload):
            return
        if written >= 0:
            raise NativeTunError("TUN write failed: native partial packet")
        _raise_failure(api, written, "TUN write")
        raise AssertionError("unreachable native status")

    async def _close_once(self) -> None:
        open_task = self._open_task
        if open_task is not None:
            await self._wait_for_task(open_task)

        handle = self._handle
        api = self._api
        if handle is None or api is None:
            opened = self._completed_open(open_task)
            if opened is None:
                self._open_task = None
                return
            handle = opened.handle
            api = opened.api

        close_failure: TunnelDeviceError | TunnelStateError | None = None
        try:
            # Close 会等待正在使用该设备的原生 I/O 返回，须在线程里执行。
            await asyncio.to_thread(self._close_native_handle, api, handle, suppress_failure=False)
        except (TunnelDeviceError, TunnelStateError) as exc:
            close_failure = exc

        for task in (self._read_task, self._write_task):
            if task is not None:
                await self._wait_for_task(task)

        self._handle = None
        self._api = None
        self._open_task = None

        if self._if_index is not None:
            try:
                await firewall.remove_tunnel_adapter_rules(self._if_index)
            except Exception:
                logger.warning("failed to remove TUN adapter firewall rule", exc_info=True)

        if close_failure is not None:
            raise close_failure

    async def _wait_for_task(self, task: asyncio.Task[Any]) -> None:
        """即使等待方再次被取消，也要 join 原生 worker。"""

        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return
                continue
            except BaseException:
                return
            else:
                return

    def _release_read_task(self, task: asyncio.Task[bytes]) -> None:
        if self._read_task is task:
            self._read_task = None
        self._consume_task_exception(task)

    def _release_write_task(self, task: asyncio.Task[None]) -> None:
        if self._write_task is task:
            self._write_task = None
        self._consume_task_exception(task)

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _completed_open(task: asyncio.Task[_OpenedNative] | None) -> _OpenedNative | None:
        if task is None or not task.done() or task.cancelled():
            return None
        try:
            return task.result()
        except BaseException:
            return None

    def _close_native_handle(self, api: _NativeApi, handle: int, *, suppress_failure: bool) -> None:
        status = int(api.close(ctypes.c_int64(handle)))
        if status != _OK and not suppress_failure:
            _raise_failure(api, status, "TUN close")

    def _require_api(self) -> _NativeApi:
        if self._api is None:
            raise TunnelStateError("TUN device is not running")
        return self._api

    def _require_running(self) -> int:
        if self._state is DeviceState.CLOSED:
            raise TunnelStateError("TUN device is closed")
        if self._state is not DeviceState.RUNNING or self._handle is None:
            raise TunnelStateError("TUN device is not running")
        return self._handle


__all__ = ["ADAPTER_NAME", "DeviceState", "NativeTunDevice", "NativeTunError"]

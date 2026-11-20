# 协议字段笔记

本文件区分已观察到的行为与本项目的实现选择。它不是 UU 的完整 schema；源码和 `proto/connect_options.proto` 才是当前载荷的准确信息。

## 已观察到的字段

- 房间记录使用 `token` 和 `signaling_list`，形如 `{"token":"...","signaling_list":["..."]}`。
- API code `1120` 表示 `token_expired`。
- 业务消息外层为 `type` 和 `data`。
- 手动确认请求为 `remote_control_by_confirmation`，带 `control_id` 和 `name`；取消为 `remote_control_by_confirmation_cancel`，带 `control_id`。

## API 和设备

所有业务请求使用 HMAC-SHA256 签名；签名基串依次为大写 HTTP 方法、路径、按小写名称排序的 `x-param-*` 头和原始 JSON body。Windows 设备初始化使用 `platform: 1`、字符串 `mac` 和以 MiB 计的整数 `memory`。

| 操作 | 方法和路径 |
|---|---|
| 初始化 Windows 设备 | `POST /api/v1/device/windows/init` |
| 发送短信验证码 | `POST /api/v1/security/mobile/code` |
| 短信登录 | `POST /api/v1/login/by_mobile` |
| 创建游客身份 | `POST /api/v1/guest/create` |
| 列出账号设备 | `GET /api/v1/device/groups/of/my` |
| 创建账号房间 | `POST /api/v1/room/create` |
| 创建游客房间 | `POST /api/v1/guest/room/create` |
| 发布游客协助 ID | `POST /api/v1/guest/share/info` |
| 加入账号设备房间 | `POST /api/v1/room/join/by_device/{device_id}` |
| 清理账号房间 | `POST /api/v1/room/clear/by_device/{device_id}` |
| 密码方式加入 | `POST /api/v2/room/join/share/by_code` |
| 手动确认查询 | `POST /api/v2/room/join/share/by_confirmation` |
| 游客提交密码证明 | `POST /api/v1/guest/room/share/upload/sign` |
| 游客确认或拒绝 | `POST /api/v1/guest/room/share/confirmation` |

房间创建 body 固定为 `{"last_controlled_interval":0}`。当前没有已知的设备改名/更新端点；`rename` 只保存本地名称，供下一次设备注册使用。`logout` 也没有 HTTP 请求，只删除本地登录态。

## 游客访问码

- 字符集为 `ABCDEFGHJKLMNPQRSTUVWXYZ123456789`，长度固定为 8。
- 密码模式把 `SHA-256(salt + code)` 的大写十六进制值作为 `sign` 上传。
- 带 `--code` 的主控端只走密码端点；不带 `--code` 的主控端只走手动确认端点。两种模式不自动降级。
- 手动模式只有 `Y/y` 同意；`N/n`、Enter 和本地确认失败都会拒绝。

## 本项目的信令和隧道规则

- `control` 的 `app_data` 是 `ConnectOptions` protobuf；每次 controller 会话生成新的小写 UUID4 `app_control_id`。`ConnectOptions.device_id` 是本机主控设备 ID。
- WSS 握手还使用 `X-NRD-CONTROLLING`、`streamer_version="V3.1.14"` 和 gzip SDP 标志。`ice_network_type=3` 是固定应用值，官方枚举含义未确认。
- `x_uu_tunnel` 是自定义 ICE bootstrap，不是官方逆向事实。被控端只能接受与 route、generation 和主控端选中 profile 一致的 offer。
- 数据面：控制通道 JSON（`hello` / `tun` / `handoff_*`，`v=2`）加数据通道原始 IPv4 二进制包；网段 `100.64.0.0/10`，每台机器一个固定地址，MTU `1280`。

## 验证边界

当前 Windows 产物是 `native/bin/windows/uutun.dll` 的 Go `sing-tun` ABI-2 实现，嵌入 Wintun。Rust ABI-1 实现已移除。

已建立的隧道在网络切换后不会自动重新选路。官方客户端的 DataChannel 载荷、`ice_network_type=3` 的语义、所有 NAT/relay 情况和其他平台原生层都不应视为已验证。

# 协议笔记

这里记录本项目实际使用的载荷和已观察到的 UU 行为，不是 UU 的完整官方协议说明。`proto/connect_options.proto` 及生成的 `uu_tunnel/webrtc/connect_options_pb2.py` 只服务于本项目的 controller `control` 载荷。

## 信令和协商

- 房间 token 用 `X-NRD-AUTH` 连接认证 WSS/Socket.IO。库负责 Engine.IO、Socket.IO、心跳、二进制附件和 ACK；应用只处理 UU 二进制 MESSAGE 的前导 `0x04`。
- 主控端每次会话发送新的 `app_control_id`、`ConnectOptions` protobuf `app_data` 和 `streamer_data`。其中 `device_id` 是本机主控身份，不是命令传入的远端设备 ID。
- `control` ACK 固定本次 SOAC route 的 `client_id`、`ice_id` 和 `app_control_id`。同一路由中可以换下一份 ICE profile，但不会重发 `control` 或复用结果不明的发送。
- offer 和成功 answer 使用完整 gzip SDP，必须带候选和 `a=end-of-candidates`；协商不是 trickle ICE。

`x_uu_tunnel` 是本项目自己的 SOAC 扩展，不是已确认的 UU 官方字段。它让两端使用主控端选定的 ICE profile、generation 和 relay 策略。强制 relay 时，两端只收集 relay 候选，并在 DataChannel 打开后验证实际提名的本地候选；没有合格 TURN/TURNS 或验证失败就终止，不会悄悄回退为直连。

## L3 数据面

这也是本项目的约定，不是逆向出的 UU DataChannel 格式：

1. 隧道建立后两端在控制 DataChannel（`uu-tunnel-ctl`）上各发一条 `hello`：`{"type":"hello","v":2,"addr":"100.x.x.x","name":"<设备名>","tun":<本机 TUN 是否已挂载>}`。收不到对端 hello 即握手失败。
2. 本机 TUN 挂载/卸载时向全部对端发 `{"type":"tun","v":2,"mounted":<bool>}`；对端未挂载时本机不往该隧道发包。
3. 数据 DataChannel 的每个二进制消息就是一个原始 IPv4 包，没有额外封套或分片；接收方只接受 源=对端地址、目的=本机地址 的包。
4. `handoff_request` / `handoff_room` / `handoff_commit` / `handoff_committed` / `handoff_error`（`v":2`）用于把一条隧道的传输迁移到一次性协助会话：控制端请求，被控端开房并回连接 ID 与访问码，控制端加入新会话并在其控制通道上重新交换 hello、发 commit，被控端切换传输后回 committed。

网段固定为 `100.64.0.0/10`，每台机器一个由机器身份派生的固定地址，不区分主控/被控。TUN 由 Go `sing-tun` ABI-2 `c-shared` 库创建（Windows 产物 `native/bin/windows/uutun.dll`，内嵌 Wintun）。

## 已知边界

- 原生 DLL 的本地 UDP read/write 已验证。不要据此推断官方客户端互通、所有 NAT 拓扑或所有 relay 情况均已验证。
- 已建立隧道在网络切换后还不会自动重新选路；停止并创建新的 room/session。
- 字段、端点和访问码的具体记录在 [FIELD_NOTES.md](FIELD_NOTES.md)。

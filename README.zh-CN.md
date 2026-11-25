# NanoU

[English](README.md)

NanoU 可通过官方 UU 远程客户端或 NanoU 浏览器控制端远程操作 Sipeed NanoKVM。

## 在 NanoKVM Go 上直接运行

[NanoKVM Go 版](nanokvm-go/README.zh-CN.md)是一个 ARMv7 SDK 应用，可将设备的 H.264 视频发送给官方 UU 远程客户端，并转发键盘和鼠标输入。

## 使用无需浏览器的 Linux 桥接

桥接程序以普通 Linux 进程运行。它沿用设备端应用的固件接口，直接转发 NanoKVM Go 硬件编码的 H.264 视频，并发送键盘、鼠标输入。控制端使用官方 UU 远程客户端。Linux 主机需为 x86-64 或 ARM64，安装 Python 3.10 或以上版本，并能访问 KVM 和 UU 服务。

1. 安装 Docker、Git、带 pip 的 Python、tar 和 unzip，然后为 Linux 主机构建安装包。

   ```sh
   sh nanokvm-go/build.sh --linux amd64
   tar -xzf nanokvm-go/NanoU-linux-amd64.tar.gz
   cd NanoU-linux-amd64
   ```

   ARM64 主机请将命令中的 `amd64` 替换为 `arm64`。

2. 以普通 Linux 用户运行绑定命令，用官方 UU 远程手机端扫描终端中的二维码。

   ```sh
   ./nanou-bridge login --name "NanoKVM Go"
   ```

   可复用 `~/.nanou/native-uu.json` 中已有的 Linux 桥接绑定。请先停止旧桥接程序，再启动直连桥接。

3. 填写设备的 HTTPS 地址和已验证的 TLS 证书指纹。按提示输入本机 KVM 密码，然后从官方 UU 远程客户端打开已绑定的设备。

   ```sh
   ./nanou-bridge run https://192.168.1.42 --tls-pin YOUR_VERIFIED_SHA256_FINGERPRINT
   ```

   本机 KVM 账号不是 `admin` 时，使用 `--username`。无人值守运行可通过环境变量 `NANOU_KVM_PASSWORD` 和 `NANOU_KVM_TLS_PIN` 提供配置。用 `--mouse-sensitivity` 设置相对鼠标灵敏度百分比。使用 `--audio` 时，请先在 KVM 启用虚拟音频和 MCP 服务，再通过 `NANOU_KVM_MCP_KEY` 提供密钥。

在项目目录中，`python3 -m nanou native-login --bundle /path/to/NanoU-linux-amd64` 与 `python3 -m nanou native --bundle /path/to/NanoU-linux-amd64 …` 会调用同一个程序。直连桥接使用 NanoKVM Go 固件的 H.264 和 HID 接口。

桥接服务会将 H.264 的实际画面尺寸同步到 UU 远程。UU 连接期间，如果 NanoKVM 报告无视频信号，桥接服务会轻微移动一次鼠标。设置 `NANOU_WAKE_ON_NO_SIGNAL=false` 可关闭此行为。

### iPhone 和 iPad

如果连接到 NanoKVM Go 的目标设备是 iPhone，请在该设备上开启辅助触控。启动 Linux 直连桥接时加上 `--iphone-drag-repair`，即可在重新连接后的第一次鼠标输入前自动修复拖拽。请在 UU 远程中使用鼠标指针模式。

对于支持 USB 触摸屏的目标设备，启动时加上 `--click-to-touch` 可将左键点击和拖拽转换为单指触控。在 NanoKVM 网页控制台选择**多点触控屏**，在 UU 远程选择**屏幕触控**并关闭**平板触控模式**。

如需让官方 UU 远程客户端持续显示，可在系统设置中选择 **显示与亮度 → 自动锁定 → 永不**（如果有此选项）。桥接服务无法修改手机的自动锁定设置。

锁屏后解锁并返回 UU 远程。连接恢复时，直连桥接服务可从新的 H.264 关键帧恢复画面。如果 UU 已结束会话，请重新打开已配对的设备。

### Chromium 兼容模式

NanoU 在 Linux 的虚拟显示器中用 Chromium 打开 NanoKVM 控制台，再由 [VallierDev 的 UU 兼容主机程序](https://github.com/VallierDev/uu-remote-linux)将画面发送到官方客户端。键盘和鼠标输入沿相反方向传递。

请使用能访问 NanoKVM 和 UU 服务的专用 Linux 主机或虚拟机。UU 兼容主机程序也可为登录账号提供终端和文件访问功能，请将个人文件和无关凭据保存在其他设备上。NanoU 默认关闭远程协助、端口映射及壁纸上传。

1. 从[上游版本发布页](https://github.com/VallierDev/uu-remote-linux/releases)安装 `uuremote-server`。同时安装 Python 3.10 或以上版本、Chromium 或 Chrome、含 libx264 的 FFmpeg、Xvfb、xauth、Openbox 和 xdotool。Debian 上对应软件包为 `python3 chromium ffmpeg xvfb xauth openbox xdotool`。Ubuntu 上也可使用 Google Chrome。
2. 在 Linux 桥接主机的项目目录运行以下命令，用准备控制设备的 UU 账号在官方手机端扫码绑定。

   ```sh
   python3 -m nanou native-login --kiosk
   ```

3. 以普通 Linux 用户启动桥接服务，将示例地址替换为桥接主机可访问的 NanoKVM 实际地址。

   ```sh
   python3 -m nanou native --kiosk http://192.168.1.42
   ```

4. 在电脑或手机的官方 UU 客户端设备列表中选择在线的 Linux 主机，打开远程桌面，并在画面中的网页上登录 NanoKVM。按 Ctrl+C 可停止桥接服务。

桥接服务运行在 Linux 主机或虚拟机上。此模式会对已渲染的画面再编码一次。被客户端或浏览器拦截的组合键可通过 NanoKVM 的屏幕键盘菜单发送。使用 HTTPS 时，请在桥接主机上按本地访问的方式信任 NanoKVM 证书。

## 使用 NanoU 浏览器控制端

此模式使用 UU 登录和信令、WebRTC 数据通道和 TCP 转发。两端分别运行 NanoU，使用各自的设备身份，并登录同一个 UU 账号。登录使用中国大陆手机短信验证码（+86）。

1. 两台电脑均安装 Python 3.10 或以上版本和 [uv](https://docs.astral.sh/uv/)，然后在项目目录运行 `uv sync --locked`。
2. 在能访问 NanoKVM 的电脑上启动网关，将示例地址替换为实际地址，并按提示登录。

   ```sh
   uv run --locked python -m nanou gateway http://192.168.1.42 --name "NanoU Gateway"
   ```

3. 在控制端运行 `uv run --locked python -m nanou`。登录后选择网关及 HTTP 或 HTTPS 协议，点击 **Connect**，再点击 **Open NanoKVM**。在打开的控制台中用 NanoKVM 账号登录。点击 **Disconnect** 可关闭本地端口和隧道。

网关只转发到指定的控制台地址。控制端页面及转发端口只监听本机回环地址。隧道支持 HTTP、WebSocket 和 MJPEG 等基于 TCP 的视频模式。如果固件的 WebRTC 视频无法通过隧道连接，可切换到 MJPEG。HTTPS 转发的浏览器地址为 `127.0.0.1`，签发给设备局域网地址的证书通常无法通过该地址的名称验证。Linux 桥接模式可使用设备原有的 HTTPS 地址。

Linux 桥接和浏览器控制端分别将 UU 登录状态存放在 `~/.nanou` 下，可用 `NANOU_STATE_DIR` 更改位置。

## 实现参考

- [隧道协议说明](upstream/docs/protocol/README.md)记录了浏览器控制端的数据通道格式。
- 网易公开的集成文档覆盖了移动端客户端唤起和受支持的 Windows、macOS 设备绑定。官方客户端互通由 UU 兼容主机程序实现。[网易 UU 远程集成指南](https://github.com/netease-uuyc/uuremote-integration-guide) · [UU Remote Linux](https://github.com/VallierDev/uu-remote-linux)
- 控制台和硬件输入由 NanoKVM 固件提供。[Sipeed NanoKVM](https://github.com/sipeed/NanoKVM)

## 许可证

NanoU 采用 GPL-3.0-or-later，与上游 UU 主机项目一致。详见 [LICENSE](LICENSE) 及[随附的 aioice 许可证](vendor/aioice/LICENSE)。

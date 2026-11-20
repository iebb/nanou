# NanoU

[English](README.md)

NanoU 可通过官方 UU 远程客户端或 NanoU 浏览器控制端远程操作 Sipeed NanoKVM。

## 在 NanoKVM Go 上直接运行

[NanoKVM Go 版](nanokvm-go/README.zh-CN.md)是一个 ARMv7 SDK 应用，可将设备的 H.264 视频发送给官方 UU 远程客户端，并转发键盘和鼠标输入。

## 通过 Linux 桥接使用官方 UU 远程

NanoU 在 Linux 的虚拟显示器中用 Chromium 打开 NanoKVM 控制台，再由 [VallierDev 的 UU 兼容主机程序](https://github.com/VallierDev/uu-remote-linux)将画面发送到官方客户端。键盘和鼠标输入沿相反方向传递。

请使用能访问 NanoKVM 和 UU 服务的专用 Linux 主机或虚拟机。UU 兼容主机程序也可为登录账号提供终端和文件访问功能，请将个人文件和无关凭据保存在其他设备上。NanoU 默认关闭远程协助、端口映射及壁纸上传。

1. 从[上游版本发布页](https://github.com/VallierDev/uu-remote-linux/releases)安装 `uuremote-server`。同时安装 Python 3.10 或以上版本、Chromium 或 Chrome、含 libx264 的 FFmpeg、Xvfb、xauth、Openbox 和 xdotool。Debian 上对应软件包为 `python3 chromium ffmpeg xvfb xauth openbox xdotool`。Ubuntu 上也可使用 Google Chrome。
2. 在 Linux 桥接主机的项目目录运行以下命令，用准备控制设备的 UU 账号在官方手机端扫码绑定。

   ```sh
   python3 -m nanou native-login
   ```

3. 以普通 Linux 用户启动桥接服务，将示例地址替换为桥接主机可访问的 NanoKVM 实际地址。

   ```sh
   python3 -m nanou native http://192.168.1.42
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

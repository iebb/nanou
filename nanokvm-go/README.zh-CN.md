# NanoU for NanoKVM Go

[English](README.md) · [项目主页](../README.zh-CN.md)

NanoU 是运行在 NanoKVM Go 上的 32 位 ARM SDK 应用。它将设备硬件编码的 H.264 视频发送给官方 UU 远程客户端，并将 UU 的输入转换为固件的 HID 报告。支持视频、键盘、鼠标、文字输入及可选的目标电脑声音。

## 安装和绑定

1. 按下文构建 ZIP。在 NanoKVM Go 网页控制台打开 **设置 → 应用 → 已安装**，上传生成的 `nanokvm-go/NanoU-armv7.zip`。
2. 填写本机 NanoKVM 的用户名、密码及已验证的 HTTPS 证书 SHA-256 指纹。SDK 将这些信息保存在设备的应用设置中，供 NanoU 访问本机控制台。
3. 在设备触屏上打开 **应用 → NanoU**，用官方 UU 远程手机端扫描二维码并确认绑定。
4. 在同一个 UU 账号下打开新设备。如果触屏提示视频不可用，请关闭正在占用视频模式的 KVM 客户端后重试。

关闭开机启动时，退出触屏应用会停止服务。安装包附带应用专用的 32 位 Linux 运行库，无需安装系统软件包或更新固件。SDK 以 root 启动应用时，ARM 主机程序会降权运行。

## 设备配置

在网页控制台的 **设置 → 应用 → 已安装** 中编辑 NanoU 的配置。修改本机账号、密码或证书指纹后，请重新打开 NanoU。UU 账号通过触屏二维码单独绑定。

| 设置 | 用途 |
| --- | --- |
| **UU device name** | 更新 UU 远程设备列表中的名称。留空保留现有名称。 |
| **Start NanoU at boot** | 填写 `true` 后可在后台运行并随设备启动。在触屏上打开一次 NanoU 以应用设置，首次使用时仍需扫码。填写 `false` 并重新打开应用可停用后台服务。 |
| **Stream target audio** | 填写 `true` 后可在 UU 远程中听到目标电脑的声音。请先在 NanoKVM Go 上开启**虚拟音频**和 **MCP 服务**，再在 NanoU 中填写本机 MCP API 密钥。 |
| **Relative mouse sensitivity** | 相对鼠标移动的灵敏度，范围为 25–400%，默认 100%。绝对坐标模式仍按屏幕位置定位。 |

开机启动和声音传输默认关闭。声音从目标电脑传向 UU 客户端。

NanoU 转发固件输出的 H.264 视频。分辨率、帧率和码率由 NanoKVM 固件控制。

## 构建

安装 Docker、Git、带 pip 的 Python、zip 和 unzip，然后在项目根目录运行 `sh nanokvm-go/build.sh`。构建会将 [NanoKVM 补丁](host/nanokvm.patch)应用到固定的 [VallierDev UU 主机版本](https://github.com/VallierDev/uu-remote-linux/tree/e9844a0dd2fa9fdafaffb07e584d024eea5ea95d)，再将 ARMv7 主机程序、SDK 应用和运行库打包。主机程序沿用上游 GPL-3.0-or-later 许可，运行库的许可声明随 ZIP 提供。

本应用基于 NanoKVM Go 应用固件 0.2.7 开发。固件的直连视频协议或 HID 协议变更时，可能需要更新 NanoU。

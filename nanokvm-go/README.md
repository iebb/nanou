# NanoU for NanoKVM Go

[简体中文](README.zh-CN.md)

This NanoKVM Go SDK app runs the 32-bit ARM UU host on the KVM. It forwards the device’s hardware-encoded H.264 video and translates incoming UU keyboard and pointer events into the firmware’s HID reports. The UU connection supports video, keyboard, pointer, text input, and optional target audio.

## Install and link

1. Build the ZIP using the instructions below. On the NanoKVM Go web console, open **Settings → Apps → Installed** and upload the generated `nanokvm-go/NanoU-armv7.zip`.
2. Supply the local NanoKVM username, password, and verified SHA-256 fingerprint of its HTTPS certificate. The SDK stores these in the app’s device-side settings. NanoU uses them for the local KVM connection.
3. On the device touchscreen, open **Apps → NanoU**. Scan the displayed QR code with the official UU Remote mobile app and confirm enrollment.
4. From the same account, open the enrolled device in the official UU Remote app. If the touchscreen reports that video is unavailable, close competing KVM viewers and retry the UU session.

With boot startup off, exiting the touchscreen app stops the host. The package includes a private 32-bit Linux runtime and needs no system package installation or firmware update. The ARM binary runs as an unprivileged user when the SDK launcher runs as root.

## Device configuration

Edit NanoU’s app configuration under **Settings → Apps → Installed**. The NanoKVM username, password, and certificate fingerprint are used for the connection from NanoU to the device’s own video and input endpoints. Restart NanoU after changing them. The UU account is linked separately by scanning the app’s enrollment QR code.

| Setting | What it does |
| --- | --- |
| UU device name | Updates the device alias in UU Remote. Leave empty to keep the existing name. |
| Start NanoU at boot | Enter `true` to run in the background and start automatically after reboot. Open NanoU once on the touchscreen to apply the setting and pair if needed. Enter `false` and reopen NanoU to disable the background service. |
| Stream target audio | Enter `true` to hear the attached computer in UU Remote. Enable **Virtual Audio** and **MCP Service** on the NanoKVM Go, then provide the local **MCP API key for audio** in NanoU’s app settings. |
| Repair iPhone drag automatically | Enter `true` when the attached target is an iPhone with AssistiveTouch enabled. NanoU runs the repair before the first mouse input after reconnecting. Use Mouse Pointer mode in UU Remote. |
| Relative mouse sensitivity | Use 25–400 percent. The default is 100. Absolute pointer mode keeps its screen coordinates. |

Boot startup and audio default to off. Audio sends computer sound to the viewer. The touchscreen must be used for the first UU pairing even with boot startup enabled.

NanoU forwards the firmware’s H.264 stream. Adjust resolution, frame rate, and bitrate in the NanoKVM video settings.

## Video latency

The relay keeps up to five frames queued and discards frames that have spent 175 ms in the relay. If sending falls behind, it resumes at a fresh H.264 keyframe. On a constrained connection, reduce the NanoKVM video bitrate. A shorter encoder keyframe interval can also reduce the pause after a dropped frame.

## Build

With Docker, Git, Python with pip, zip, and unzip available, run `sh nanokvm-go/build.sh` from the project root. The build applies [the NanoKVM patch](host/nanokvm.patch) to the pinned [VallierDev UU host revision](https://github.com/VallierDev/uu-remote-linux/tree/e9844a0dd2fa9fdafaffb07e584d024eea5ea95d). It packages the compiled ARMv7 binary, the SDK app, and the compatible runtime libraries together. The host uses the upstream GPL-3.0-or-later license. Runtime library notices are included in the ZIP.

The app was developed against NanoKVM Go app firmware 0.2.7. Firmware changes to its direct video or HID protocol may require an update to the app.

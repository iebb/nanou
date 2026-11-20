# NanoU

[简体中文](README.zh-CN.md)

NanoU provides remote access to Sipeed NanoKVM devices through the official UU Remote app or NanoU’s browser controller.

## Run on a NanoKVM Go

The [NanoKVM Go port](nanokvm-go/README.md) provides an ARMv7 SDK app that sends the device’s H.264 video and handles input from the official UU Remote app.

## Use the browserless Linux bridge

The bridge runs a patched UU host as a regular Linux process. It forwards the NanoKVM Go’s hardware-encoded H.264 stream and sends keyboard and mouse input through the same firmware interface as the on-device app. The viewer is the official UU Remote app. Use a Linux x86-64 or ARM64 host with Python 3.10+ and network access to the KVM and UU services.

1. With Docker, Git, Python with pip, tar, and unzip installed, build the bundle for your Linux host.

   ```sh
   sh nanokvm-go/build.sh --linux amd64
   tar -xzf nanokvm-go/NanoU-linux-amd64.tar.gz
   cd NanoU-linux-amd64
   ```

   Use `arm64` in place of `amd64` for an ARM64 host.

2. As a regular user on the Linux host, pair it with your UU account. Scan the terminal QR code with the official UU Remote mobile app.

   ```sh
   ./nanou-bridge login --name "NanoKVM Go"
   ```

   Existing Linux bridge pairings under `~/.nanou/native-uu.json` can be reused. Stop the old bridge before starting the direct bridge.

3. Start the bridge with your KVM’s HTTPS URL and verified TLS certificate fingerprint. NanoU prompts for the local KVM password. Open the paired device in the official UU app.

   ```sh
   ./nanou-bridge run https://192.168.1.42 --tls-pin YOUR_VERIFIED_SHA256_FINGERPRINT
   ```

   Use `--username` when the local KVM account is named something other than `admin`. For unattended operation, the bridge reads `NANOU_KVM_PASSWORD` and `NANOU_KVM_TLS_PIN` from the environment. `--mouse-sensitivity` sets the relative pointer scale in percent. `--audio` uses `NANOU_KVM_MCP_KEY` with Virtual Audio and MCP Service enabled on the KVM.

From a checkout, `python3 -m nanou native-login --bundle /path/to/NanoU-linux-amd64` and `python3 -m nanou native --bundle /path/to/NanoU-linux-amd64 …` call the same launcher. The direct bridge targets the NanoKVM Go firmware’s direct H.264 and HID endpoints.

### iPhone and iPad

If an iPhone is the target plugged into NanoKVM Go, enable AssistiveTouch on that phone. Start the direct Linux bridge with `--iphone-drag-repair` to run the repair before the first mouse input after reconnecting. In UU Remote, use Mouse Pointer mode.

For a target that supports a USB touchscreen, use `--click-to-touch` to translate primary clicks and drags into one-finger contacts. Select **Multi-touch Screen** in the NanoKVM web console and **Screen Touch** in UU Remote with **Tablet Touch Mode** off.

To keep the official UU Remote app visible, set **Settings → Display & Brightness → Auto-Lock → Never** if that option is available. The host cannot change the phone’s Auto-Lock setting.

If you lock and unlock the device, return to UU Remote. The direct bridge can restart video from a fresh H.264 keyframe when the connection recovers. If UU has ended the session, open the paired device again.

### Chromium fallback

The official app connects to [VallierDev’s UU-compatible Linux host](https://github.com/VallierDev/uu-remote-linux). NanoU starts that host on a private virtual display with the NanoKVM web console open in Chromium. Video travels from NanoKVM to Chromium and then through UU Remote. Keyboard and mouse input follow the reverse path.

Use a dedicated Linux machine or VM that can open the NanoKVM URL and reach UU’s service. The Linux host also supports terminal and file access for the signed-in UU account, so keep personal files and unrelated credentials outside that machine or VM. NanoU starts the host with remote assistance, port mapping, and wallpaper uploads disabled.

1. Install `uuremote-server` on the Linux bridge from the [upstream releases](https://github.com/VallierDev/uu-remote-linux/releases). Install Python 3.10+, Chromium or Chrome, FFmpeg with libx264, Xvfb, xauth, Openbox, and xdotool. On Debian, the packages are `python3 chromium ffmpeg xvfb xauth openbox xdotool`. On Ubuntu, Google Chrome can be used where Chromium is packaged as a Snap. Keep this bridge’s UU identity dedicated to NanoU.
2. On the Linux bridge, from this project directory, enroll with a QR code. Scan it in the official UU mobile app using the account you will control the device from.

   ```sh
   python3 -m nanou native-login --kiosk
   ```

3. Start the bridge as a regular Linux user. Replace the example with the same NanoKVM URL that works from that machine.

   ```sh
   python3 -m nanou native --kiosk http://192.168.1.42
   ```

4. Open the official UU app on your phone or computer. Once the Linux host appears online under your devices, open its **Remote Desktop** and sign in to NanoKVM on the displayed page. Press Ctrl+C on the bridge to stop it.

The Linux host runs on the bridge or VM. It does not require installing anything on the NanoKVM firmware or on the computer plugged into NanoKVM. This mode captures the rendered console, so it adds one video encode. Use NanoKVM’s on-screen keyboard menu for combinations intercepted by the viewer or browser. For HTTPS, trust the NanoKVM certificate on the Linux bridge the same way you would for local browser access.

## Use NanoU’s browser controller

This mode uses UU login and signaling, WebRTC data channels, and TCP forwarding. Run NanoU on both ends with separate device identities and the same UU account. Sign-in uses mainland China SMS (+86).

1. Install Python 3.10+ and [uv](https://docs.astral.sh/uv/). From the project directory on each computer, run `uv sync --locked`.
2. On a computer that can reach NanoKVM, run the gateway and sign in when prompted. Replace the example address with your NanoKVM address.

   ```sh
   uv run --locked python -m nanou gateway http://192.168.1.42 --name "NanoU Gateway"
   ```

3. On the controller computer, run `uv run --locked python -m nanou`. Sign in, select your gateway and its HTTP or HTTPS protocol, then select **Connect** and **Open NanoKVM**. Use the NanoKVM account in the console. Select **Disconnect** to close the local console port and tunnel.

The gateway forwards only the configured console destination. The launcher and forwarded port bind to the controller’s loopback address. The tunnel carries HTTP and WebSocket traffic, including TCP-based video modes such as MJPEG. Select MJPEG if your firmware’s WebRTC video mode cannot connect through the tunnel. For HTTPS, the forwarded browser address is `127.0.0.1`. A certificate issued for the device’s LAN name or IP will normally fail hostname verification there. Use the Linux bridge mode for a browser that connects using the original HTTPS origin.

Both modes store separate local UU sign-ins under `~/.nanou`. Set `NANOU_STATE_DIR` to relocate them. NanoU does not need your NanoKVM password on the command line.

## Implementation sources

- The [tunnel protocol notes](upstream/docs/protocol/README.md) describe the browser controller’s custom tunnel format.
- NetEase’s published integration supports launching the mobile client and binding supported Windows/macOS hosts. The official-client mode uses the independent Linux host for session interoperability. [NetEase integration guide](https://github.com/netease-uuyc/uuremote-integration-guide) · [UU Remote Linux](https://github.com/VallierDev/uu-remote-linux)
- The device console and controls are provided by NanoKVM firmware. [Sipeed NanoKVM](https://github.com/sipeed/NanoKVM)

## License

NanoU is licensed under GPL-3.0-or-later, matching the upstream UU host. See [LICENSE](LICENSE) and the [vendored aioice license](vendor/aioice/LICENSE).

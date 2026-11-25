// NanoU forwarding backend for the embedded UU Remote host.
use crate::capture_setting::VideoCodec;
use crate::control_protocol::ControlInput;
use crate::protocol::{decode_wire, WireValue};
use crate::video_track::VideoTrack;
use std::os::unix::net::UnixDatagram;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use tokio::io::AsyncReadExt;
use tokio::net::UnixStream;
use tokio::sync::{watch, Notify};
use webrtc::data_channel::{data_channel_state::RTCDataChannelState, RTCDataChannel};
use webrtc::media::Sample;
use webrtc::peer_connection::{peer_connection_state::RTCPeerConnectionState, RTCPeerConnection};

#[path = "low_latency.rs"]
mod low_latency;
#[path = "h264_dimensions.rs"]
mod h264_dimensions;
use low_latency::{Frame, VideoBuffer, MAX_AGE};

fn dimensions() -> &'static watch::Sender<Option<(u32, u32)>> {
    static DIMENSIONS: OnceLock<watch::Sender<Option<(u32, u32)>>> = OnceLock::new();
    DIMENSIONS.get_or_init(|| watch::channel(None).0)
}

pub fn screen_size() -> Option<(u32, u32)> {
    *dimensions().borrow()
}

pub fn screen_changes() -> watch::Receiver<Option<(u32, u32)>> {
    dimensions().subscribe()
}

pub async fn report_screen_changes(
    channel: Arc<RTCDataChannel>,
    sequence: Arc<std::sync::atomic::AtomicU64>,
    mut sizes: watch::Receiver<Option<(u32, u32)>>,
    settings: Arc<watch::Sender<crate::capture_setting::VideoSettings>>,
) {
    while channel.ready_state() == RTCDataChannelState::Open {
        tokio::select! {
            result = sizes.changed() => if result.is_err() { return; },
            _ = tokio::time::sleep(Duration::from_millis(500)) => continue,
        }
        let Some(size) = *sizes.borrow_and_update() else { continue };
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs();
        let seq = || sequence.fetch_add(1, Ordering::Relaxed);
        let Ok(sources) = crate::capture_setting::screen_sources(seq(), ts, size, Some(size), "NanoKVM") else { return };
        let settings = crate::capture_setting::VideoSettings { output: Some(size), ..*settings.borrow() };
        let sync = crate::initialization::envelope(seq(), ts, 25, &crate::capture_setting::sync_payload(&settings, size));
        for message in [sources, sync] {
            if channel.send(&bytes::Bytes::from(message)).await.is_err() {
                return;
            }
        }
    }
}

pub fn enabled() -> bool {
    std::env::var_os("NANOU_DEVICE_SOCKET_DIR").is_some()
}

fn socket(name: &str) -> Option<PathBuf> {
    std::env::var_os("NANOU_DEVICE_SOCKET_DIR").map(|path| PathBuf::from(path).join(name))
}

fn send(value: serde_json::Value) {
    let Some(path) = socket("input") else { return };
    // Reuse the datagram socket for high-rate pointer input. Keep the UU callback nonblocking.
    static SENDER: OnceLock<Option<UnixDatagram>> = OnceLock::new();
    let sender = SENDER.get_or_init(|| {
        let sender = UnixDatagram::unbound().ok()?;
        sender.set_nonblocking(true).ok()?;
        Some(sender)
    });
    if let Some(sender) = sender {
        let _ = sender.send_to(value.to_string().as_bytes(), path);
    }
}

pub fn reset() {
    send(serde_json::json!({"event":"reset"}));
}

pub fn input(event: ControlInput) {
    use serde_json::json;
    let value = match event {
        ControlInput::Keyboard { key, pressed } => json!({"event":"key","key":key,"down":pressed}),
        ControlInput::KeyboardClick { key } => json!({"event":"key_click","key":key}),
        ControlInput::MouseAbsolute { x, y } => json!({"event":"absolute","x":x,"y":y}),
        ControlInput::MouseRelative { dx, dy } => json!({"event":"relative","x":dx,"y":dy}),
        ControlInput::MouseScroll { dx: _, dy } => json!({"event":"scroll","y":dy}),
        ControlInput::MouseButton { button, pressed } => json!({"event":"button","button":button,"down":pressed}),
        ControlInput::MouseClick { button } => json!({"event":"button_click","button":button}),
        ControlInput::TextInput { text } if text.len() <= 8192 => json!({"event":"text","text":text}),
        _ => return,
    };
    send(value);
}

pub fn harmless_control(data: &[u8]) -> bool {
    // Keep the viewer echo/status exchange. The KVM account grants HID access,
    // never administration of the appliance OS or filesystem.
    let Ok(fields) = decode_wire(data) else { return false };
    fields.iter().all(|field| match (&field.value, field.number) {
        (WireValue::Varint(_), 1 | 2) | (WireValue::Bytes(_), 16) => true,
        (WireValue::Bytes(action), 3) => decode_wire(action).is_ok_and(|action_fields| {
            action_fields.iter().any(|field| matches!((&field.value, field.number), (WireValue::Varint(0), 1)))
            && !action_fields.iter().any(|field| matches!((&field.value, field.number), (WireValue::Varint(1..), 1)))
        }),
        _ => false,
    })
}

async fn read_frame(stream: &mut UnixStream) -> Result<Vec<u8>, String> {
    let len = stream.read_u32().await.map_err(|e| e.to_string())? as usize;
    if !(10..=8 * 1024 * 1024).contains(&len) {
        return Err("Invalid NanoKVM H.264 frame length".into());
    }
    let mut frame = vec![0; len];
    stream.read_exact(&mut frame).await.map_err(|e| e.to_string())?;
    Ok(frame)
}

pub async fn stream(pc: Arc<RTCPeerConnection>, track: Arc<VideoTrack>, idr_request: Arc<AtomicBool>) -> Result<(), String> {
    let path = socket("video").ok_or("NanoKVM media socket is not configured")?;
    while !peer_ended(&pc) {
        if pc.connection_state() != RTCPeerConnectionState::Connected {
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        }
        let relay = async {
            let connection = UnixStream::connect(&path).await.map_err(|e| e.to_string())?;
            relay_video(connection, &track, &idr_request).await
        };
        let disconnected = async {
            while pc.connection_state() == RTCPeerConnectionState::Connected {
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        };
        tokio::select! {
            result = relay => {
                if let Err(error) = result {
                    log::debug!("Reopening NanoKVM video: {error}");
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
            _ = disconnected => {}
        }
    }
    Ok(())
}

async fn relay_video(mut connection: UnixStream, track: &VideoTrack, idr_request: &AtomicBool) -> Result<(), String> {
    let buffer = Mutex::new(VideoBuffer::new());
    let ready = Notify::new();

    // The firmware feed must keep draining even when UU's transport slows down.
    let read = async {
        let mut previous_pts: Option<u64> = None;
        loop {
            let packet = tokio::time::timeout(Duration::from_secs(8), read_frame(&mut connection))
                .await.map_err(|_| "Timed out waiting for NanoKVM H.264 video")??;
            if packet[0] == 1 || screen_size().is_none() {
                if let Some(size) = h264_dimensions::dimensions(&packet[9..]) {
                    dimensions().send_if_modified(|current| {
                        if *current == Some(size) { return false; }
                        *current = Some(size);
                        true
                    });
                }
            }
            let pts = u64::from_le_bytes(packet[1..9].try_into().unwrap());
            let delta = previous_pts.and_then(|previous| pts.checked_sub(previous))
                .filter(|delta| (1_000..=250_000).contains(delta)).unwrap_or(33_333);
            previous_pts = Some(pts);
            let frame = Frame { packet, arrived: Instant::now(), duration: Duration::from_micros(delta) };
            if buffer.lock().unwrap().push(frame) {
                ready.notify_one();
            }
        }
    };

    let write = async {
        let extensions = [crate::rtp_metadata::capture_index(0), crate::rtp_metadata::new_frame()];
        loop {
            let frame = {
                let mut buffer = buffer.lock().unwrap();
                let now = Instant::now();
                // The viewer may have discarded its decoder state while suspended.
                if idr_request.swap(false, Ordering::AcqRel) {
                    buffer.skip_to_keyframe(now);
                }
                buffer.pop(now)
            };
            let Some(frame) = frame else {
                ready.notified().await;
                continue;
            };
            let sample = Sample {
                data: bytes::Bytes::from(frame.packet).slice(9..),
                duration: frame.duration,
                ..Default::default()
            };
            let deadline = tokio::time::Instant::from_std(frame.arrived + MAX_AGE);
            let send = track.write_sample_with_extensions(VideoCodec::H264, &sample, &extensions);
            match tokio::time::timeout_at(deadline, send).await {
                Ok(result) => result.map_err(|e| e.to_string())?,
                Err(_) => buffer.lock().unwrap().skip_to_keyframe(Instant::now()),
            }
        }
    };

    tokio::select! {
        result = read => result,
        result = write => result,
    }
}

pub async fn set_alias(client: &crate::auth::UUAuthClient, device_id: &str) {
    let Ok(name) = std::env::var("NANOU_UU_NAME") else { return };
    if name.is_empty() || device_id.is_empty()
        || !device_id.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
    {
        return;
    }
    let Some(config_dir) = crate::config::path().parent().map(|dir| dir.join("nanou-alias")) else { return };
    if std::fs::read_to_string(&config_dir).is_ok_and(|current| current == name) {
        return;
    }
    let path = format!("/api/v1/device/{device_id}");
    let result = client.put_json(&path, &serde_json::json!({"alias": name})).await;
    if matches!(result, Ok((200, body)) if body.get("code").and_then(|v| v.as_i64()) == Some(0)) {
        let _ = std::fs::write(config_dir, name);
    } else {
        log::warn!("UU rejected the configured NanoU device name");
    }
}

pub fn audio_enabled() -> bool {
    socket("audio").is_some_and(|path| path.exists())
}

fn peer_ended(peer: &RTCPeerConnection) -> bool {
    matches!(peer.connection_state(), RTCPeerConnectionState::Closed | RTCPeerConnectionState::Failed)
}

/// Relay received Opus directly. The firmware and the UU viewer have their own
/// RTP timelines, so the UU track allocates sequence numbers and timestamps.
pub async fn stream_audio(
    viewer: Arc<RTCPeerConnection>,
    output: Arc<webrtc::track::track_local::track_local_static_sample::TrackLocalStaticSample>,
) {
    while viewer.connection_state() != RTCPeerConnectionState::Connected {
        if peer_ended(&viewer) { return; }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    while !peer_ended(&viewer) {
        if let Err(error) = speaker_connection(&viewer, output.clone()).await {
            log::debug!("NanoKVM speaker connection ended: {error}");
        }
        if !peer_ended(&viewer) {
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    }
}

async fn speaker_connection(
    viewer: &RTCPeerConnection,
    output: Arc<webrtc::track::track_local::track_local_static_sample::TrackLocalStaticSample>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use webrtc::api::{media_engine::MediaEngine, APIBuilder};
    use webrtc::ice_transport::ice_candidate::RTCIceCandidateInit;
    use webrtc::peer_connection::{configuration::RTCConfiguration, sdp::session_description::RTCSessionDescription};
    use webrtc::rtp_transceiver::{rtp_codec::RTPCodecType, rtp_transceiver_direction::RTCRtpTransceiverDirection, RTCRtpTransceiverInit};

    let path = socket("audio").ok_or("NanoKVM audio socket is not configured")?;
    let signaling = UnixStream::connect(path).await?;
    let (reader, mut writer) = signaling.into_split();
    let mut messages = BufReader::new(reader).lines();
    let mut media = MediaEngine::default();
    media.register_default_codecs()?;
    let peer = APIBuilder::new().with_media_engine(media).build()
        .new_peer_connection(RTCConfiguration::default()).await?;
    peer.on_track(Box::new(move |track, _, _| {
        let output = output.clone();
        Box::pin(async move {
            if !track.codec().capability.mime_type.eq_ignore_ascii_case("audio/opus") { return; }
            tokio::spawn(async move {
                while let Ok((packet, _)) = track.read_rtp().await {
                    if packet.payload.is_empty() { continue; }
                    let sample = Sample {
                        data: packet.payload,
                        duration: Duration::from_millis(20),
                        ..Default::default()
                    };
                    if output.write_sample(&sample).await.is_err() { break; }
                }
            });
        })
    }));
    let result = async {
        peer.add_transceiver_from_kind(RTPCodecType::Audio, Some(RTCRtpTransceiverInit {
            direction: RTCRtpTransceiverDirection::Recvonly,
            send_encodings: vec![],
        })).await?;
        let mut gathered = peer.gathering_complete_promise().await;
        peer.set_local_description(peer.create_offer(None).await?).await?;
        tokio::time::timeout(Duration::from_secs(5), gathered.recv()).await?;
        let local = peer.local_description().await.ok_or("Missing speaker SDP offer")?;
        let offer = serde_json::json!({"event": "offer", "data": serde_json::to_string(&local)?});
        writer.write_all(offer.to_string().as_bytes()).await?;
        writer.write_all(b"\n").await?;
        let mut pending = Vec::new();
        let mut answered = false;
        let mut health = tokio::time::interval(Duration::from_secs(1));
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            tokio::select! {
                _ = health.tick() => {
                    if peer_ended(viewer) || peer_ended(&peer) { return Ok(()); }
                    if !answered && tokio::time::Instant::now() >= deadline {
                        return Err("NanoKVM did not answer the audio offer".into());
                    }
                }
                line = messages.next_line() => {
                    let Some(line) = line? else { return Ok(()) };
                    let signal: serde_json::Value = serde_json::from_str(&line)?;
                    let body = signal.get("data").and_then(|value| value.as_str()).unwrap_or("{}");
                    match signal.get("event").and_then(|value| value.as_str()) {
                        Some("answer") if !answered => {
                            peer.set_remote_description(serde_json::from_str::<RTCSessionDescription>(body)?).await?;
                            answered = true;
                            for candidate in pending.drain(..) { peer.add_ice_candidate(candidate).await?; }
                        }
                        Some("candidate") => {
                            let payload: serde_json::Value = serde_json::from_str(body)?;
                            if !payload.get("candidate").and_then(|value| value.as_str()).is_some_and(|value| !value.is_empty()) {
                                continue;
                            }
                            let candidate = serde_json::from_value::<RTCIceCandidateInit>(payload)?;
                            if answered {
                                peer.add_ice_candidate(candidate).await?;
                            } else if pending.len() < 32 {
                                pending.push(candidate);
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
    }.await;
    let _ = peer.close().await;
    result
}

//! Bound the H.264 relay backlog without sending delta frames whose references were dropped.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

const MAX_FRAMES: usize = 5;
pub const MAX_AGE: Duration = Duration::from_millis(175);

pub struct Frame {
    pub packet: Vec<u8>,
    pub arrived: Instant,
    pub duration: Duration,
}

impl Frame {
    fn keyframe(&self) -> bool {
        self.packet.first() == Some(&1)
    }

    fn stale(&self, now: Instant) -> bool {
        now.saturating_duration_since(self.arrived) >= MAX_AGE
    }
}

pub struct VideoBuffer {
    frames: VecDeque<Frame>,
    waiting_for_keyframe: bool,
}

impl VideoBuffer {
    pub fn new() -> Self {
        Self { frames: VecDeque::with_capacity(MAX_FRAMES), waiting_for_keyframe: true }
    }

    pub fn push(&mut self, frame: Frame) -> bool {
        if self.frames.len() == MAX_FRAMES {
            self.frames.clear();
            self.waiting_for_keyframe = true;
        } else {
            self.discard_stale(frame.arrived);
        }
        if self.waiting_for_keyframe {
            if !frame.keyframe() { return false; }
            self.waiting_for_keyframe = false;
        }
        self.frames.push_back(frame);
        true
    }

    pub fn pop(&mut self, now: Instant) -> Option<Frame> {
        self.discard_stale(now);
        self.frames.pop_front()
    }

    fn discard_stale(&mut self, now: Instant) {
        if self.frames.front().is_some_and(|frame| frame.stale(now)) {
            self.skip_to_keyframe(now);
        }
    }

    /// A dropped or incomplete frame invalidates later frames in the same GOP.
    pub fn skip_to_keyframe(&mut self, now: Instant) {
        while self.frames.front().is_some_and(|frame| !frame.keyframe() || frame.stale(now)) {
            self.frames.pop_front();
        }
        self.waiting_for_keyframe = self.frames.is_empty();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(arrived: Instant, keyframe: bool) -> Frame {
        Frame { packet: vec![u8::from(keyframe)], arrived, duration: Duration::from_millis(16) }
    }

    #[test]
    fn first_frame_must_be_a_keyframe() {
        let now = Instant::now();
        let mut queue = VideoBuffer::new();
        assert!(!queue.push(frame(now, false)));
        assert!(queue.push(frame(now, true)));
        assert!(queue.push(frame(now, false)));
        assert!(queue.pop(now).unwrap().keyframe());
        assert!(!queue.pop(now).unwrap().keyframe());
    }

    #[test]
    fn overflow_flushes_dependents_and_resumes_on_keyframe() {
        let now = Instant::now();
        let mut queue = VideoBuffer::new();
        assert!(queue.push(frame(now, true)));
        for _ in 1..MAX_FRAMES { assert!(queue.push(frame(now, false))); }
        assert!(!queue.push(frame(now, false)));
        assert!(queue.pop(now).is_none());
        assert!(!queue.push(frame(now, false)));
        assert!(queue.push(frame(now, true)));
        assert!(queue.pop(now).unwrap().keyframe());
    }

    #[test]
    fn drops_fresh_deltas_if_their_keyframe_expired() {
        let now = Instant::now();
        let later = now + MAX_AGE;
        let mut queue = VideoBuffer::new();
        queue.push(frame(now, true));
        queue.push(frame(now + Duration::from_millis(30), false));
        queue.push(frame(now + Duration::from_millis(60), true));
        queue.push(frame(now + Duration::from_millis(90), false));
        assert!(queue.pop(later).unwrap().keyframe());
        assert!(!queue.pop(later).unwrap().keyframe());
    }

    #[test]
    fn interrupted_frame_discards_remaining_deltas() {
        let now = Instant::now();
        let mut queue = VideoBuffer::new();
        queue.push(frame(now, true));
        assert!(queue.pop(now).unwrap().keyframe());
        queue.push(frame(now, false));
        queue.skip_to_keyframe(now);
        assert!(queue.pop(now).is_none());
        assert!(!queue.push(frame(now, false)));
        assert!(queue.push(frame(now, true)));
    }
}

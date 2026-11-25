// Read display dimensions from an Annex B H.264 sequence parameter set.
pub fn dimensions(frame: &[u8]) -> Option<(u32, u32)> {
    let mut starts = frame.windows(3).enumerate().filter_map(|(i, b)| (b == [0, 0, 1]).then_some(i));
    let mut start = starts.next()?;
    for end in starts.chain(std::iter::once(frame.len())) {
        let nal = &frame[start + 3..end];
        if nal.first().is_some_and(|header| header & 31 == 7) && nal.len() <= 4096 {
            if let Some(size) = sps_dimensions(&nal[1..]) {
                return Some(size);
            }
        }
        start = end;
    }
    None
}

struct Bits {
    data: Vec<u8>,
    offset: usize,
}

impl Bits {
    fn read(&mut self, count: usize) -> Option<u32> {
        let mut value = 0;
        for _ in 0..count {
            let byte = *self.data.get(self.offset / 8)?;
            value = (value << 1) | u32::from((byte >> (7 - self.offset % 8)) & 1);
            self.offset += 1;
        }
        Some(value)
    }

    fn unsigned(&mut self) -> Option<u32> {
        let mut zeros = 0;
        while self.read(1)? == 0 {
            zeros += 1;
            if zeros == 32 {
                return None;
            }
        }
        Some((1 << zeros) - 1 + self.read(zeros)?)
    }

    fn signed(&mut self) -> Option<i64> {
        let value = i64::from(self.unsigned()?);
        Some(if value % 2 == 0 { -value / 2 } else { (value + 1) / 2 })
    }

    fn scaling_list(&mut self, count: usize) -> Option<()> {
        let (mut last, mut next) = (8, 8);
        for _ in 0..count {
            if next != 0 {
                next = (last + self.signed()?).rem_euclid(256);
            }
            if next != 0 {
                last = next;
            }
        }
        Some(())
    }
}

fn sps_dimensions(nal: &[u8]) -> Option<(u32, u32)> {
    let mut bits = Bits { data: Vec::with_capacity(nal.len()), offset: 0 };
    let mut zeros = 0;
    for &byte in nal {
        if zeros == 2 && byte == 3 {
            zeros = 0;
            continue;
        }
        bits.data.push(byte);
        zeros = if byte == 0 { zeros + 1 } else { 0 };
    }
    let profile = bits.read(8)?;
    bits.read(16)?; // constraint flags and level
    bits.unsigned()?; // SPS id
    let mut chroma = 1;
    let mut separate_colour_plane = false;
    if matches!(profile, 44 | 83 | 86 | 100 | 110 | 118 | 122 | 128 | 134 | 135 | 138 | 139 | 144 | 244) {
        chroma = bits.unsigned()?;
        if chroma > 3 { return None; }
        separate_colour_plane = chroma == 3 && bits.read(1)? != 0;
        bits.unsigned()?; // luma and chroma bit depth
        bits.unsigned()?;
        bits.read(1)?; // transform bypass
        if bits.read(1)? != 0 {
            for index in 0..if chroma == 3 { 12 } else { 8 } {
                if bits.read(1)? != 0 {
                    bits.scaling_list(if index < 6 { 16 } else { 64 })?;
                }
            }
        }
    }
    bits.unsigned()?; // frame_num
    match bits.unsigned()? { // picture order count type
        0 => { bits.unsigned()?; }
        1 => {
            bits.read(1)?;
            bits.signed()?;
            bits.signed()?;
            let offsets = bits.unsigned()?;
            if offsets > 255 { return None; }
            for _ in 0..offsets { bits.signed()?; }
        }
        2 => {}
        _ => return None,
    }
    bits.unsigned()?; // maximum reference frames
    bits.read(1)?; // gaps allowed
    let width = bits.unsigned()?.checked_add(1)?.checked_mul(16)?;
    let height_units = bits.unsigned()?.checked_add(1)?;
    let frame_only = bits.read(1)? != 0;
    let fields = if frame_only { 1 } else { 2 };
    if !frame_only { bits.read(1)?; }
    let height = height_units.checked_mul(16 * fields)?;
    bits.read(1)?; // direct inference
    let (width, height) = if bits.read(1)? == 0 {
        (width, height)
    } else {
        let (x_unit, y_unit) = match if separate_colour_plane { 0 } else { chroma } {
            1 => (2, 2 * fields),
            2 => (2, fields),
            _ => (1, fields),
        };
        let crop_x = bits.unsigned()?.checked_add(bits.unsigned()?)?.checked_mul(x_unit)?;
        let crop_y = bits.unsigned()?.checked_add(bits.unsigned()?)?.checked_mul(y_unit)?;
        (width.checked_sub(crop_x)?, height.checked_sub(crop_y)?)
    };
    ((1..=8192).contains(&width) && (1..=8192).contains(&height)).then_some((width, height))
}

#[cfg(test)]
mod tests {
    use super::dimensions;

    #[test]
    fn reads_cropped_portrait_and_ultrawide_frames() {
        let portrait = [
            0, 0, 0, 1, 0x67, 0x64, 0x00, 0x32, 0xac, 0xd9, 0x40, 0x4a, 0x01, 0x3f,
            0xe2, 0x27, 0xc0, 0x44, 0, 0, 3, 0, 4, 0, 0, 3, 0, 8, 0x3c, 0x60, 0xc6, 0x58,
            0, 0, 1, 0x68, 0xff,
        ];
        let ultrawide = [
            0, 0, 1, 0x67, 0x64, 0x00, 0x32, 0xac, 0xd9, 0x40, 0x35, 0xc0, 0xb5, 0xb0,
            0x11, 0, 0, 3, 0, 1, 0, 0, 3, 0, 2, 0x0f, 0x18, 0x31, 0x96,
        ];
        assert_eq!(dimensions(&portrait), Some((1170, 2532)));
        assert_eq!(dimensions(&ultrawide), Some((3440, 1440)));
        assert_eq!(dimensions(&portrait[..12]), None);
        assert_eq!(dimensions(&[0, 0, 1, 0x65, 0xff]), None);
    }
}

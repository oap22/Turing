//! Coalesce PTY reads before JSON/IPC, with fixed latency and bounded buffering.
use std::io::Read;
use std::sync::mpsc::{sync_channel, Receiver};
use std::time::{Duration, Instant};

const READ_BYTES: usize = 4096;
const BATCH_BYTES: usize = 64 * 1024;
const WINDOW: Duration = Duration::from_millis(1);

fn next_batch(rx: &Receiver<Vec<u8>>) -> Option<Vec<u8>> {
    let mut batch = rx.recv().ok()?;
    let deadline = Instant::now() + WINDOW;
    while batch.len() < BATCH_BYTES {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match rx.recv_timeout(remaining) {
            Ok(bytes) => batch.extend_from_slice(&bytes),
            Err(_) => break,
        }
    }
    Some(batch)
}

pub fn stream(mut reader: impl Read + Send + 'static, mut emit: impl FnMut(&[u8])) {
    // At most 16 reads queued (64 KiB), plus one batch and the active read.
    // Backpressure reaches the PTY rather than accumulating an unbounded queue.
    let (tx, rx) = sync_channel(16);
    std::thread::spawn(move || {
        let mut bytes = [0; READ_BYTES];
        loop {
            match reader.read(&mut bytes) {
                Ok(0) => break,
                Ok(n) => {
                    if tx.send(bytes[..n].to_vec()).is_err() {
                        break;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
    });
    // Fixed deadline: a continuous writer cannot keep postponing a batch.
    // Dropping the sender drains its remaining output before returning EOF.
    while let Some(batch) = next_batch(&rx) {
        emit(&batch);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn arbitrary_utf8_bytes_and_tail_survive_batching() {
        let bytes = "λ😀\u{e0b0}\n".repeat(100_000).into_bytes();
        let mut received = Vec::new();
        let mut largest = 0;
        stream(std::io::Cursor::new(bytes.clone()), |batch| {
            largest = largest.max(batch.len());
            received.extend_from_slice(batch);
        });
        assert_eq!(received, bytes);
        assert!(largest < BATCH_BYTES + READ_BYTES);
    }
    #[test]
    fn lone_chunk_delivered_while_sender_is_still_alive() {
        let (tx, rx) = sync_channel(1);
        tx.send(vec![b'k']).unwrap();
        let start = Instant::now();
        assert_eq!(next_batch(&rx), Some(vec![b'k']));
        assert!(start.elapsed() < Duration::from_millis(100));
        drop(tx);
        assert_eq!(next_batch(&rx), None);
    }
}

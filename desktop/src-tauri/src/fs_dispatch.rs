//! Bounded wakeups and one notification per changed path per frame. Unlike a
//! leading-edge throttle, a final write is always delivered, even during a burst.
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::{mpsc::sync_channel, Arc, Mutex};
use std::time::Duration;

pub fn coalesce(mut emit: impl FnMut(PathBuf) + Send + 'static) -> impl Fn(PathBuf) + Send {
    let pending = Arc::new(Mutex::new(HashSet::new()));
    let paths = Arc::clone(&pending);
    let (wake, receive) = sync_channel(1);
    std::thread::spawn(move || {
        while receive.recv().is_ok() {
            // Fixed window, never extended by later writes. Continuous output
            // therefore cannot starve delivery. No timer runs while idle.
            std::thread::sleep(Duration::from_millis(16));
            let batch = std::mem::take(&mut *paths.lock().unwrap());
            for path in batch {
                emit(path);
            }
        }
    });
    move |path| {
        pending.lock().unwrap().insert(path);
        // A full channel already promises a wakeup. Paths live in the set,
        // not the channel, so coalescing wakeups cannot lose a final change.
        let _ = wake.try_send(());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::channel;
    use std::time::Instant;

    #[test]
    fn final_write_is_delivered_without_another_event() {
        let (send, receive) = channel();
        let dispatch = coalesce(move |p| {
            send.send(p).unwrap();
        });
        let path = PathBuf::from("metrics.jsonl");
        dispatch(path.clone());
        assert_eq!(receive.recv_timeout(Duration::from_secs(1)).unwrap(), path);
        let start = Instant::now();
        dispatch(path.clone());
        assert_eq!(receive.recv_timeout(Duration::from_secs(1)).unwrap(), path);
        assert!(start.elapsed() < Duration::from_millis(300));
    }

    #[test]
    fn burst_coalesces_per_path_and_drains_on_shutdown() {
        let (send, receive) = channel();
        let dispatch = coalesce(move |p| {
            send.send(p).unwrap();
        });
        for _ in 0..100 {
            dispatch(PathBuf::from("a"));
            dispatch(PathBuf::from("b"));
        }
        drop(dispatch);
        let mut paths: Vec<_> = receive.iter().collect();
        paths.sort();
        assert_eq!(paths, vec![PathBuf::from("a"), PathBuf::from("b")]);
    }
}

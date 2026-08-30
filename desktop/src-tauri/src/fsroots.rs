// Root-scoped filesystem access for the desktop frontend: every command here
// resolves `(root_id, rel_path)` through `resolve()`, which refuses to leave
// the configured root (absolute paths, `..` components, and symlink escapes
// are all rejected). This is the only path by which the webview touches the
// filesystem.

use base64::Engine as _;
use notify::{
    Config as NotifyConfig, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher,
};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

use crate::config::AppConfig;

const SKIP_DIRS: &[&str] = &["node_modules", "target", "__pycache__"];
const DEFAULT_MAX_ENTRIES: u32 = 5000;
const DEFAULT_MAX_TEXT_BYTES: u64 = 2_000_000;
const MAX_BINARY_BYTES: u64 = 25 * 1024 * 1024;
const MAX_DEPTH: u32 = 8;
const DEBOUNCE_MS: u64 = 300;

pub struct Roots(pub HashMap<String, PathBuf>);

impl Roots {
    pub fn from_config(cfg: &AppConfig) -> Self {
        let mut map = HashMap::new();
        for root in &cfg.roots {
            let raw = PathBuf::from(&root.path);
            let path = raw.canonicalize().unwrap_or(raw);
            map.insert(root.id.clone(), path);
        }
        Roots(map)
    }

    /// Resolve `rel` against `root`, refusing to leave the root's canonical
    /// directory. `rel == ""` resolves to the root itself.
    pub fn resolve(&self, root: &str, rel: &str) -> Result<PathBuf, String> {
        let root_path = self
            .0
            .get(root)
            .ok_or_else(|| format!("unknown root: {root}"))?;
        let rel_path = Path::new(rel);
        if rel_path.is_absolute() {
            return Err("absolute paths are not allowed".to_string());
        }
        if rel_path
            .components()
            .any(|c| matches!(c, Component::ParentDir))
        {
            return Err("parent-directory components are not allowed".to_string());
        }
        let joined = if rel.is_empty() {
            root_path.clone()
        } else {
            root_path.join(rel_path)
        };
        let canonical_root = root_path
            .canonicalize()
            .map_err(|e| format!("root does not exist: {e}"))?;
        let canonical = joined
            .canonicalize()
            .map_err(|e| format!("path does not exist: {e}"))?;
        if !canonical.starts_with(&canonical_root) {
            return Err("path escapes root".to_string());
        }
        Ok(canonical)
    }
}

#[derive(Default)]
pub struct WatchState {
    watcher: Mutex<Option<RecommendedWatcher>>,
    watched: Mutex<HashSet<PathBuf>>,
}

#[derive(Serialize)]
pub struct Entry {
    pub rel_path: String,
    pub is_dir: bool,
    pub size: u64,
    pub mtime_ms: u64,
}

fn mtime_ms(meta: &fs::Metadata) -> u64 {
    meta.modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn should_skip(name: &str) -> bool {
    name.starts_with('.') || SKIP_DIRS.contains(&name)
}

fn walk(
    base: &Path,
    dir: &Path,
    depth: u32,
    exts: &Option<Vec<String>>,
    max: u32,
    out: &mut Vec<Entry>,
) {
    if depth > MAX_DEPTH || out.len() as u32 >= max {
        return;
    }
    let Ok(read_dir) = fs::read_dir(dir) else {
        return;
    };
    for entry in read_dir.flatten() {
        if out.len() as u32 >= max {
            return;
        }
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if should_skip(&name) {
            continue;
        }
        let path = entry.path();
        let Ok(meta) = entry.metadata() else { continue };
        if meta.is_dir() {
            if exts.is_none() {
                if let Ok(rel) = path.strip_prefix(base) {
                    out.push(Entry {
                        rel_path: rel.to_string_lossy().replace('\\', "/"),
                        is_dir: true,
                        size: 0,
                        mtime_ms: mtime_ms(&meta),
                    });
                }
            }
            walk(base, &path, depth + 1, exts, max, out);
        } else {
            let keep = match exts {
                None => true,
                Some(list) => path
                    .extension()
                    .map(|e| {
                        list.iter()
                            .any(|want| want.to_lowercase() == e.to_string_lossy().to_lowercase())
                    })
                    .unwrap_or(false),
            };
            if !keep {
                continue;
            }
            if let Ok(rel) = path.strip_prefix(base) {
                out.push(Entry {
                    rel_path: rel.to_string_lossy().replace('\\', "/"),
                    is_dir: false,
                    size: meta.len(),
                    mtime_ms: mtime_ms(&meta),
                });
            }
        }
    }
}

#[tauri::command]
pub fn fs_list(
    roots: tauri::State<'_, Roots>,
    root: String,
    rel: String,
    exts: Option<Vec<String>>,
    max: Option<u32>,
) -> Result<Vec<Entry>, String> {
    let base_full = roots.resolve(&root, &rel)?;
    let max = max.unwrap_or(DEFAULT_MAX_ENTRIES);
    let mut out = Vec::new();
    walk(&base_full, &base_full, 0, &exts, max, &mut out);
    out.sort_by_key(|e| std::cmp::Reverse(e.mtime_ms));
    Ok(out)
}

fn read_text_impl(path: &Path, max_bytes: u64) -> Result<String, String> {
    let meta = fs::metadata(path).map_err(|e| e.to_string())?;
    let len = meta.len();
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    if len > max_bytes {
        file.seek(SeekFrom::Start(len - max_bytes))
            .map_err(|e| e.to_string())?;
    }
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).map_err(|e| e.to_string())?;
    Ok(String::from_utf8_lossy(&buf).into_owned())
}

#[tauri::command]
pub fn fs_read_text(
    roots: tauri::State<'_, Roots>,
    root: String,
    rel: String,
    max_bytes: Option<u64>,
) -> Result<String, String> {
    let path = roots.resolve(&root, &rel)?;
    read_text_impl(&path, max_bytes.unwrap_or(DEFAULT_MAX_TEXT_BYTES))
}

#[tauri::command]
pub fn fs_read_binary(
    roots: tauri::State<'_, Roots>,
    root: String,
    rel: String,
) -> Result<String, String> {
    let path = roots.resolve(&root, &rel)?;
    let meta = fs::metadata(&path).map_err(|e| e.to_string())?;
    if meta.len() > MAX_BINARY_BYTES {
        return Err("file exceeds 25 MB cap".to_string());
    }
    let data = fs::read(&path).map_err(|e| e.to_string())?;
    Ok(base64::engine::general_purpose::STANDARD.encode(data))
}

/// One `fs_tail` read. Every byte offset the frontend needs is computed here,
/// on the bytes, so the webview never does byte arithmetic on a `String`
/// (`from_utf8_lossy` can change the length, and JS string length is UTF-16
/// code units either way).
///
/// The contract a line-oriented consumer (the metrics pane) relies on:
///
/// * `data` is the bytes `[start, offset)` of the file — **whole lines only**.
///   A trailing partial line is not consumed: `data` is cut at the last `\n`
///   and `offset` stops right after it, so the fragment is re-read, complete,
///   by the next call. A non-empty read with no `\n` at all returns empty
///   `data` and `offset == start`.
/// * `start` is where the read actually began: the caller's offset, or `0`
///   when the file is shorter than that offset (`restarted`). A caller that
///   holds an offset applies a chunk only if `start` equals it — any other
///   `start` is a duplicate or stale response and is not this file's next
///   bytes.
/// * `dev`/`ino` identify the file the bytes came from (unix; `None`
///   elsewhere). A run file rotated away and re-created at the same path is a
///   different inode even when the new bytes are at least as long as the old
///   ones, which no offset check can see.
#[derive(Serialize)]
pub struct TailChunk {
    pub data: String,
    pub offset: u64,
    pub start: u64,
    pub dev: Option<u64>,
    pub ino: Option<u64>,
    pub restarted: bool,
}

#[cfg(unix)]
fn file_identity(meta: &fs::Metadata) -> (Option<u64>, Option<u64>) {
    use std::os::unix::fs::MetadataExt;
    (Some(meta.dev()), Some(meta.ino()))
}

#[cfg(not(unix))]
fn file_identity(_meta: &fs::Metadata) -> (Option<u64>, Option<u64>) {
    (None, None)
}

fn tail_impl(path: &Path, offset: u64) -> Result<TailChunk, String> {
    // Open first, then fstat the open handle: `len`, `dev`, `ino` and the
    // bytes all come from the same file, so a rotation between a stat and an
    // open cannot label a new file's bytes with the old file's identity.
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    let meta = file.metadata().map_err(|e| e.to_string())?;
    let len = meta.len();
    let restarted = offset > len;
    let start = if restarted { 0 } else { offset };
    let (dev, ino) = file_identity(&meta);
    file.seek(SeekFrom::Start(start))
        .map_err(|e| e.to_string())?;
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).map_err(|e| e.to_string())?;
    // Hold back a trailing partial line: keep bytes up to and including the
    // last `\n`; with no `\n` at all, keep nothing.
    let keep = buf.iter().rposition(|&b| b == b'\n').map_or(0, |i| i + 1);
    buf.truncate(keep);
    let new_offset = start + keep as u64;
    Ok(TailChunk {
        data: String::from_utf8_lossy(&buf).into_owned(),
        offset: new_offset,
        start,
        dev,
        ino,
        restarted,
    })
}

#[tauri::command]
pub fn fs_tail(
    roots: tauri::State<'_, Roots>,
    root: String,
    rel: String,
    offset: u64,
) -> Result<TailChunk, String> {
    let path = roots.resolve(&root, &rel)?;
    tail_impl(&path, offset)
}

#[tauri::command]
pub fn fs_open_external(
    roots: tauri::State<'_, Roots>,
    root: String,
    rel: String,
) -> Result<(), String> {
    let path = roots.resolve(&root, &rel)?;
    open::that(path).map_err(|e| e.to_string())
}

#[derive(Clone, Serialize)]
struct FsChangePayload {
    root: String,
    rel_path: String,
}

#[tauri::command]
pub fn fs_watch(
    app: AppHandle,
    roots: tauri::State<'_, Roots>,
    watch: tauri::State<'_, WatchState>,
    root: String,
    rel: String,
) -> Result<(), String> {
    let dir = roots.resolve(&root, &rel)?;
    {
        let mut watched = watch.watched.lock().unwrap();
        if watched.contains(&dir) {
            return Ok(());
        }
        watched.insert(dir.clone());
    }

    let mut guard = watch.watcher.lock().unwrap();
    if guard.is_none() {
        let app_for_handler = app.clone();
        // Snapshot roots for rel-path resolution inside the watcher thread —
        // the map is immutable after startup.
        let roots_snapshot: HashMap<String, PathBuf> = roots.0.clone();
        let debounce = std::sync::Arc::new(Mutex::new(HashMap::<PathBuf, Instant>::new()));
        let handler = move |res: notify::Result<Event>| {
            let Ok(event) = res else { return };
            let is_relevant = matches!(event.kind, EventKind::Create(_) | EventKind::Modify(_));
            if !is_relevant {
                return;
            }
            for path in event.paths {
                if !path.is_file() {
                    continue;
                }
                {
                    let mut db = debounce.lock().unwrap();
                    let now = Instant::now();
                    if let Some(last) = db.get(&path) {
                        if now.duration_since(*last) < Duration::from_millis(DEBOUNCE_MS) {
                            continue;
                        }
                    }
                    db.insert(path.clone(), now);
                }
                for (root_id, root_path) in roots_snapshot.iter() {
                    if let Ok(rel_path) = path.strip_prefix(root_path) {
                        let payload = FsChangePayload {
                            root: root_id.clone(),
                            rel_path: rel_path.to_string_lossy().replace('\\', "/"),
                        };
                        let _ = app_for_handler.emit("fs-change", payload);
                        break;
                    }
                }
            }
        };
        let watcher =
            RecommendedWatcher::new(handler, NotifyConfig::default()).map_err(|e| e.to_string())?;
        *guard = Some(watcher);
    }
    if let Some(w) = guard.as_mut() {
        w.watch(&dir, RecursiveMode::Recursive)
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn roots_for(dir: &Path) -> Roots {
        let mut map = HashMap::new();
        map.insert("r".to_string(), dir.canonicalize().unwrap());
        Roots(map)
    }

    #[test]
    fn rejects_parent_dir_component() {
        let dir = tempfile::tempdir().unwrap();
        let roots = roots_for(dir.path());
        assert!(roots.resolve("r", "../escape").is_err());
    }

    #[test]
    fn rejects_absolute_path() {
        let dir = tempfile::tempdir().unwrap();
        let roots = roots_for(dir.path());
        assert!(roots.resolve("r", "/etc/passwd").is_err());
    }

    #[test]
    fn rejects_symlink_escaping_root() {
        let dir = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let link = dir.path().join("escape-link");
        #[cfg(unix)]
        std::os::unix::fs::symlink(outside.path(), &link).unwrap();
        let roots = roots_for(dir.path());
        #[cfg(unix)]
        assert!(roots.resolve("r", "escape-link").is_err());
    }

    #[test]
    fn accepts_plain_nested_path() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("sub")).unwrap();
        fs::write(dir.path().join("sub/file.txt"), b"hi").unwrap();
        let roots = roots_for(dir.path());
        let resolved = roots.resolve("r", "sub/file.txt").unwrap();
        assert!(resolved.ends_with("sub/file.txt"));
    }

    #[test]
    fn rejects_unknown_root() {
        let dir = tempfile::tempdir().unwrap();
        let roots = roots_for(dir.path());
        assert!(roots.resolve("nope", "").is_err());
    }

    fn append(path: &Path, text: &str) {
        let mut f = fs::OpenOptions::new().append(true).open(path).unwrap();
        write!(f, "{text}").unwrap();
    }

    #[test]
    fn tail_offsets_advance_on_append() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log.txt");
        fs::write(&path, "hello\n").unwrap();
        let chunk1 = tail_impl(&path, 0).unwrap();
        assert_eq!(chunk1.data, "hello\n");
        assert_eq!(chunk1.start, 0);
        assert_eq!(chunk1.offset, 6);
        assert!(!chunk1.restarted);
        append(&path, "world\n");
        let chunk2 = tail_impl(&path, chunk1.offset).unwrap();
        assert_eq!(chunk2.data, "world\n");
        assert_eq!(chunk2.start, 6);
        assert_eq!(chunk2.offset, 12);
        assert!(!chunk2.restarted);
        assert_eq!(chunk2.ino, chunk1.ino);
        assert_eq!(chunk2.dev, chunk1.dev);
        #[cfg(unix)]
        assert!(chunk1.ino.is_some() && chunk1.dev.is_some());
        // Nothing new: an empty chunk starting and ending where the caller is.
        let chunk3 = tail_impl(&path, chunk2.offset).unwrap();
        assert_eq!(chunk3.data, "");
        assert_eq!(chunk3.start, 12);
        assert_eq!(chunk3.offset, 12);
        assert!(!chunk3.restarted);
    }

    #[test]
    fn tail_restarts_on_truncate() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log2.txt");
        fs::write(&path, "0123456789\n").unwrap();
        let first = tail_impl(&path, 0).unwrap();
        let offset = first.offset + 100;
        // Truncate in place to a shorter file — same inode, stale offset — and
        // the read must restart from 0 and say so.
        fs::write(&path, "new\n").unwrap();
        let chunk = tail_impl(&path, offset).unwrap();
        assert_eq!(chunk.data, "new\n");
        assert_eq!(chunk.start, 0);
        assert_eq!(chunk.offset, 4);
        assert!(chunk.restarted);
        assert_eq!(chunk.ino, first.ino);
    }

    #[test]
    fn tail_restarted_boundary_is_strictly_past_the_end() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log2b.txt");
        fs::write(&path, "abc\n").unwrap();
        // offset == len: caught up, not restarted; nothing to read.
        let at_end = tail_impl(&path, 4).unwrap();
        assert!(!at_end.restarted);
        assert_eq!(
            (at_end.start, at_end.offset, at_end.data.as_str()),
            (4, 4, "")
        );
        // offset == len + 1: one past the end is a shorter file — restart.
        let past = tail_impl(&path, 5).unwrap();
        assert!(past.restarted);
        assert_eq!(
            (past.start, past.offset, past.data.as_str()),
            (0, 4, "abc\n")
        );
    }

    #[test]
    fn tail_reports_a_new_inode_when_the_file_is_replaced_by_a_longer_one() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log3.txt");
        fs::write(&path, "one\n").unwrap();
        let first = tail_impl(&path, 0).unwrap();
        assert_eq!(first.offset, 4);
        // Rotate the file away and create a fresh one at the same path whose
        // bytes are at least as long as the caller's offset: no restart is
        // visible from lengths alone, so identity is what tells the caller.
        fs::rename(&path, dir.path().join("prior-log3.txt")).unwrap();
        fs::write(&path, "alpha\nbeta\n").unwrap();
        let chunk = tail_impl(&path, first.offset).unwrap();
        assert!(!chunk.restarted);
        assert_eq!(chunk.start, 4);
        // Bytes 4.. of the new file, cut at whole lines: "a\nbeta\n".
        assert_eq!(chunk.data, "a\nbeta\n");
        assert_eq!(chunk.offset, 11);
        #[cfg(unix)]
        {
            assert!(chunk.ino.is_some());
            assert_ne!(chunk.ino, first.ino);
        }
    }

    #[test]
    fn tail_holds_back_a_trailing_partial_line() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log4.txt");
        fs::write(&path, "{\"step\":1}\n{\"step\":2").unwrap();
        let chunk1 = tail_impl(&path, 0).unwrap();
        assert_eq!(chunk1.data, "{\"step\":1}\n");
        assert_eq!(chunk1.start, 0);
        // Stops right after the last `\n`, before the fragment.
        assert_eq!(chunk1.offset, 11);
        // The fragment alone, still incomplete: nothing is consumed.
        let chunk2 = tail_impl(&path, chunk1.offset).unwrap();
        assert_eq!(chunk2.data, "");
        assert_eq!(chunk2.start, 11);
        assert_eq!(chunk2.offset, 11);
        assert!(!chunk2.restarted);
        // The writer finishes the line: the next read returns it whole.
        append(&path, "}\n");
        let chunk3 = tail_impl(&path, chunk2.offset).unwrap();
        assert_eq!(chunk3.data, "{\"step\":2}\n");
        assert_eq!(chunk3.start, 11);
        assert_eq!(chunk3.offset, 22);
    }
}

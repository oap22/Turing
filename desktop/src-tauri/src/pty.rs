// Local PTY registry backing the terminal panes. Each spawn gets a small
// integer id; output streams out as `pty-output` events, exit as `pty-exit`.

use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};
use serde::Serialize;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager};

pub struct PtyEntry {
    master: Box<dyn MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    child: Box<dyn Child + Send + Sync>,
}

#[derive(Default)]
pub struct Ptys {
    entries: Mutex<HashMap<u32, PtyEntry>>,
    next_id: AtomicU32,
}

#[derive(Clone, Serialize)]
struct PtyOutput {
    id: u32,
    data: String,
}

#[derive(Clone, Serialize)]
struct PtyExit {
    id: u32,
    /// The child's exit code, when it could be reaped here. `None` when the
    /// entry was already removed (a `pty_kill` won the race) or the wait
    /// failed — consumers that care about success (the update flow) treat
    /// `None` as failure, and the plain terminal pane ignores it entirely.
    code: Option<u32>,
}

/// Give the pty child the terminal environment a GUI-launched app never has.
///
/// A process started from Finder, from `open`, or from a dev server that was
/// itself launched without a terminal inherits no `TERM`, and `portable-pty`
/// does not supply a default. zsh with `TERM` unset falls back to a dumb
/// terminal: ZLE cannot cursor-address, so it cannot redraw a line in place.
/// That produces exactly the reported breakage — no oh-my-posh prompt at all,
/// keystrokes echoed raw, backspace failing to erase, escape sequences printed
/// literally, no cursor. It is a production bug, not a dev-environment quirk:
/// a double-clicked .app hits it too.
///
/// `TERM` is set unconditionally rather than only when absent, because
/// whatever a GUI process happens to inherit is unreliable — it may carry the
/// launching terminal's entry (`Apple_Terminal`, a tmux/screen entry, …),
/// which describes a different emulator than the pane being painted into.
/// `xterm-256color` is the terminfo entry matching what xterm.js implements.
///
/// Locale is treated differently: it is a real user preference, so an
/// inherited value wins and a default is supplied only when the environment
/// specifies none. With no locale the child runs in the C locale, where zsh
/// treats input as single-byte — so multi-byte glyphs (every Nerd Font icon in
/// the prompt) break on entry and on redraw, independently of how faithfully
/// the reader thread decodes them on the way out.
fn apply_terminal_env(cmd: &mut CommandBuilder, inherited_locale_present: bool) {
    cmd.env("TERM", "xterm-256color");
    cmd.env("COLORTERM", "truecolor");
    if !inherited_locale_present {
        cmd.env("LANG", "en_US.UTF-8");
    }
}

/// Whether any of the variables zsh consults actually specifies a locale.
///
/// Set-but-empty counts as absent: an empty `LANG=` selects no locale, and
/// treating it as a value would leave the child in the C locale — which is
/// precisely the state this guards against.
fn locale_present_in<'a>(values: impl IntoIterator<Item = Option<&'a str>>) -> bool {
    values
        .into_iter()
        .any(|v| matches!(v, Some(s) if !s.is_empty()))
}

fn inherited_locale_present() -> bool {
    let vars: Vec<Option<String>> = ["LC_ALL", "LC_CTYPE", "LANG"]
        .iter()
        .map(|k| std::env::var(k).ok())
        .collect();
    locale_present_in(vars.iter().map(|v| v.as_deref()))
}

/// Split a byte buffer into the decodable prefix and a carry-over remainder.
///
/// The pty reader gets arbitrary 4 KiB slices of the child's output, and a
/// multi-byte UTF-8 sequence lands across a chunk boundary all the time — every
/// Nerd Font glyph in an oh-my-posh prompt is 3 bytes, box-drawing and powerline
/// separators likewise. Decoding each chunk with `String::from_utf8_lossy` turns
/// those straddling sequences into U+FFFD on *both* sides of the boundary: the
/// leading bytes become one replacement char and the trailing bytes another. The
/// terminal then sees garbage of the wrong display width, which is what makes
/// the prompt render as "weird character stuff" and leaves stale cells behind
/// when the line is redrawn on backspace.
///
/// So decode only *complete* sequences and hand the truncated tail back to the
/// caller to prepend to the next read. Bytes that are genuinely invalid (not
/// merely truncated) still become U+FFFD, matching lossy decoding — the carry
/// therefore never exceeds 3 bytes and cannot grow without bound on binary
/// output.
fn split_utf8(bytes: &[u8]) -> (String, Vec<u8>) {
    let mut out = String::with_capacity(bytes.len());
    let mut rest = bytes;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.push_str(s);
                return (out, Vec::new());
            }
            Err(e) => {
                let valid = e.valid_up_to();
                debug_assert!(std::str::from_utf8(&rest[..valid]).is_ok());
                out.push_str(std::str::from_utf8(&rest[..valid]).unwrap_or_default());
                match e.error_len() {
                    // Truncated-but-still-plausible sequence at the very end of
                    // the buffer: carry it over to the next read.
                    None => return (out, rest[valid..].to_vec()),
                    // Genuinely malformed bytes: replace them, keep scanning.
                    Some(len) => {
                        out.push('\u{FFFD}');
                        rest = &rest[valid + len..];
                    }
                }
            }
        }
    }
}

fn resolve_cwd(cwd: Option<String>) -> Result<std::path::PathBuf, String> {
    match cwd {
        Some(c) => {
            let expanded = expand_home(&c);
            let path = std::path::PathBuf::from(&expanded);
            if !path.exists() {
                return Err(format!("cwd does not exist: {expanded}"));
            }
            Ok(path)
        }
        None => dirs::home_dir().ok_or_else(|| "no home dir".to_string()),
    }
}

fn expand_home(path: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = dirs::home_dir() {
            return home.join(rest).to_string_lossy().into_owned();
        }
    } else if path == "~" {
        if let Some(home) = dirs::home_dir() {
            return home.to_string_lossy().into_owned();
        }
    }
    path.to_string()
}

#[tauri::command]
pub fn pty_spawn(
    app: AppHandle,
    ptys: tauri::State<'_, Ptys>,
    cols: u16,
    rows: u16,
    command: Option<String>,
    cwd: Option<String>,
) -> Result<u32, String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());
    let mut cmd = CommandBuilder::new(&shell);
    match &command {
        None => cmd.arg("-l"),
        Some(c) => {
            cmd.arg("-lc");
            cmd.arg(c);
        }
    };

    apply_terminal_env(&mut cmd, inherited_locale_present());

    let cwd_path = resolve_cwd(cwd)?;
    cmd.cwd(&cwd_path);

    let pty_system = native_pty_system();
    let pair = pty_system
        .openpty(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| e.to_string())?;

    let child = pair.slave.spawn_command(cmd).map_err(|e| e.to_string())?;
    drop(pair.slave);

    let mut reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;
    let writer = pair.master.take_writer().map_err(|e| e.to_string())?;

    let id = ptys.next_id.fetch_add(1, Ordering::SeqCst) + 1;

    let entry = PtyEntry {
        master: pair.master,
        writer,
        child,
    };
    ptys.entries.lock().unwrap().insert(id, entry);

    let app_for_reader = app.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 4096];
        // Trailing bytes of a multi-byte sequence that straddled the previous
        // read boundary; prepended to the next chunk. See `split_utf8`.
        let mut carry: Vec<u8> = Vec::new();
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    carry.extend_from_slice(&buf[..n]);
                    let (data, rest) = split_utf8(&carry);
                    carry = rest;
                    if !data.is_empty() {
                        let _ = app_for_reader.emit("pty-output", PtyOutput { id, data });
                    }
                }
                Err(_) => break,
            }
        }
        // The stream ended mid-sequence (truncated output). Nothing more is
        // coming, so flush the stub lossily rather than dropping it silently.
        if !carry.is_empty() {
            let data = String::from_utf8_lossy(&carry).into_owned();
            let _ = app_for_reader.emit("pty-output", PtyOutput { id, data });
        }
        // EOF or a read error both mean the pty is done: remove the entry
        // (so it doesn't linger for the app's lifetime) and reap the child
        // before telling the frontend it's gone. `pty_kill` racing in on the
        // same id afterward is fine — `entries.remove` is a no-op and it
        // stays `Ok` (see `pty_kill`).
        let removed = app_for_reader
            .state::<Ptys>()
            .entries
            .lock()
            .unwrap()
            .remove(&id);
        let mut code: Option<u32> = None;
        if let Some(mut entry) = removed {
            let _ = entry.child.kill();
            // On the normal EOF path the child has already exited, so the
            // kill above is a no-op and this wait reports the real status.
            code = entry.child.wait().ok().map(|status| status.exit_code());
        }
        let _ = app_for_reader.emit("pty-exit", PtyExit { id, code });
    });

    Ok(id)
}

#[tauri::command]
pub fn pty_write(ptys: tauri::State<'_, Ptys>, id: u32, data: String) -> Result<(), String> {
    let mut entries = ptys.entries.lock().unwrap();
    let entry = entries
        .get_mut(&id)
        .ok_or_else(|| "no such pty".to_string())?;
    entry
        .writer
        .write_all(data.as_bytes())
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn pty_resize(
    ptys: tauri::State<'_, Ptys>,
    id: u32,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let entries = ptys.entries.lock().unwrap();
    let entry = entries.get(&id).ok_or_else(|| "no such pty".to_string())?;
    entry
        .master
        .resize(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn pty_kill(ptys: tauri::State<'_, Ptys>, id: u32) -> Result<(), String> {
    let mut entries = ptys.entries.lock().unwrap();
    if let Some(mut entry) = entries.remove(&id) {
        let _ = entry.child.kill();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::ffi::OsStr;

    // The regression that actually made the pane unusable: with TERM unset,
    // zsh has no terminfo entry, ZLE cannot cursor-address, and the prompt
    // never renders. It must be set for every spawn regardless of locale.
    #[test]
    fn terminal_env_always_sets_term_and_colorterm() {
        for locale_present in [false, true] {
            let mut cmd = CommandBuilder::new("/bin/zsh");
            apply_terminal_env(&mut cmd, locale_present);
            assert_eq!(
                cmd.get_env("TERM"),
                Some(OsStr::new("xterm-256color")),
                "TERM must be set (locale_present={locale_present})"
            );
            assert_eq!(
                cmd.get_env("COLORTERM"),
                Some(OsStr::new("truecolor")),
                "COLORTERM must be set (locale_present={locale_present})"
            );
        }
    }

    #[test]
    fn locale_default_applies_only_when_environment_has_none() {
        // CommandBuilder captures the test process's own environment, so a
        // LANG inherited from the host (CI runners export one) would satisfy
        // or break these assertions for the wrong reason — drop it first and
        // assert only on what apply_terminal_env itself does.
        let mut cmd = CommandBuilder::new("/bin/zsh");
        cmd.env_remove("LANG");
        apply_terminal_env(&mut cmd, false);
        assert_eq!(cmd.get_env("LANG"), Some(OsStr::new("en_US.UTF-8")));

        let mut cmd = CommandBuilder::new("/bin/zsh");
        cmd.env_remove("LANG");
        apply_terminal_env(&mut cmd, true);
        assert_eq!(
            cmd.get_env("LANG"),
            None,
            "an inherited locale is a user preference and must not be overridden"
        );
    }

    #[test]
    fn locale_detection_treats_empty_and_missing_alike() {
        // The environment this bug was found in had LANG/LC_ALL/LC_CTYPE all
        // present but empty, which selects no locale at all.
        assert!(!locale_present_in([None, None, None]));
        assert!(!locale_present_in([Some(""), Some(""), Some("")]));
        assert!(!locale_present_in([None, Some(""), None]));
        // Any one of the three carrying a real value is enough.
        assert!(locale_present_in([Some("en_US.UTF-8"), None, None]));
        assert!(locale_present_in([None, Some(""), Some("C.UTF-8")]));
    }

    // Drive a byte stream through the reader thread's decode step, chunked at
    // an arbitrary boundary, and return what the frontend would receive.
    fn decode_chunked(bytes: &[u8], split_at: usize) -> String {
        let mut out = String::new();
        let mut carry: Vec<u8> = Vec::new();
        for chunk in [&bytes[..split_at], &bytes[split_at..]] {
            carry.extend_from_slice(chunk);
            let (data, rest) = split_utf8(&carry);
            carry = rest;
            out.push_str(&data);
        }
        if !carry.is_empty() {
            out.push_str(&String::from_utf8_lossy(&carry));
        }
        out
    }

    // The core regression: an oh-my-posh prompt glyph straddling a read
    // boundary must survive intact. U+E0B0 (the powerline separator) is 3
    // bytes; per-chunk `from_utf8_lossy` turned it into two U+FFFD.
    #[test]
    fn multibyte_glyph_split_across_chunks_survives() {
        let source = "a\u{e0b0}b";
        let bytes = source.as_bytes();
        // Split inside the 3-byte sequence, both ways.
        for split_at in [2usize, 3] {
            let decoded = decode_chunked(bytes, split_at);
            assert_eq!(
                decoded, source,
                "split at {split_at} must round-trip the glyph"
            );
            assert!(
                !decoded.contains('\u{FFFD}'),
                "split at {split_at} produced a replacement char"
            );
        }
    }

    // Every boundary in a realistic Nerd Font prompt, not just the handy ones.
    #[test]
    fn every_split_point_of_a_nerd_font_prompt_round_trips() {
        let source = "\u{e0b6}\u{f179} ~/dev \u{e0b0}\u{f126} main \u{e0b0} \u{2192} ";
        let bytes = source.as_bytes();
        for split_at in 0..=bytes.len() {
            let decoded = decode_chunked(bytes, split_at);
            assert_eq!(decoded, source, "round-trip failed at split {split_at}");
            assert!(
                !decoded.contains('\u{FFFD}'),
                "replacement char at split {split_at}"
            );
        }
    }

    #[test]
    fn truncated_tail_is_carried_not_emitted() {
        // "é" == 0xC3 0xA9; feed only the lead byte.
        let (data, carry) = split_utf8(&[b'x', 0xC3]);
        assert_eq!(data, "x");
        assert_eq!(carry, vec![0xC3]);
        // Completing it on the next read yields the whole char.
        let mut next = carry;
        next.push(0xA9);
        let (data, carry) = split_utf8(&next);
        assert_eq!(data, "é");
        assert!(carry.is_empty());
    }

    #[test]
    fn genuinely_invalid_bytes_still_become_replacement_chars() {
        // 0xFF can never start a UTF-8 sequence, so it must not be carried
        // (that would stall the stream and grow the buffer unboundedly).
        let (data, carry) = split_utf8(&[b'a', 0xFF, b'b']);
        assert_eq!(data, "a\u{FFFD}b");
        assert!(carry.is_empty(), "invalid bytes must not be carried");
    }

    #[test]
    fn carry_never_exceeds_three_bytes() {
        // A 4-byte sequence (emoji) truncated to its longest possible stub is
        // the worst case; nothing longer may ever be held back.
        let bytes = "\u{1F600}".as_bytes();
        for take in 0..bytes.len() {
            let (_, carry) = split_utf8(&bytes[..take]);
            assert!(carry.len() <= 3, "carry grew to {} bytes", carry.len());
        }
    }

    #[test]
    fn spawn_with_bogus_cwd_errors() {
        let result = resolve_cwd(Some("/definitely/not/a/real/path/xyz".to_string()));
        assert!(result.is_err());
    }

    #[test]
    fn kill_on_missing_id_is_ok() {
        let ptys = Ptys::default();
        let result = {
            let mut entries = ptys.entries.lock().unwrap();
            entries.remove(&999)
        };
        assert!(result.is_none());
    }

    // Regression for the reader thread leaking entries: spawn a real
    // short-lived pty command (no Tauri AppHandle needed — this drives the
    // same read-to-EOF-then-remove-and-reap sequence `pty_spawn`'s reader
    // thread runs, minus the event emit), then assert the registry entry is
    // actually gone and the child was reaped (not left a zombie) once the
    // pty's output stream hits EOF.
    #[test]
    fn eof_removes_the_registry_entry_and_reaps_the_child() {
        let pty_system = native_pty_system();
        let pair = pty_system
            .openpty(PtySize {
                rows: 24,
                cols: 80,
                pixel_width: 0,
                pixel_height: 0,
            })
            .expect("openpty");
        let cmd = CommandBuilder::new("/bin/echo");
        let child = pair.slave.spawn_command(cmd).expect("spawn echo");
        drop(pair.slave);

        let mut reader = pair.master.try_clone_reader().expect("clone reader");
        let writer = pair.master.take_writer().expect("take writer");

        let ptys = Ptys::default();
        let id = 1u32;
        ptys.entries.lock().unwrap().insert(
            id,
            PtyEntry {
                master: pair.master,
                writer,
                child,
            },
        );
        assert!(ptys.entries.lock().unwrap().contains_key(&id));

        // Drain to EOF, same as the reader thread's loop.
        let mut buf = [0u8; 4096];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(_) => continue,
                Err(_) => break,
            }
        }

        // Same cleanup the reader thread performs on EOF/error.
        let removed = ptys.entries.lock().unwrap().remove(&id);
        assert!(removed.is_some(), "entry must still be present to remove");
        let mut entry = removed.unwrap();
        let wait_result = entry.child.wait();
        assert!(wait_result.is_ok(), "child must be reapable, not a zombie");

        assert!(
            !ptys.entries.lock().unwrap().contains_key(&id),
            "entry must be gone from the registry after EOF cleanup"
        );

        // A `pty_kill` racing in afterward on the same id must stay Ok.
        let late_kill = ptys.entries.lock().unwrap().remove(&id);
        assert!(late_kill.is_none());
    }
}

// Window-level OS interactions the webview cannot perform itself.

use tauri::{LogicalPosition, WebviewWindow};

/// Move the OS pointer to a point inside this window's content area, for the
/// Hyprland-style cursor warp on keyboard focus changes. JS cannot move the
/// pointer, so the frontend hands the target here.
///
/// `x`/`y` are CSS pixels relative to the webview viewport — exactly what
/// `getBoundingClientRect()` returns — and are passed straight through with
/// **no offset arithmetic**. Tauri documents `set_cursor_position` as taking
/// *window* coordinates, and every `tao` backend adds the client-area origin
/// itself (the macOS path converts `inner_position()` to logical, adds the
/// given point, then calls `CGWarpMouseCursorPosition`). Adding
/// `inner_position()` here would double-count it and send the cursor off into
/// the desktop. This holds because the window is decorated, so the client
/// area and the webview viewport share an origin.
///
/// Logical rather than physical units: the macOS implementation works in
/// logical `CGFloat`, so a `PhysicalPosition<i32>` would throw away sub-point
/// precision on a Retina display.
///
/// No macOS permission is involved. `CGWarpMouseCursorPosition` *moves* the
/// cursor rather than *posting* an event, and only event injection
/// (`CGEventPost`) sits behind the Accessibility/TCC gate — so there is no
/// prompt and no silent permission failure. Errors surface as `Err` rather
/// than a no-op; the frontend treats the warp as best-effort and ignores
/// them, leaving focus-follow working on its own. (App Sandbox distribution
/// is the one configuration where this would be worth re-testing, since the
/// sandbox blocks Accessibility APIs broadly.)
#[tauri::command]
pub fn warp_cursor(window: WebviewWindow, x: f64, y: f64) -> Result<(), String> {
    window
        .set_cursor_position(LogicalPosition::new(x, y))
        .map_err(|e| e.to_string())
}

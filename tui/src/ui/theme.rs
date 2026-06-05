//! Colour vocabulary, shared across panes so the TUI reads consistently and
//! matches the webui's warn/danger semantics.

use ratatui::style::{Color, Modifier, Style};

use crate::model::Severity;

pub const ACCENT: Color = Color::Cyan;
pub const DIM: Color = Color::DarkGray;
pub const OK: Color = Color::Green;
pub const WARN: Color = Color::Yellow;
pub const DANGER: Color = Color::Red;

pub fn severity_color(sev: Severity) -> Color {
    match sev {
        Severity::Ok => OK,
        Severity::Warn => WARN,
        Severity::Danger => DANGER,
    }
}

/// Style for the selected row of the focused pane.
pub fn selected() -> Style {
    Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
}

/// Colour for a queue/chat status token.
pub fn status_color(status: &str) -> Color {
    match status {
        "proposed" => Color::White,
        "approved" => ACCENT,
        "in-flight" | "streaming" | "pending" => WARN,
        "drafted" | "completed" => Color::Magenta,
        "curated" => OK,
        "error" => DANGER,
        _ => DIM,
    }
}

/// Colour for a curation decision token.
pub fn decision_color(decision: &str) -> Color {
    match decision {
        "accept" => OK,
        "reject" => DANGER,
        "edit" => WARN,
        _ => DIM,
    }
}

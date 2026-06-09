//! Colour vocabulary, shared across panes so the TUI reads consistently and
//! matches the webui's warn/danger semantics.
//!
//! The look is "minimalist computer": near-black canvas, a single cyan
//! accent, green/yellow/red reserved for semantics, dim grey chrome, and
//! block-element gauges instead of decorative widgets.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;

use crate::model::Severity;

pub const ACCENT: Color = Color::Cyan;
pub const FG: Color = Color::Gray;
pub const DIM: Color = Color::DarkGray;
pub const OK: Color = Color::Green;
pub const WARN: Color = Color::Yellow;
pub const DANGER: Color = Color::Red;
/// Drafted/completed states — work awaiting a human decision.
pub const REVIEW: Color = Color::Magenta;

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

/// Border style for a pane: accent when focused, dim chrome otherwise.
pub fn border(active: bool) -> Style {
    if active {
        Style::default().fg(ACCENT)
    } else {
        Style::default().fg(DIM)
    }
}

/// Pane title: ` name ` uppercase-ish tag, accent when focused.
pub fn title(text: &str, active: bool) -> Span<'static> {
    let fg = if active { ACCENT } else { FG };
    Span::styled(
        format!(" {text} "),
        Style::default().fg(fg).add_modifier(Modifier::BOLD),
    )
}

/// Colour for a queue/chat status token.
pub fn status_color(status: &str) -> Color {
    match status {
        "proposed" => Color::White,
        "approved" => ACCENT,
        "in-flight" | "streaming" | "pending" => WARN,
        "drafted" | "completed" => REVIEW,
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

/// Colour for a trace event-type by family — keeps the stream scannable
/// without turning it into a rainbow.
pub fn event_color(event_type: &str) -> Color {
    if event_type.ends_with(".error") {
        return DANGER;
    }
    match event_type.split('.').next().unwrap_or("") {
        "llm" => REVIEW,
        "tool" => ACCENT,
        "mesh" | "presence" => OK,
        _ => FG,
    }
}

/// A fixed-width block-element gauge: `▮▮▮▯▯▯▯▯` for 3/8.
pub fn gauge(pct: f64, width: usize) -> String {
    let clamped = pct.clamp(0.0, 100.0);
    let filled = ((clamped / 100.0) * width as f64).round() as usize;
    let mut s = String::with_capacity(width * 3);
    for i in 0..width {
        s.push(if i < filled { '▮' } else { '▯' });
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gauge_is_fixed_width_and_clamped() {
        assert_eq!(gauge(0.0, 5), "▯▯▯▯▯");
        assert_eq!(gauge(100.0, 5), "▮▮▮▮▮");
        assert_eq!(gauge(250.0, 5), "▮▮▮▮▮");
        assert_eq!(gauge(-10.0, 5), "▯▯▯▯▯");
        assert_eq!(gauge(50.0, 4).chars().count(), 4);
        assert_eq!(gauge(50.0, 4), "▮▮▯▯");
    }

    #[test]
    fn event_families_are_distinct() {
        assert_eq!(event_color("tool.web_fetch"), ACCENT);
        assert_eq!(event_color("llm.call"), REVIEW);
        assert_eq!(event_color("llm.call.error"), DANGER);
        assert_eq!(event_color("mesh.heartbeat"), OK);
    }
}

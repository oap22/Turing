//! Top-level frame composition: alert banner, brand + tab strip + connection
//! status, the active pane body, a context footer, and the input-modal overlay.

mod panes;
mod theme;

use ratatui::layout::{Constraint, Flex, Layout, Rect};
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, Paragraph, Tabs};
use ratatui::Frame;

use crate::app::{App, Pane};
use crate::event::WsStatus;

pub fn draw(f: &mut Frame, app: &App) {
    let area = f.area();
    let has_banner = !app.alerts.is_empty();
    let constraints = if has_banner {
        vec![
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
        ]
    } else {
        vec![
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
        ]
    };
    let rows = Layout::vertical(constraints).split(area);
    let (banner_row, tab_row, body, footer) = if has_banner {
        (Some(rows[0]), rows[1], rows[2], rows[3])
    } else {
        (None, rows[0], rows[1], rows[2])
    };

    if let Some(b) = banner_row {
        draw_banner(f, b, app);
    }
    draw_tabs(f, tab_row, app);
    draw_body(f, body, app);
    draw_footer(f, footer, app);

    if app.input.is_some() {
        draw_input(f, area, app);
    }
}

fn draw_banner(f: &mut Frame, area: Rect, app: &App) {
    let mut danger = 0;
    let mut warn = 0;
    let mut bits: Vec<String> = Vec::new();
    for a in app.alert_list() {
        match a.severity.as_str() {
            "danger" => danger += 1,
            "warn" => warn += 1,
            _ => {}
        }
        let unit = if a.field == "temp_celsius" {
            "°C"
        } else {
            "%"
        };
        bits.push(format!(
            "{} {}{}{}",
            a.node_name,
            a.field
                .replace("_celsius", "")
                .replace("_pct", "")
                .to_uppercase(),
            a.value as i64,
            unit
        ));
    }
    let color = if danger > 0 {
        theme::DANGER
    } else {
        theme::WARN
    };
    let summary = format!(" ⚠ {} alert(s): {}  ", danger + warn, bits.join(", "));
    let p = Paragraph::new(Line::from(Span::styled(
        summary,
        Style::default().fg(Color::Black).bold(),
    )))
    .style(Style::default().bg(color).fg(Color::Black));
    f.render_widget(p, area);
}

fn draw_tabs(f: &mut Frame, area: Rect, app: &App) {
    let split = Layout::horizontal([
        Constraint::Length(9),
        Constraint::Min(0),
        Constraint::Length(30),
    ])
    .split(area);

    // brand block, the one loud element on screen.
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            " TURING ",
            Style::default().fg(Color::Black).bg(theme::ACCENT).bold(),
        ))),
        split[0],
    );

    let titles: Vec<Line> = Pane::TABS
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let count = if *p == Pane::Alerts && !app.alerts.is_empty() {
                format!("({}) ", app.alerts.len())
            } else {
                String::new()
            };
            Line::from(vec![
                Span::styled(format!(" {} ", i + 1), Style::default().fg(theme::DIM)),
                Span::raw(format!("{} {count}", p.title())),
            ])
        })
        .collect();
    let selected = Pane::TABS
        .iter()
        .position(|&p| p == app.active)
        .unwrap_or(0);
    let tabs = Tabs::new(titles)
        .select(selected)
        .highlight_style(Style::default().fg(theme::ACCENT).bold().reversed())
        .divider(Span::styled("·", Style::default().fg(theme::DIM)));
    f.render_widget(tabs, split[1]);

    // connection / node status, right-aligned.
    let (label, color) = match &app.ws_status {
        WsStatus::Connected => ("● live".to_string(), theme::OK),
        WsStatus::Connecting => ("◌ connecting".to_string(), theme::WARN),
        WsStatus::Disconnected(_) => ("● offline".to_string(), theme::DANGER),
    };
    let node = if app.node_name.is_empty() {
        "turing".to_string()
    } else {
        app.node_name.clone()
    };
    let status = Line::from(vec![
        Span::styled(format!("{node} "), Style::default().fg(theme::DIM)),
        Span::styled(label, Style::default().fg(color).bold()),
        Span::raw(" "),
    ])
    .right_aligned();
    f.render_widget(Paragraph::new(status), split[2]);
}

fn draw_body(f: &mut Frame, area: Rect, app: &App) {
    match app.active {
        Pane::Queue => panes::queue(f, area, app),
        Pane::Chat => panes::chat(f, area, app),
        Pane::Specs => panes::specs(f, area, app),
        Pane::Trace => panes::trace(f, area, app),
        Pane::Alerts => panes::alerts(f, area, app),
        Pane::Help => panes::help(f, area),
    }
}

/// Footer hints as `key desc` pairs — keys in accent, descriptions dim.
fn draw_footer(f: &mut Frame, area: Rect, app: &App) {
    if let Some(notice) = &app.notice {
        let p = Paragraph::new(Line::from(Span::styled(
            format!(" {notice}"),
            Style::default().fg(theme::WARN),
        )));
        f.render_widget(p, area);
        return;
    }
    let hints: &[(&str, &str)] = match app.active {
        Pane::Queue => &[
            ("h/l", "col"),
            ("j/k", "row"),
            ("a", "approve"),
            ("y/n/e", "curate"),
            ("?", "help"),
        ],
        Pane::Chat => &[
            ("j/k", "thread"),
            ("h/l", "subtask"),
            ("i", "prompt"),
            ("y/n/e", "thumb"),
            ("?", "help"),
        ],
        Pane::Specs => &[("j/k", "rows"), ("?", "help"), ("q", "quit")],
        Pane::Trace => &[
            ("j/k", "scroll"),
            ("f", "follow"),
            ("?", "help"),
            ("q", "quit"),
        ],
        Pane::Alerts => &[
            ("j/k", "rows"),
            ("s", "snooze 4h"),
            ("?", "help"),
            ("q", "quit"),
        ],
        Pane::Help => &[("1-5", "return to a pane"), ("q", "quit")],
    };
    let mut spans: Vec<Span> = Vec::with_capacity(hints.len() * 3 + 1);
    spans.push(Span::raw(" "));
    for (i, (key, desc)) in hints.iter().enumerate() {
        if i > 0 {
            spans.push(Span::styled("  ·  ", Style::default().fg(theme::DIM)));
        }
        spans.push(Span::styled(
            *key,
            Style::default().fg(theme::ACCENT).bold(),
        ));
        spans.push(Span::styled(
            format!(" {desc}"),
            Style::default().fg(theme::DIM),
        ));
    }
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn draw_input(f: &mut Frame, area: Rect, app: &App) {
    let Some(input) = &app.input else { return };
    let popup = centered(area, 70, 4);
    f.render_widget(Clear, popup);
    let block = Block::bordered()
        .title(theme::title(&input.label, true))
        .title_bottom(
            Line::from(vec![
                Span::styled(" enter", Style::default().fg(theme::ACCENT)),
                Span::styled(" submit · ", Style::default().fg(theme::DIM)),
                Span::styled("esc", Style::default().fg(theme::ACCENT)),
                Span::styled(" cancel ", Style::default().fg(theme::DIM)),
            ])
            .right_aligned(),
        )
        .border_style(Style::default().fg(theme::ACCENT));
    let text = Line::from(vec![
        Span::styled("> ", Style::default().fg(theme::ACCENT)),
        Span::raw(input.buffer.clone()),
        Span::styled("▌", Style::default().fg(theme::ACCENT)), // cursor
    ]);
    f.render_widget(Paragraph::new(text).block(block), popup);
}

/// A centred rect `pct_w`% wide and `height` rows tall.
fn centered(area: Rect, pct_w: u16, height: u16) -> Rect {
    let [h] = Layout::horizontal([Constraint::Percentage(pct_w)])
        .flex(Flex::Center)
        .areas(area);
    let [v] = Layout::vertical([Constraint::Length(height)])
        .flex(Flex::Center)
        .areas(h);
    v
}

#[cfg(test)]
mod tests {
    use super::draw;
    use crate::app::{App, Pane};
    use crate::model::parse_frame;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    /// A populated app exercising every pane's data path.
    fn populated() -> App {
        let mut app = App::new();
        app.apply_frame(
            parse_frame(r#"{"type":"hello","node_name":"pi-alpha","uptime_s":99}"#).unwrap(),
        );
        app.apply_frame(
            parse_frame(
                r#"{"type":"queue.snapshot","timestamp_ms":1,"items":[
                {"id":"q1","prompt":"how does rope scaling work?","specialty":"research",
                 "status":"drafted","proposed_by":"operator","episode_id":"e1",
                 "consumed_upstreams":[],"created_at_ms":1,"decision":null,"corrected_answer":null}]}"#,
            )
            .unwrap(),
        );
        app.apply_frame(
            parse_frame(
                r#"{"type":"chat.snapshot","timestamp_ms":1,"sessions":[
                {"id":"s1","prompt":"summarize attention","specialty":"research","created_at_ms":1,
                 "subtasks":[{"id":"st1","session_id":"s1","index":0,"specialty":"research",
                 "prompt":"","content":"attention is…","status":"completed","consumed_upstreams":[]}]}]}"#,
            )
            .unwrap(),
        );
        app.apply_frame(
            parse_frame(
                r#"{"type":"alert","node_id":"jetson-2","node_name":"jetson-2","field":"temp_celsius",
                "severity":"danger","value":84.0,"threshold":82.0,"state":"alerting","fired_at_ms":1}"#,
            )
            .unwrap(),
        );
        app.apply_frame(
            parse_frame(r#"{"type":"message_trace","node_name":"jetson-1","event_type":"tool.web_fetch","timestamp_ms":5,"duration_ms":12.0}"#)
                .unwrap(),
        );
        app
    }

    fn buffer_text(t: &Terminal<TestBackend>) -> String {
        t.backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect()
    }

    #[test]
    fn every_pane_renders_without_panic() {
        let mut app = populated();
        for pane in [
            Pane::Queue,
            Pane::Chat,
            Pane::Specs,
            Pane::Trace,
            Pane::Alerts,
            Pane::Help,
        ] {
            app.active = pane;
            let mut term = Terminal::new(TestBackend::new(120, 40)).unwrap();
            term.draw(|f| draw(f, &app)).unwrap();
            let text = buffer_text(&term);
            // The alert banner is always present here, and the tab strip too.
            assert!(text.contains("alert"), "{pane:?} missing alert banner");
            assert!(text.contains("Queue"), "{pane:?} missing tab strip");
            assert!(text.contains("TURING"), "{pane:?} missing brand block");
        }
    }

    #[test]
    fn input_modal_renders() {
        let mut app = populated();
        app.active = Pane::Chat;
        // open the chat input
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
        app.on_key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        let mut term = Terminal::new(TestBackend::new(120, 40)).unwrap();
        term.draw(|f| draw(f, &app)).unwrap();
        assert!(buffer_text(&term).contains("research prompt"));
    }

    #[test]
    fn footer_shows_keycap_hints() {
        let mut app = populated();
        app.active = Pane::Trace;
        let mut term = Terminal::new(TestBackend::new(120, 40)).unwrap();
        term.draw(|f| draw(f, &app)).unwrap();
        let text = buffer_text(&term);
        assert!(text.contains("follow"), "trace footer missing follow hint");
    }

    #[test]
    fn tiny_terminal_does_not_panic() {
        // Layout must survive a pathologically small terminal (resize spam).
        let app = populated();
        for (w, h) in [(1u16, 1u16), (4, 2), (10, 5), (20, 3)] {
            let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
            term.draw(|f| draw(f, &app)).unwrap();
        }
    }
}

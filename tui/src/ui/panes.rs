//! Per-pane renderers. Each takes the immutable [`App`] and draws into a rect.
//! Selection state is rebuilt per-frame from the app's indices, so rendering is
//! a pure function of state.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Cell, List, ListItem, ListState, Paragraph, Row, Table, Wrap};
use ratatui::Frame;

use crate::app::{App, Pane};
use crate::model::{disk_percent, severity_for_disk, severity_for_temp, QUEUE_COLUMNS};
use crate::ui::theme;

// ── small formatters ──────────────────────────────────────────────────────────
fn fmt_bytes(b: u64) -> String {
    const U: [&str; 5] = ["B", "K", "M", "G", "T"];
    let mut v = b as f64;
    let mut i = 0;
    while v >= 1024.0 && i < U.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{b}{}", U[0])
    } else {
        format!("{v:.1}{}", U[i])
    }
}

fn fmt_uptime(secs: u64) -> String {
    let d = secs / 86_400;
    let h = (secs % 86_400) / 3_600;
    let m = (secs % 3_600) / 60;
    if d > 0 {
        format!("{d}d{h}h")
    } else if h > 0 {
        format!("{h}h{m}m")
    } else {
        format!("{m}m")
    }
}

fn truncate(s: &str, max: usize) -> String {
    let one_line = s.replace(['\n', '\r'], " ");
    if one_line.chars().count() <= max {
        one_line
    } else {
        let mut t: String = one_line.chars().take(max.saturating_sub(1)).collect();
        t.push('…');
        t
    }
}

// ── queue ─────────────────────────────────────────────────────────────────────
pub fn queue(f: &mut Frame, area: Rect, app: &App) {
    let cols = Layout::horizontal([Constraint::Ratio(1, 5); 5]).split(area);
    for (i, name) in QUEUE_COLUMNS.iter().enumerate() {
        let items = app.queue_column_items(i);
        let active = app.active == Pane::Queue && app.queue_col == i;
        let width = cols[i].width.saturating_sub(2) as usize;
        let rows: Vec<ListItem> = items
            .iter()
            .map(|it| {
                let mut spans = Vec::new();
                if let Some(d) = &it.decision {
                    spans.push(Span::styled(
                        format!("{} ", mark(d)),
                        Style::default().fg(theme::decision_color(d)),
                    ));
                }
                spans.push(Span::raw(truncate(&it.prompt, width.saturating_sub(2))));
                ListItem::new(Line::from(spans))
            })
            .collect();
        let border = if active {
            Style::default().fg(theme::ACCENT)
        } else {
            Style::default().fg(theme::DIM)
        };
        let title = Span::styled(
            format!(" {name} ({}) ", items.len()),
            Style::default().fg(theme::status_color(name)),
        );
        let block = Block::bordered().title(title).border_style(border);
        let list = List::new(rows)
            .block(block)
            .highlight_style(theme::selected());
        if active {
            let mut st = ListState::default();
            st.select(Some(app.queue_row));
            f.render_stateful_widget(list, cols[i], &mut st);
        } else {
            f.render_widget(list, cols[i]);
        }
    }
}

fn mark(decision: &str) -> &'static str {
    match decision {
        "accept" => "✓",
        "reject" => "✗",
        "edit" => "✎",
        _ => "•",
    }
}

// ── chat ──────────────────────────────────────────────────────────────────────
pub fn chat(f: &mut Frame, area: Rect, app: &App) {
    let parts = Layout::horizontal([Constraint::Length(34), Constraint::Min(0)]).split(area);
    let active = app.active == Pane::Chat;

    // sessions list
    let sess_items: Vec<ListItem> = app
        .sessions
        .iter()
        .map(|s| ListItem::new(truncate(&s.prompt, 30)))
        .collect();
    let sblock = Block::bordered()
        .title(" threads ")
        .border_style(border(active));
    let slist = List::new(sess_items)
        .block(sblock)
        .highlight_style(theme::selected());
    let mut sstate = ListState::default();
    if !app.sessions.is_empty() {
        sstate.select(Some(app.chat_sel));
    }
    f.render_stateful_widget(slist, parts[0], &mut sstate);

    // thread detail
    let tblock = Block::bordered()
        .title(" thread ")
        .border_style(Style::default().fg(theme::DIM));
    match app.selected_session() {
        None => {
            let p = Paragraph::new("No threads yet. Press 'i' to submit a research prompt.")
                .style(Style::default().fg(theme::DIM))
                .block(tblock)
                .wrap(Wrap { trim: true });
            f.render_widget(p, parts[1]);
        }
        Some(session) => {
            let sub_items: Vec<ListItem> = session
                .subtasks
                .iter()
                .map(|st| {
                    let head = Line::from(vec![
                        Span::styled(format!("#{} ", st.index), Style::default().fg(theme::DIM)),
                        Span::styled(
                            format!("[{}] ", st.status),
                            Style::default().fg(theme::status_color(&st.status)),
                        ),
                        Span::styled(
                            format!("{} ", st.specialty),
                            Style::default().fg(theme::ACCENT),
                        ),
                    ]);
                    let body = Line::from(Span::raw(truncate(
                        &st.content,
                        parts[1].width.saturating_sub(4) as usize,
                    )));
                    ListItem::new(vec![head, body])
                })
                .collect();
            let inner = tblock.inner(parts[1]);
            f.render_widget(tblock, parts[1]);
            // header line with the prompt
            let chunks = Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(inner);
            let hdr = Paragraph::new(Line::from(vec![
                Span::styled("Q: ", Style::default().fg(theme::ACCENT)),
                Span::raw(session.prompt.clone()),
            ]))
            .wrap(Wrap { trim: true });
            f.render_widget(hdr, chunks[0]);
            let list = List::new(sub_items).highlight_style(theme::selected());
            let mut state = ListState::default();
            if active && !session.subtasks.is_empty() {
                state.select(Some(app.chat_subtask_sel));
            }
            f.render_stateful_widget(list, chunks[1], &mut state);
        }
    }
}

// ── specs ─────────────────────────────────────────────────────────────────────
pub fn specs(f: &mut Frame, area: Rect, app: &App) {
    let header = Row::new(["node", "hw", "cpu%", "mem", "disk", "temp", "up", "load"])
        .style(Style::default().fg(theme::ACCENT).bold());
    let rows: Vec<Row> = app
        .peers
        .iter()
        .map(|p| {
            let s = p.specs.as_ref();
            let cpu = s
                .map(|x| format!("{:.0}", x.cpu_percent))
                .unwrap_or_else(|| "—".into());
            let mem = s
                .map(|x| {
                    format!(
                        "{}/{}",
                        fmt_bytes(x.mem_used_bytes),
                        fmt_bytes(x.ram_total_bytes)
                    )
                })
                .unwrap_or_else(|| "—".into());
            let dpct =
                s.and_then(|x| disk_percent(Some(x.disk_used_bytes), Some(x.disk_total_bytes)));
            let disk = dpct
                .map(|d| format!("{d:.0}%"))
                .unwrap_or_else(|| "—".into());
            let temp = s.and_then(|x| x.temp_celsius);
            let temp_s = temp
                .map(|t| format!("{t:.0}°"))
                .unwrap_or_else(|| "—".into());
            let up = s
                .map(|x| fmt_uptime(x.uptime_seconds))
                .unwrap_or_else(|| "—".into());
            let load = s
                .map(|x| format!("{:.2}", x.loadavg_1m))
                .unwrap_or_else(|| "—".into());
            let hw = s
                .map(|x| truncate(&x.model_name, 18))
                .unwrap_or_else(|| "—".into());

            let temp_style = Style::default().fg(theme::severity_color(severity_for_temp(temp)));
            let disk_style = Style::default().fg(theme::severity_color(severity_for_disk(dpct)));
            let mut name_style = Style::default().bold();
            if p.stale {
                name_style = Style::default().fg(theme::DIM);
            }
            let name = if p.is_self {
                format!("{} (self)", p.node_name)
            } else {
                p.node_name.clone()
            };
            Row::new(vec![
                Cell::from(name).style(name_style),
                Cell::from(hw).style(Style::default().fg(theme::DIM)),
                Cell::from(cpu),
                Cell::from(mem),
                Cell::from(disk).style(disk_style),
                Cell::from(temp_s).style(temp_style),
                Cell::from(up),
                Cell::from(load),
            ])
        })
        .collect();
    let widths = [
        Constraint::Length(18),
        Constraint::Length(20),
        Constraint::Length(5),
        Constraint::Length(13),
        Constraint::Length(6),
        Constraint::Length(6),
        Constraint::Length(6),
        Constraint::Length(6),
    ];
    let active = app.active == Pane::Specs;
    let block = Block::bordered()
        .title(format!(" fleet ({}) ", app.peers.len()))
        .border_style(border(active));
    let table = Table::new(rows, widths)
        .header(header)
        .block(block)
        .row_highlight_style(theme::selected());
    let mut state = ratatui::widgets::TableState::default();
    if active && !app.peers.is_empty() {
        state.select(Some(app.specs_sel));
    }
    f.render_stateful_widget(table, area, &mut state);
}

// ── trace ─────────────────────────────────────────────────────────────────────
pub fn trace(f: &mut Frame, area: Rect, app: &App) {
    let active = app.active == Pane::Trace;
    let width = area.width.saturating_sub(2) as usize;
    let items: Vec<ListItem> = app
        .trace
        .iter()
        .map(|e| {
            let dur = e
                .duration_ms
                .map(|d| format!("{d:.0}ms"))
                .unwrap_or_default();
            let mut spans = vec![
                Span::styled(
                    format!("{:<10} ", truncate(&e.node_name, 10)),
                    Style::default().fg(theme::ACCENT),
                ),
                Span::raw(format!("{:<22} ", truncate(&e.event_type, 22))),
                Span::styled(format!("{dur:>7} "), Style::default().fg(theme::DIM)),
            ];
            if let Some(err) = &e.error {
                spans.push(Span::styled(
                    truncate(err, width.saturating_sub(42)),
                    Style::default().fg(theme::DANGER),
                ));
            }
            ListItem::new(Line::from(spans))
        })
        .collect();
    let follow = if app.trace_follow { " ▶follow" } else { "" };
    let block = Block::bordered()
        .title(format!(" trace ({}){follow} ", app.trace.len()))
        .border_style(border(active));
    let list = List::new(items)
        .block(block)
        .highlight_style(theme::selected());
    let mut state = ListState::default();
    if !app.trace.is_empty() {
        state.select(Some(app.trace_sel));
    }
    f.render_stateful_widget(list, area, &mut state);
}

// ── alerts ────────────────────────────────────────────────────────────────────
pub fn alerts(f: &mut Frame, area: Rect, app: &App) {
    let active = app.active == Pane::Alerts;
    let list_data = app.alert_list();
    let items: Vec<ListItem> = list_data
        .iter()
        .map(|a| {
            let sev = match a.severity.as_str() {
                "danger" => theme::DANGER,
                "warn" => theme::WARN,
                _ => theme::OK,
            };
            let unit = if a.field == "temp_celsius" {
                "°C"
            } else {
                "%"
            };
            let snoozed = a.snoozed_until_ms.map(|_| "  (snoozed)").unwrap_or("");
            ListItem::new(Line::from(vec![
                Span::styled(
                    format!(
                        "{:<7} ",
                        a.field.replace("_celsius", "").replace("_pct", "")
                    ),
                    Style::default().fg(sev).bold(),
                ),
                Span::styled(
                    format!("{:<12} ", a.node_name),
                    Style::default().fg(theme::ACCENT),
                ),
                Span::styled(
                    format!("{:.1}{unit} (>{:.0}{unit})", a.value, a.threshold),
                    Style::default().fg(sev),
                ),
                Span::styled(snoozed, Style::default().fg(theme::DIM)),
            ]))
        })
        .collect();
    let block = Block::bordered()
        .title(format!(" alerts ({}) — 's' snooze ", list_data.len()))
        .border_style(border(active));
    if list_data.is_empty() {
        let p = Paragraph::new("No active hardware alerts. 🟢")
            .style(Style::default().fg(theme::DIM))
            .block(block);
        f.render_widget(p, area);
        return;
    }
    let list = List::new(items)
        .block(block)
        .highlight_style(theme::selected());
    let mut state = ListState::default();
    state.select(Some(app.alert_sel));
    f.render_stateful_widget(list, area, &mut state);
}

// ── help ──────────────────────────────────────────────────────────────────────
pub fn help(f: &mut Frame, area: Rect) {
    let lines = vec![
        Line::from(Span::styled(
            "Turing TUI — operator surface",
            Style::default().fg(theme::ACCENT).bold(),
        )),
        Line::from(""),
        Line::from("Global:  1-5 panes · Tab/Shift-Tab cycle · ? help · q / Ctrl-C quit"),
        Line::from(""),
        Line::from(Span::styled("Queue (1)", Style::default().bold())),
        Line::from("  h/l move column · j/k move row"),
        Line::from("  a approve (proposed) · y accept · n reject · e edit (drafted)"),
        Line::from(""),
        Line::from(Span::styled("Chat (2)", Style::default().bold())),
        Line::from("  j/k thread · h/l subtask · i new prompt"),
        Line::from("  y accept · n reject · e edit (completed subtask)"),
        Line::from(""),
        Line::from(Span::styled("Specs (3)", Style::default().bold())),
        Line::from("  j/k rows — temp/disk colour-shift toward danger; stale peers dim"),
        Line::from(""),
        Line::from(Span::styled("Trace (4)", Style::default().bold())),
        Line::from("  j/k scroll · f toggle follow (auto-scroll to newest)"),
        Line::from(""),
        Line::from(Span::styled("Alerts (5)", Style::default().bold())),
        Line::from("  j/k rows · s snooze the selected (peer, field) for 4h"),
        Line::from(""),
        Line::from("In an input box:  type · Enter submit · Esc cancel"),
    ];
    let block = Block::bordered()
        .title(" help ")
        .border_style(Style::default().fg(theme::ACCENT));
    f.render_widget(
        Paragraph::new(lines).block(block).wrap(Wrap { trim: true }),
        area,
    );
}

fn border(active: bool) -> Style {
    if active {
        Style::default().fg(theme::ACCENT)
    } else {
        Style::default().fg(theme::DIM)
    }
}

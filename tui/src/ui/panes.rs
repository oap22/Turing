//! Per-pane renderers. Each takes the immutable [`App`] and draws into a rect.
//! Selection state is rebuilt per-frame from the app's indices, so rendering is
//! a pure function of state.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, Cell, List, ListItem, ListState, Paragraph, Row, Scrollbar, ScrollbarOrientation,
    ScrollbarState, Table, Wrap,
};
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

/// Wall-clock `HH:MM:SS` (UTC) from an epoch-milliseconds stamp.
fn fmt_clock(ts_ms: i64) -> String {
    let secs = (ts_ms / 1000).rem_euclid(86_400);
    format!(
        "{:02}:{:02}:{:02}",
        secs / 3_600,
        (secs % 3_600) / 60,
        secs % 60
    )
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

/// Right-hand scrollbar for a list with `len` rows and selection `sel`.
fn scrollbar(f: &mut Frame, area: Rect, len: usize, sel: usize) {
    if len == 0 || area.height as usize >= len {
        return;
    }
    let mut state = ScrollbarState::new(len).position(sel);
    f.render_stateful_widget(
        Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None)
            .end_symbol(None)
            .track_symbol(Some("│"))
            .thumb_symbol("┃")
            .track_style(Style::default().fg(theme::DIM))
            .thumb_style(Style::default().fg(theme::ACCENT)),
        area.inner(ratatui::layout::Margin {
            horizontal: 0,
            vertical: 1,
        }),
        &mut state,
    );
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
                let mut head = Vec::new();
                if let Some(d) = &it.decision {
                    head.push(Span::styled(
                        format!("{} ", mark(d)),
                        Style::default().fg(theme::decision_color(d)),
                    ));
                }
                head.push(Span::raw(truncate(&it.prompt, width.saturating_sub(2))));
                let meta = Line::from(vec![
                    Span::styled(
                        format!("  {}", truncate(&it.specialty, 14)),
                        Style::default().fg(theme::ACCENT),
                    ),
                    Span::styled(
                        format!(" · {}", truncate(&it.proposed_by, 12)),
                        Style::default().fg(theme::DIM),
                    ),
                ]);
                ListItem::new(vec![Line::from(head), meta])
            })
            .collect();
        let title = Line::from(vec![
            theme::title(name, active),
            Span::styled(format!("{} ", items.len()), Style::default().fg(theme::DIM)),
        ]);
        let block = Block::bordered()
            .title(title)
            .border_style(theme::border(active));
        let list = List::new(rows)
            .block(block)
            .highlight_style(theme::selected());
        if active {
            let mut st = ListState::default();
            st.select(Some(app.queue_row));
            f.render_stateful_widget(list, cols[i], &mut st);
            scrollbar(f, cols[i], items.len() * 2, app.queue_row * 2);
        } else {
            f.render_widget(list, cols[i]);
        }
    }
}

fn mark(decision: &str) -> &'static str {
    match decision {
        "accept" => "accept",
        "reject" => "reject",
        "edit" => "edit",
        _ => "curated",
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
    let stitle = Line::from(vec![
        theme::title("threads", active),
        Span::styled(
            format!("{} ", app.sessions.len()),
            Style::default().fg(theme::DIM),
        ),
    ]);
    let sblock = Block::bordered()
        .title(stitle)
        .border_style(theme::border(active));
    let slist = List::new(sess_items)
        .block(sblock)
        .highlight_style(theme::selected());
    let mut sstate = ListState::default();
    if !app.sessions.is_empty() {
        sstate.select(Some(app.chat_sel));
    }
    f.render_stateful_widget(slist, parts[0], &mut sstate);
    scrollbar(f, parts[0], app.sessions.len(), app.chat_sel);

    // thread detail
    let tblock = Block::bordered()
        .title(theme::title("thread", false))
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
                    let status_tag = if st.status == "streaming" {
                        format!("[{}…] ", st.status)
                    } else {
                        format!("[{}] ", st.status)
                    };
                    let mut head = vec![
                        Span::styled(format!("#{} ", st.index), Style::default().fg(theme::DIM)),
                        Span::styled(
                            status_tag,
                            Style::default().fg(theme::status_color(&st.status)),
                        ),
                        Span::styled(
                            format!("{} ", st.specialty),
                            Style::default().fg(theme::ACCENT),
                        ),
                    ];
                    if let Some(d) = &st.decision {
                        head.push(Span::styled(
                            format!("{} {}", mark(d), d),
                            Style::default().fg(theme::decision_color(d)),
                        ));
                    }
                    let body = Line::from(Span::raw(truncate(
                        &st.content,
                        parts[1].width.saturating_sub(4) as usize,
                    )));
                    ListItem::new(vec![Line::from(head), body])
                })
                .collect();
            let inner = tblock.inner(parts[1]);
            f.render_widget(tblock, parts[1]);
            // header line with the prompt
            let chunks = Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(inner);
            let hdr = Paragraph::new(Line::from(vec![
                Span::styled("Q: ", Style::default().fg(theme::ACCENT).bold()),
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
const GAUGE_W: usize = 5;

/// `▮▮▯▯▯ 42%` cell with severity colouring.
fn gauge_cell(pct: Option<f64>, sev_color: ratatui::style::Color) -> Cell<'static> {
    match pct {
        Some(p) => Cell::from(Line::from(vec![
            Span::styled(theme::gauge(p, GAUGE_W), Style::default().fg(sev_color)),
            Span::styled(format!(" {p:>3.0}%"), Style::default().fg(sev_color)),
        ])),
        None => Cell::from("—").style(Style::default().fg(theme::DIM)),
    }
}

pub fn specs(f: &mut Frame, area: Rect, app: &App) {
    let header = Row::new([
        "node",
        "hw",
        "cpu",
        "mem",
        "disk",
        "temp",
        "up",
        "load 1/5/15",
    ])
    .style(Style::default().fg(theme::ACCENT).bold());
    let rows: Vec<Row> = app
        .peers
        .iter()
        .map(|p| {
            let s = p.specs.as_ref();
            let cpu_pct = s.map(|x| x.cpu_percent);
            let mem_pct =
                s.and_then(|x| disk_percent(Some(x.mem_used_bytes), Some(x.ram_total_bytes)));
            let mem_abs = s
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
            let temp = s.and_then(|x| x.temp_celsius);
            let temp_s = temp
                .map(|t| format!("{t:.0}°"))
                .unwrap_or_else(|| "—".into());
            let up = s
                .map(|x| fmt_uptime(x.uptime_seconds))
                .unwrap_or_else(|| "—".into());
            let load = s
                .map(|x| {
                    format!(
                        "{:.2} {:.2} {:.2}",
                        x.loadavg_1m, x.loadavg_5m, x.loadavg_15m
                    )
                })
                .unwrap_or_else(|| "—".into());
            let hw = s
                .map(|x| {
                    if x.cpu_cores > 0 {
                        format!("{} {}c", truncate(&x.model_name, 15), x.cpu_cores)
                    } else {
                        truncate(&x.model_name, 18)
                    }
                })
                .unwrap_or_else(|| "—".into());

            let cpu_color = match cpu_pct {
                Some(c) if c >= 90.0 => theme::DANGER,
                Some(c) if c >= 75.0 => theme::WARN,
                _ => theme::FG,
            };
            let mem_color = theme::FG;
            let temp_style = Style::default().fg(theme::severity_color(severity_for_temp(temp)));
            let disk_color = theme::severity_color(severity_for_disk(dpct));
            let mut name_style = Style::default().bold();
            if p.stale {
                name_style = Style::default().fg(theme::DIM);
            }
            let name = if p.is_self {
                format!("{} ◆", p.node_name)
            } else {
                p.node_name.clone()
            };
            Row::new(vec![
                Cell::from(name).style(name_style),
                Cell::from(hw).style(Style::default().fg(theme::DIM)),
                gauge_cell(cpu_pct, cpu_color),
                Cell::from(Line::from(vec![
                    Span::styled(
                        theme::gauge(mem_pct.unwrap_or(0.0), GAUGE_W),
                        Style::default().fg(mem_color),
                    ),
                    Span::styled(format!(" {mem_abs}"), Style::default().fg(theme::FG)),
                ])),
                gauge_cell(dpct, disk_color),
                Cell::from(temp_s).style(temp_style),
                Cell::from(up),
                Cell::from(load),
            ])
        })
        .collect();
    let widths = [
        Constraint::Length(18),
        Constraint::Length(20),
        Constraint::Length(10),
        Constraint::Length(19),
        Constraint::Length(10),
        Constraint::Length(6),
        Constraint::Length(6),
        Constraint::Length(17),
    ];
    let active = app.active == Pane::Specs;
    let title = Line::from(vec![
        theme::title("fleet", active),
        Span::styled(
            format!("{} node(s) · ◆ self ", app.peers.len()),
            Style::default().fg(theme::DIM),
        ),
    ]);
    let block = Block::bordered()
        .title(title)
        .border_style(theme::border(active));
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
                    format!("{} ", fmt_clock(e.timestamp_ms)),
                    Style::default().fg(theme::DIM),
                ),
                Span::styled(
                    format!("{:<10} ", truncate(&e.node_name, 10)),
                    Style::default().fg(theme::ACCENT),
                ),
                Span::styled(
                    format!("{:<22} ", truncate(&e.event_type, 22)),
                    Style::default().fg(theme::event_color(&e.event_type)),
                ),
                Span::styled(format!("{dur:>7} "), Style::default().fg(theme::DIM)),
            ];
            if let Some(err) = &e.error {
                spans.push(Span::styled(
                    truncate(err, width.saturating_sub(51)),
                    Style::default().fg(theme::DANGER),
                ));
            }
            ListItem::new(Line::from(spans))
        })
        .collect();
    let follow = if app.trace_follow {
        Span::styled("▶ follow ", Style::default().fg(theme::OK))
    } else {
        Span::styled("⏸ paused ", Style::default().fg(theme::WARN))
    };
    let title = Line::from(vec![
        theme::title("trace", active),
        Span::styled(
            format!("{} ", app.trace.len()),
            Style::default().fg(theme::DIM),
        ),
        follow,
    ]);
    let block = Block::bordered()
        .title(title)
        .border_style(theme::border(active));
    let list = List::new(items)
        .block(block)
        .highlight_style(theme::selected());
    let mut state = ListState::default();
    if !app.trace.is_empty() {
        state.select(Some(app.trace_sel));
    }
    f.render_stateful_widget(list, area, &mut state);
    scrollbar(f, area, app.trace.len(), app.trace_sel);
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
                Span::styled("▌ ", Style::default().fg(sev)),
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
    let title = Line::from(vec![
        theme::title("alerts", active),
        Span::styled(
            format!("{} · s snooze 4h ", list_data.len()),
            Style::default().fg(theme::DIM),
        ),
    ]);
    let block = Block::bordered()
        .title(title)
        .border_style(theme::border(active));
    if list_data.is_empty() {
        let p = Paragraph::new("No active hardware alerts.")
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
/// One `key  description` help row with the key in accent.
fn help_row(key: &str, desc: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("  {key:<14}"), Style::default().fg(theme::ACCENT)),
        Span::styled(desc.to_string(), Style::default().fg(theme::FG)),
    ])
}

fn help_section(name: &str) -> Line<'static> {
    Line::from(Span::styled(
        format!("░ {name}"),
        Style::default().fg(theme::FG).bold(),
    ))
}

pub fn help(f: &mut Frame, area: Rect) {
    let lines = vec![
        Line::from(vec![
            Span::styled("TURING", Style::default().fg(theme::ACCENT).bold()),
            Span::styled(
                " // terminal operator surface",
                Style::default().fg(theme::DIM),
            ),
        ]),
        Line::from(""),
        help_section("global"),
        help_row("1-5", "switch pane"),
        help_row("tab / S-tab", "cycle panes"),
        help_row("?", "this help"),
        help_row("q · ctrl-c", "quit"),
        Line::from(""),
        help_section("queue [1]"),
        help_row("h/l · j/k", "move column · row"),
        help_row("a", "approve (proposed)"),
        help_row("y / n / e", "accept / reject / edit (drafted)"),
        Line::from(""),
        help_section("chat [2]"),
        help_row("j/k · h/l", "thread · subtask"),
        help_row("i", "new research prompt"),
        help_row("y / n / e", "thumb a completed subtask"),
        Line::from(""),
        help_section("specs [3]"),
        help_row(
            "j/k",
            "rows — temp/disk shift toward danger; stale peers dim",
        ),
        Line::from(""),
        help_section("trace [4]"),
        help_row("j/k", "scroll"),
        help_row("f", "toggle follow (auto-scroll to newest)"),
        Line::from(""),
        help_section("alerts [5]"),
        help_row("j/k", "rows"),
        help_row("s", "snooze the selected (peer, field) for 4h"),
        Line::from(""),
        help_section("input box"),
        help_row("enter / esc", "submit / cancel"),
    ];
    let block = Block::bordered()
        .title(theme::title("help", true))
        .border_style(Style::default().fg(theme::ACCENT));
    f.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

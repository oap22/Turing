//! Application state + pure reducers.
//!
//! The design keeps all mutation in pure methods — [`App::apply_frame`],
//! [`App::on_key`] — that take decoded input and return either nothing or a
//! list of [`Action`]s for the async layer to perform. No method here does
//! I/O, so the entire operator surface is unit-testable without a gateway
//! (see the tests at the bottom). The render loop in `main.rs` is the only
//! place that touches sockets and the terminal.

use std::collections::{BTreeMap, VecDeque};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::event::{EngineEvent, WsStatus};
use crate::model::{
    Alert, ChatSession, ChatSubtask, Frame, Peer, QueueItem, TraceEvent, QUEUE_COLUMNS,
};

const TRACE_CAP: usize = 1000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pane {
    Queue,
    Chat,
    Specs,
    Trace,
    Alerts,
    Help,
}

impl Pane {
    pub fn title(self) -> &'static str {
        match self {
            Pane::Queue => "Queue",
            Pane::Chat => "Chat",
            Pane::Specs => "Specs",
            Pane::Trace => "Trace",
            Pane::Alerts => "Alerts",
            Pane::Help => "Help",
        }
    }
    /// The tab strip order (Help is reached with `?`, not a number).
    pub const TABS: [Pane; 5] = [
        Pane::Queue,
        Pane::Chat,
        Pane::Specs,
        Pane::Trace,
        Pane::Alerts,
    ];
}

/// An operator action the async layer must perform against the gateway.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    QueueApprove(String),
    QueueAccept(String),
    QueueReject(String),
    QueueEdit {
        id: String,
        text: String,
    },
    ChatSubmit {
        prompt: String,
    },
    ChatAccept {
        sid: String,
        stid: String,
    },
    ChatReject {
        sid: String,
        stid: String,
    },
    ChatEdit {
        sid: String,
        stid: String,
        text: String,
    },
    Snooze {
        node_id: String,
        field: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum InputPurpose {
    ChatSubmit,
    QueueEdit { id: String },
    ChatEdit { sid: String, stid: String },
}

#[derive(Debug, Clone)]
pub struct InputState {
    pub label: String,
    pub buffer: String,
    purpose: InputPurpose,
}

pub struct App {
    pub should_quit: bool,
    pub active: Pane,
    pub ws_status: WsStatus,
    pub notice: Option<String>,
    pub node_name: String,

    pub queue: Vec<QueueItem>,
    pub queue_col: usize,
    pub queue_row: usize,

    pub sessions: Vec<ChatSession>,
    pub chat_sel: usize,
    pub chat_subtask_sel: usize,

    pub peers: Vec<Peer>,
    pub specs_sel: usize,

    pub trace: VecDeque<TraceEvent>,
    pub trace_sel: usize,
    pub trace_follow: bool,

    /// Active alerts keyed by (node_id, field); cleared alerts are removed.
    /// BTreeMap → stable display order.
    pub alerts: BTreeMap<(String, String), Alert>,
    pub alert_sel: usize,

    pub input: Option<InputState>,
}

impl Default for App {
    fn default() -> Self {
        Self {
            should_quit: false,
            active: Pane::Queue,
            ws_status: WsStatus::Connecting,
            notice: None,
            node_name: String::new(),
            queue: Vec::new(),
            queue_col: 0,
            queue_row: 0,
            sessions: Vec::new(),
            chat_sel: 0,
            chat_subtask_sel: 0,
            peers: Vec::new(),
            specs_sel: 0,
            trace: VecDeque::new(),
            trace_sel: 0,
            trace_follow: true,
            alerts: BTreeMap::new(),
            alert_sel: 0,
            input: None,
        }
    }
}

impl App {
    pub fn new() -> Self {
        Self::default()
    }

    // ── engine events ─────────────────────────────────────────────────────────
    pub fn on_engine(&mut self, ev: EngineEvent) -> bool {
        match ev {
            EngineEvent::Frame(f) => self.apply_frame(f),
            EngineEvent::WsStatus(s) => {
                let changed = self.ws_status != s;
                self.ws_status = s;
                changed
            }
            EngineEvent::Peers(p) => {
                self.peers = p;
                self.peers.sort_by(|a, b| a.node_name.cmp(&b.node_name));
                self.clamp();
                true
            }
            EngineEvent::TraceBacklog(events) => {
                // Oldest first; keep within cap.
                for e in events {
                    self.push_trace(e);
                }
                true
            }
            EngineEvent::Notice(msg) => {
                self.notice = Some(msg);
                true
            }
        }
    }

    pub fn apply_frame(&mut self, frame: Frame) -> bool {
        match frame {
            Frame::Hello { node_name, .. } => {
                self.node_name = node_name;
                true
            }
            Frame::QueueSnapshot(items) => {
                self.queue = items;
                self.sort_queue();
                self.clamp();
                true
            }
            Frame::QueueDelta { item, .. } => {
                self.upsert_queue(item);
                self.clamp();
                true
            }
            Frame::ChatSnapshot(sessions) => {
                self.sessions = sessions;
                self.clamp();
                true
            }
            Frame::ChatDelta {
                session,
                session_id,
                subtask,
                ..
            } => {
                if let Some(s) = session {
                    self.upsert_session(s);
                }
                if let (Some(sid), Some(st)) = (session_id, subtask) {
                    self.upsert_subtask(&sid, st);
                }
                self.clamp();
                true
            }
            Frame::Trace(ev) | Frame::Metric(ev) => {
                self.push_trace(ev);
                true
            }
            Frame::Alert(a) => {
                let key = (a.node_id.clone(), a.field.clone());
                if a.state == "cleared" {
                    self.alerts.remove(&key);
                } else {
                    self.alerts.insert(key, a);
                }
                self.clamp();
                true
            }
            Frame::Other(_) => false,
        }
    }

    fn sort_queue(&mut self) {
        self.queue
            .sort_by(|a, b| (a.created_at_ms, &a.id).cmp(&(b.created_at_ms, &b.id)));
    }

    fn upsert_queue(&mut self, item: QueueItem) {
        if let Some(slot) = self.queue.iter_mut().find(|q| q.id == item.id) {
            *slot = item;
        } else {
            self.queue.push(item);
        }
        self.sort_queue();
    }

    fn upsert_session(&mut self, session: ChatSession) {
        if let Some(slot) = self.sessions.iter_mut().find(|s| s.id == session.id) {
            *slot = session;
        } else {
            self.sessions.push(session);
        }
    }

    fn upsert_subtask(&mut self, sid: &str, st: ChatSubtask) {
        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == sid) {
            if let Some(slot) = session.subtasks.iter_mut().find(|s| s.id == st.id) {
                *slot = st;
            } else {
                session.subtasks.push(st);
                session.subtasks.sort_by_key(|s| s.index);
            }
        }
    }

    fn push_trace(&mut self, ev: TraceEvent) {
        self.trace.push_back(ev);
        while self.trace.len() > TRACE_CAP {
            self.trace.pop_front();
        }
        if self.trace_follow {
            self.trace_sel = self.trace.len().saturating_sub(1);
        }
    }

    // ── selection helpers ─────────────────────────────────────────────────────
    /// Items in the currently-selected queue column.
    pub fn queue_column_items(&self, col: usize) -> Vec<&QueueItem> {
        let status = QUEUE_COLUMNS[col.min(QUEUE_COLUMNS.len() - 1)];
        self.queue.iter().filter(|q| q.status == status).collect()
    }

    pub fn selected_queue_item(&self) -> Option<&QueueItem> {
        let items = self.queue_column_items(self.queue_col);
        items.get(self.queue_row).copied()
    }

    pub fn selected_session(&self) -> Option<&ChatSession> {
        self.sessions.get(self.chat_sel)
    }

    pub fn selected_subtask(&self) -> Option<&ChatSubtask> {
        self.selected_session()
            .and_then(|s| s.subtasks.get(self.chat_subtask_sel))
    }

    pub fn alert_list(&self) -> Vec<&Alert> {
        self.alerts.values().collect()
    }

    fn clamp(&mut self) {
        let col_len = self.queue_column_items(self.queue_col).len();
        self.queue_row = self.queue_row.min(col_len.saturating_sub(1));
        if self.queue_col >= QUEUE_COLUMNS.len() {
            self.queue_col = QUEUE_COLUMNS.len() - 1;
        }
        self.chat_sel = self.chat_sel.min(self.sessions.len().saturating_sub(1));
        let sub_len = self
            .selected_session()
            .map(|s| s.subtasks.len())
            .unwrap_or(0);
        self.chat_subtask_sel = self.chat_subtask_sel.min(sub_len.saturating_sub(1));
        self.specs_sel = self.specs_sel.min(self.peers.len().saturating_sub(1));
        self.alert_sel = self.alert_sel.min(self.alerts.len().saturating_sub(1));
        if self.trace.is_empty() {
            self.trace_sel = 0;
        } else {
            self.trace_sel = self.trace_sel.min(self.trace.len() - 1);
        }
    }

    // ── input handling ────────────────────────────────────────────────────────
    /// Handle a key. Mutates navigation/input state in place; returns any
    /// gateway [`Action`]s the async layer must perform.
    pub fn on_key(&mut self, key: KeyEvent) -> Vec<Action> {
        // Clear a stale notice on the next keystroke.
        self.notice = None;

        // Global: Ctrl-C always quits.
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.should_quit = true;
            return vec![];
        }

        // Modal input captures everything else.
        if self.input.is_some() {
            return self.on_key_input(key);
        }

        match key.code {
            KeyCode::Char('q') => self.should_quit = true,
            KeyCode::Char('?') => self.active = Pane::Help,
            KeyCode::Tab => self.cycle_pane(1),
            KeyCode::BackTab => self.cycle_pane(-1),
            KeyCode::Char(c @ '1'..='5') => {
                let idx = (c as u8 - b'1') as usize;
                self.active = Pane::TABS[idx];
                self.clamp();
            }
            _ => return self.on_key_pane(key),
        }
        vec![]
    }

    fn cycle_pane(&mut self, dir: i32) {
        let cur = Pane::TABS
            .iter()
            .position(|&p| p == self.active)
            .unwrap_or(0) as i32;
        let n = Pane::TABS.len() as i32;
        let next = ((cur + dir) % n + n) % n;
        self.active = Pane::TABS[next as usize];
        self.clamp();
    }

    fn on_key_input(&mut self, key: KeyEvent) -> Vec<Action> {
        let Some(input) = self.input.as_mut() else {
            return vec![];
        };
        match key.code {
            KeyCode::Esc => {
                self.input = None;
            }
            KeyCode::Enter => {
                let state = self.input.take().unwrap();
                let text = state.buffer.trim().to_string();
                if text.is_empty() {
                    return vec![];
                }
                return match state.purpose {
                    InputPurpose::ChatSubmit => vec![Action::ChatSubmit { prompt: text }],
                    InputPurpose::QueueEdit { id } => vec![Action::QueueEdit { id, text }],
                    InputPurpose::ChatEdit { sid, stid } => {
                        vec![Action::ChatEdit { sid, stid, text }]
                    }
                };
            }
            KeyCode::Backspace => {
                input.buffer.pop();
            }
            KeyCode::Char(c) => input.buffer.push(c),
            _ => {}
        }
        vec![]
    }

    fn on_key_pane(&mut self, key: KeyEvent) -> Vec<Action> {
        match self.active {
            Pane::Queue => self.on_key_queue(key),
            Pane::Chat => self.on_key_chat(key),
            Pane::Specs => {
                self.move_sel(&mut Self::specs_delta, key);
                vec![]
            }
            Pane::Trace => {
                self.on_key_trace(key);
                vec![]
            }
            Pane::Alerts => self.on_key_alerts(key),
            Pane::Help => vec![],
        }
    }

    // Each pane reads/writes its own selection; factored out where it helps.
    fn specs_delta(&mut self, d: i32) {
        self.specs_sel = step(self.specs_sel, d, self.peers.len());
    }

    fn move_sel(&mut self, f: &mut dyn FnMut(&mut Self, i32), key: KeyEvent) {
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => f(self, -1),
            KeyCode::Down | KeyCode::Char('j') => f(self, 1),
            _ => {}
        }
    }

    fn on_key_queue(&mut self, key: KeyEvent) -> Vec<Action> {
        match key.code {
            KeyCode::Left | KeyCode::Char('h') => {
                self.queue_col = self.queue_col.saturating_sub(1);
                self.queue_row = 0;
            }
            KeyCode::Right | KeyCode::Char('l') => {
                self.queue_col = (self.queue_col + 1).min(QUEUE_COLUMNS.len() - 1);
                self.queue_row = 0;
            }
            KeyCode::Up | KeyCode::Char('k') => {
                let n = self.queue_column_items(self.queue_col).len();
                self.queue_row = step(self.queue_row, -1, n);
            }
            KeyCode::Down | KeyCode::Char('j') => {
                let n = self.queue_column_items(self.queue_col).len();
                self.queue_row = step(self.queue_row, 1, n);
            }
            KeyCode::Char('a') => {
                if let Some(item) = self.selected_queue_item() {
                    if item.status == "proposed" {
                        return vec![Action::QueueApprove(item.id.clone())];
                    }
                    self.notice = Some("approve only applies to a proposed item".into());
                }
            }
            KeyCode::Char('y') => {
                if let Some(item) = self.selected_queue_item() {
                    if item.status == "drafted" {
                        return vec![Action::QueueAccept(item.id.clone())];
                    }
                    self.notice = Some("accept only applies to a drafted item".into());
                }
            }
            KeyCode::Char('n') => {
                if let Some(item) = self.selected_queue_item() {
                    if item.status == "drafted" {
                        return vec![Action::QueueReject(item.id.clone())];
                    }
                    self.notice = Some("reject only applies to a drafted item".into());
                }
            }
            KeyCode::Char('e') => {
                if let Some(item) = self.selected_queue_item() {
                    if item.status == "drafted" {
                        let id = item.id.clone();
                        self.open_input(
                            format!("edit answer for {id}"),
                            InputPurpose::QueueEdit { id },
                        );
                    } else {
                        self.notice = Some("edit only applies to a drafted item".into());
                    }
                }
            }
            _ => {}
        }
        vec![]
    }

    fn on_key_chat(&mut self, key: KeyEvent) -> Vec<Action> {
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                self.chat_sel = step(self.chat_sel, -1, self.sessions.len());
                self.chat_subtask_sel = 0;
            }
            KeyCode::Down | KeyCode::Char('j') => {
                self.chat_sel = step(self.chat_sel, 1, self.sessions.len());
                self.chat_subtask_sel = 0;
            }
            KeyCode::Left | KeyCode::Char('h') => {
                let n = self
                    .selected_session()
                    .map(|s| s.subtasks.len())
                    .unwrap_or(0);
                self.chat_subtask_sel = step(self.chat_subtask_sel, -1, n);
            }
            KeyCode::Right | KeyCode::Char('l') => {
                let n = self
                    .selected_session()
                    .map(|s| s.subtasks.len())
                    .unwrap_or(0);
                self.chat_subtask_sel = step(self.chat_subtask_sel, 1, n);
            }
            KeyCode::Char('i') => {
                self.open_input("new research prompt".into(), InputPurpose::ChatSubmit);
            }
            KeyCode::Char('y') => return self.chat_thumb('y'),
            KeyCode::Char('n') => return self.chat_thumb('n'),
            KeyCode::Char('e') => return self.chat_thumb('e'),
            _ => {}
        }
        vec![]
    }

    fn chat_thumb(&mut self, kind: char) -> Vec<Action> {
        let (sid, stid, status) = match (self.selected_session(), self.selected_subtask()) {
            (Some(s), Some(st)) => (s.id.clone(), st.id.clone(), st.status.clone()),
            _ => return vec![],
        };
        if status != "completed" {
            self.notice = Some("only a completed subtask can be thumbed".into());
            return vec![];
        }
        match kind {
            'y' => vec![Action::ChatAccept { sid, stid }],
            'n' => vec![Action::ChatReject { sid, stid }],
            'e' => {
                self.open_input(
                    format!("edit answer for {stid}"),
                    InputPurpose::ChatEdit { sid, stid },
                );
                vec![]
            }
            _ => vec![],
        }
    }

    fn on_key_trace(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                self.trace_follow = false;
                self.trace_sel = self.trace_sel.saturating_sub(1);
            }
            KeyCode::Down | KeyCode::Char('j') => {
                if !self.trace.is_empty() {
                    self.trace_sel = (self.trace_sel + 1).min(self.trace.len() - 1);
                }
                if self.trace_sel == self.trace.len().saturating_sub(1) {
                    self.trace_follow = true;
                }
            }
            KeyCode::Char('f') => {
                self.trace_follow = !self.trace_follow;
                if self.trace_follow {
                    self.trace_sel = self.trace.len().saturating_sub(1);
                }
            }
            _ => {}
        }
    }

    fn on_key_alerts(&mut self, key: KeyEvent) -> Vec<Action> {
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                self.alert_sel = step(self.alert_sel, -1, self.alerts.len());
            }
            KeyCode::Down | KeyCode::Char('j') => {
                self.alert_sel = step(self.alert_sel, 1, self.alerts.len());
            }
            KeyCode::Char('s') => {
                if let Some(a) = self.alert_list().get(self.alert_sel) {
                    return vec![Action::Snooze {
                        node_id: a.node_id.clone(),
                        field: a.field.clone(),
                    }];
                }
            }
            _ => {}
        }
        vec![]
    }

    fn open_input(&mut self, label: String, purpose: InputPurpose) {
        self.input = Some(InputState {
            label,
            buffer: String::new(),
            purpose,
        });
    }
}

/// Move a selection index by `d` within `[0, len)`, saturating at the ends.
fn step(idx: usize, d: i32, len: usize) -> usize {
    if len == 0 {
        return 0;
    }
    let max = len - 1;
    if d < 0 {
        idx.saturating_sub((-d) as usize)
    } else {
        (idx + d as usize).min(max)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::parse_frame;

    fn key(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
    }
    fn keycode(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn snapshot(items: &str) -> Frame {
        parse_frame(&format!(
            r#"{{"type":"queue.snapshot","timestamp_ms":1,"items":{items}}}"#
        ))
        .unwrap()
    }

    fn item(id: &str, status: &str, created: i64) -> String {
        format!(
            r#"{{"id":"{id}","prompt":"p {id}","specialty":"research","status":"{status}",
            "proposed_by":"operator","episode_id":"e{id}","consumed_upstreams":[],
            "created_at_ms":{created},"decision":null,"corrected_answer":null}}"#
        )
    }

    #[test]
    fn queue_snapshot_then_columns() {
        let mut app = App::new();
        let items = format!(
            "[{},{},{}]",
            item("a", "proposed", 1),
            item("b", "drafted", 2),
            item("c", "proposed", 3)
        );
        app.apply_frame(snapshot(&items));
        assert_eq!(app.queue_column_items(0).len(), 2); // proposed: a, c
        assert_eq!(app.queue_column_items(3).len(), 1); // drafted: b
                                                        // sorted by created_at_ms within the column
        assert_eq!(app.queue_column_items(0)[0].id, "a");
        assert_eq!(app.queue_column_items(0)[1].id, "c");
    }

    #[test]
    fn approve_emits_action_only_for_proposed() {
        let mut app = App::new();
        let items = format!("[{}]", item("a", "proposed", 1));
        app.apply_frame(snapshot(&items));
        app.active = Pane::Queue;
        let actions = app.on_key(key('a'));
        assert_eq!(actions, vec![Action::QueueApprove("a".into())]);
    }

    #[test]
    fn accept_only_on_drafted() {
        let mut app = App::new();
        let items = format!("[{}]", item("b", "drafted", 1));
        app.apply_frame(snapshot(&items));
        app.active = Pane::Queue;
        // move to drafted column (index 3)
        app.on_key(keycode(KeyCode::Right));
        app.on_key(keycode(KeyCode::Right));
        app.on_key(keycode(KeyCode::Right));
        let actions = app.on_key(key('y'));
        assert_eq!(actions, vec![Action::QueueAccept("b".into())]);
        // 'a' (approve) on a drafted item produces no action, sets a notice
        let none = app.on_key(key('a'));
        assert!(none.is_empty());
        assert!(app.notice.is_some());
    }

    #[test]
    fn queue_delta_upserts() {
        let mut app = App::new();
        app.apply_frame(snapshot(&format!("[{}]", item("a", "proposed", 1))));
        let delta = parse_frame(&format!(
            r#"{{"type":"queue.delta","action":"approve","timestamp_ms":2,"item":{}}}"#,
            item("a", "approved", 1)
        ))
        .unwrap();
        app.apply_frame(delta);
        assert_eq!(app.queue_column_items(0).len(), 0); // no longer proposed
        assert_eq!(app.queue_column_items(1).len(), 1); // now approved
    }

    #[test]
    fn chat_submit_input_flow() {
        let mut app = App::new();
        app.active = Pane::Chat;
        app.on_key(key('i')); // open input
        assert!(app.input.is_some());
        for c in "hello world".chars() {
            app.on_key(key(c));
        }
        let actions = app.on_key(keycode(KeyCode::Enter));
        assert_eq!(
            actions,
            vec![Action::ChatSubmit {
                prompt: "hello world".into()
            }]
        );
        assert!(app.input.is_none());
    }

    #[test]
    fn esc_cancels_input_no_action() {
        let mut app = App::new();
        app.active = Pane::Chat;
        app.on_key(key('i'));
        app.on_key(key('x'));
        let actions = app.on_key(keycode(KeyCode::Esc));
        assert!(actions.is_empty());
        assert!(app.input.is_none());
    }

    #[test]
    fn alert_insert_then_clear() {
        let mut app = App::new();
        let firing = parse_frame(
            r#"{"type":"alert","node_id":"jetson-1","node_name":"jetson-1","field":"temp_celsius",
            "severity":"danger","value":84.0,"threshold":82.0,"state":"alerting","fired_at_ms":1}"#,
        )
        .unwrap();
        app.apply_frame(firing);
        assert_eq!(app.alerts.len(), 1);
        let cleared = parse_frame(
            r#"{"type":"alert","node_id":"jetson-1","node_name":"jetson-1","field":"temp_celsius",
            "severity":"ok","value":60.0,"threshold":82.0,"state":"cleared","fired_at_ms":2}"#,
        )
        .unwrap();
        app.apply_frame(cleared);
        assert_eq!(app.alerts.len(), 0);
    }

    #[test]
    fn snooze_action_from_alerts_pane() {
        let mut app = App::new();
        app.apply_frame(
            parse_frame(
                r#"{"type":"alert","node_id":"jetson-2","node_name":"jetson-2","field":"disk_pct",
                "severity":"warn","value":90.0,"threshold":85.0,"state":"alerting","fired_at_ms":1}"#,
            )
            .unwrap(),
        );
        app.active = Pane::Alerts;
        let actions = app.on_key(key('s'));
        assert_eq!(
            actions,
            vec![Action::Snooze {
                node_id: "jetson-2".into(),
                field: "disk_pct".into()
            }]
        );
    }

    #[test]
    fn pane_switch_by_number_and_tab() {
        let mut app = App::new();
        app.on_key(key('3'));
        assert_eq!(app.active, Pane::Specs);
        app.on_key(keycode(KeyCode::Tab));
        assert_eq!(app.active, Pane::Trace);
        app.on_key(key('q'));
        assert!(app.should_quit);
    }

    #[test]
    fn trace_ring_is_bounded() {
        let mut app = App::new();
        for i in 0..(TRACE_CAP + 50) {
            app.apply_frame(
                parse_frame(&format!(
                    r#"{{"type":"message_trace","node_name":"n","event_type":"t","timestamp_ms":{i}}}"#
                ))
                .unwrap(),
            );
        }
        assert_eq!(app.trace.len(), TRACE_CAP);
    }
}

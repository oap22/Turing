//! Event plumbing between the async I/O tasks and the render loop.
//!
//! Input (keys/resize) is read directly from crossterm's `EventStream` inside
//! the main `select!`. Everything else — decoded WS frames, connection status,
//! specs polls, transient notices — arrives as an [`EngineEvent`] over an mpsc
//! channel. The render loop only redraws when an event actually changes state,
//! so an idle TUI costs ~0 CPU.

use crate::model::{Frame, Peer, TraceEvent};

// Frame is the large variant; this is a low-frequency control channel, so the
// size delta is immaterial and boxing would only add indirection.
#[allow(clippy::large_enum_variant)]
#[derive(Debug)]
pub enum EngineEvent {
    /// A decoded WebSocket frame (queue/chat/alert/trace/metric/hello).
    Frame(Frame),
    /// WebSocket connection status changed.
    WsStatus(WsStatus),
    /// Fresh `/peers` poll (fleet specs).
    Peers(Vec<Peer>),
    /// Initial `/api/events` backlog for the trace pane.
    TraceBacklog(Vec<TraceEvent>),
    /// A transient one-line status message (action result, error).
    Notice(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WsStatus {
    Connecting,
    Connected,
    Disconnected(String),
}

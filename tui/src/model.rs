//! Wire types + frame parsing for the Turing gateway.
//!
//! Every struct here mirrors the exact JSON the FastAPI gateway emits — see
//! `src/turing/gateway/queue_manager.py` (`QueueItem.to_frame`),
//! `chat_manager.py` (`ChatSession`/`ChatSubtask.to_frame`),
//! `src/turing/specs/collector.py` (`NodeSpecs.to_dict`),
//! `src/turing/coordinator/alerts/types.py` (`Alert.to_frame`), and the
//! `/peers` / `/api/events` responses in `app.py`.
//!
//! Parsing is deliberately tolerant: unknown frame types degrade to
//! [`Frame::Other`] and missing optional fields default, so a gateway that
//! grows a field never crashes the TUI. The functions are pure and covered by
//! unit tests at the bottom of this file.

use serde::Deserialize;

// ── thresholds (kept in lock-step with webui/src/specs/thresholds.ts and
// src/turing/coordinator/alerts/types.py) ────────────────────────────────────
pub const TEMP_WARN: f64 = 75.0;
pub const TEMP_DANGER: f64 = 82.0;
pub const DISK_WARN: f64 = 85.0;
pub const DISK_DANGER: f64 = 95.0;

/// Severity grade for a live reading — drives row colouring, mirroring the
/// webui's `severity_for_temp` / `severity_for_disk`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Ok,
    Warn,
    Danger,
}

pub fn severity_for_temp(temp: Option<f64>) -> Severity {
    match temp {
        Some(t) if t >= TEMP_DANGER => Severity::Danger,
        Some(t) if t >= TEMP_WARN => Severity::Warn,
        _ => Severity::Ok,
    }
}

pub fn disk_percent(used: Option<u64>, total: Option<u64>) -> Option<f64> {
    match total {
        Some(t) if t > 0 => Some(100.0 * used.unwrap_or(0) as f64 / t as f64),
        _ => None,
    }
}

pub fn severity_for_disk(pct: Option<f64>) -> Severity {
    match pct {
        Some(p) if p >= DISK_DANGER => Severity::Danger,
        Some(p) if p >= DISK_WARN => Severity::Warn,
        _ => Severity::Ok,
    }
}

// ── queue ────────────────────────────────────────────────────────────────────
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct QueueItem {
    pub id: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub specialty: String,
    #[serde(default = "default_proposed")]
    pub status: String,
    #[serde(default)]
    pub proposed_by: String,
    #[serde(default)]
    pub episode_id: Option<String>,
    #[serde(default)]
    pub created_at_ms: i64,
    #[serde(default)]
    pub decision: Option<String>,
    #[serde(default)]
    pub corrected_answer: Option<String>,
}

fn default_proposed() -> String {
    "proposed".to_string()
}

/// The five frontier columns, left → right, matching ADR 0010 §3.
pub const QUEUE_COLUMNS: [&str; 5] = ["proposed", "approved", "in-flight", "drafted", "curated"];

// ── chat ─────────────────────────────────────────────────────────────────────
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct ChatSubtask {
    pub id: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub index: i64,
    #[serde(default)]
    pub specialty: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub content: String,
    #[serde(default = "default_pending")]
    pub status: String,
    #[serde(default)]
    pub decision: Option<String>,
}

fn default_pending() -> String {
    "pending".to_string()
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct ChatSession {
    pub id: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub specialty: String,
    #[serde(default)]
    pub created_at_ms: i64,
    #[serde(default)]
    pub subtasks: Vec<ChatSubtask>,
}

// ── specs / peers ─────────────────────────────────────────────────────────────
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct NodeSpecs {
    #[serde(default)]
    pub model_name: String,
    #[serde(default)]
    pub arch: String,
    #[serde(default)]
    pub cpu_percent: f64,
    #[serde(default)]
    pub mem_used_bytes: u64,
    #[serde(default)]
    pub ram_total_bytes: u64,
    #[serde(default)]
    pub disk_used_bytes: u64,
    #[serde(default)]
    pub disk_total_bytes: u64,
    #[serde(default)]
    pub temp_celsius: Option<f64>,
    #[serde(default)]
    pub uptime_seconds: u64,
    #[serde(default)]
    pub loadavg_1m: f64,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Peer {
    #[serde(default)]
    pub node_id: String,
    #[serde(default)]
    pub node_name: String,
    #[serde(default)]
    pub is_self: bool,
    #[serde(default)]
    pub specs: Option<NodeSpecs>,
    #[serde(default)]
    pub stale: bool,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct PeersResponse {
    #[serde(default)]
    pub peers: Vec<PeerRaw>,
}

/// `/peers` uses the JSON key `self`, a Rust keyword — capture it here then
/// fold into [`Peer`] via [`PeerRaw::into_peer`].
#[derive(Debug, Clone, Deserialize)]
pub struct PeerRaw {
    #[serde(default)]
    pub node_id: String,
    #[serde(default)]
    pub node_name: String,
    #[serde(default, rename = "self")]
    pub is_self: bool,
    #[serde(default)]
    pub specs: Option<NodeSpecs>,
    #[serde(default)]
    pub stale: bool,
}

impl PeerRaw {
    pub fn into_peer(self) -> Peer {
        Peer {
            node_id: self.node_id,
            node_name: self.node_name,
            is_self: self.is_self,
            specs: self.specs,
            stale: self.stale,
        }
    }
}

// ── trace / events ────────────────────────────────────────────────────────────
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct TraceEvent {
    #[serde(default)]
    pub node_name: String,
    #[serde(default)]
    pub event_type: String,
    #[serde(default)]
    pub timestamp_ms: i64,
    #[serde(default)]
    pub duration_ms: Option<f64>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct EventsResponse {
    #[serde(default)]
    pub events: Vec<TraceEvent>,
}

// ── alerts ────────────────────────────────────────────────────────────────────
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Alert {
    #[serde(default)]
    pub node_id: String,
    #[serde(default)]
    pub node_name: String,
    #[serde(default)]
    pub field: String,
    #[serde(default)]
    pub severity: String,
    #[serde(default)]
    pub value: f64,
    #[serde(default)]
    pub threshold: f64,
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub snoozed_until_ms: Option<i64>,
}

// ── frames ────────────────────────────────────────────────────────────────────
/// One decoded WebSocket frame. [`Frame::Other`] carries the unrecognised
/// `type` string so the status line can show that traffic arrived without the
/// TUI having to model every diagnostic frame (gap_marker, stream_reset…).
///
/// A few fields (`uptime_s`, the delta `action`s, the `Other` payload) are
/// parsed to mirror the wire protocol exactly but aren't all rendered yet —
/// kept so the decoder is faithful and forward-compatible.
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub enum Frame {
    Hello {
        node_name: String,
        uptime_s: u64,
    },
    QueueSnapshot(Vec<QueueItem>),
    QueueDelta {
        action: String,
        item: QueueItem,
    },
    ChatSnapshot(Vec<ChatSession>),
    ChatDelta {
        action: String,
        session: Option<ChatSession>,
        session_id: Option<String>,
        subtask: Option<ChatSubtask>,
    },
    Trace(TraceEvent),
    Metric(TraceEvent),
    Alert(Alert),
    Other(String),
}

/// Parse one raw WS text frame. Returns `None` only when the payload is not
/// JSON or has no string `type` — everything else maps to a [`Frame`] variant
/// (unknown types → [`Frame::Other`]).
pub fn parse_frame(raw: &str) -> Option<Frame> {
    let v: serde_json::Value = serde_json::from_str(raw).ok()?;
    let ty = v.get("type")?.as_str()?.to_string();
    let frame = match ty.as_str() {
        "hello" => Frame::Hello {
            node_name: v
                .get("node_name")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            // uptime_s is a float on the wire (monotonic-clock delta), so read
            // it as f64 and truncate — as_u64() returns None for a non-integer.
            uptime_s: v
                .get("uptime_s")
                .and_then(|x| x.as_f64())
                .map(|f| f as u64)
                .unwrap_or(0),
        },
        "queue.snapshot" => {
            let items = v.get("items").cloned().unwrap_or_default();
            Frame::QueueSnapshot(serde_json::from_value(items).unwrap_or_default())
        }
        "queue.delta" => {
            let item = serde_json::from_value(v.get("item").cloned().unwrap_or_default()).ok()?;
            Frame::QueueDelta {
                action: v
                    .get("action")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string(),
                item,
            }
        }
        "chat.snapshot" => {
            let sessions = v.get("sessions").cloned().unwrap_or_default();
            Frame::ChatSnapshot(serde_json::from_value(sessions).unwrap_or_default())
        }
        "chat.delta" => Frame::ChatDelta {
            action: v
                .get("action")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            session: v
                .get("session")
                .cloned()
                .and_then(|s| serde_json::from_value(s).ok()),
            session_id: v
                .get("session_id")
                .and_then(|x| x.as_str())
                .map(String::from),
            subtask: v
                .get("subtask")
                .cloned()
                .and_then(|s| serde_json::from_value(s).ok()),
        },
        "message_trace" => Frame::Trace(serde_json::from_value(v).unwrap_or(TraceEvent {
            node_name: String::new(),
            event_type: "message_trace".into(),
            timestamp_ms: 0,
            duration_ms: None,
            error: None,
        })),
        "metric" => Frame::Metric(serde_json::from_value(v).unwrap_or(TraceEvent {
            node_name: String::new(),
            event_type: "metric".into(),
            timestamp_ms: 0,
            duration_ms: None,
            error: None,
        })),
        "alert" => Frame::Alert(serde_json::from_value(v).ok()?),
        other => Frame::Other(other.to_string()),
    };
    Some(frame)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hello() {
        let f = parse_frame(r#"{"type":"hello","node_name":"pi-alpha","uptime_s":42}"#).unwrap();
        match f {
            Frame::Hello {
                node_name,
                uptime_s,
            } => {
                assert_eq!(node_name, "pi-alpha");
                assert_eq!(uptime_s, 42);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn parses_hello_float_uptime() {
        // The gateway emits uptime_s as a float (monotonic delta).
        let f = parse_frame(r#"{"type":"hello","node_name":"n","uptime_s":42.9}"#).unwrap();
        match f {
            Frame::Hello { uptime_s, .. } => assert_eq!(uptime_s, 42),
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn parses_queue_snapshot_with_real_shape() {
        let raw = r#"{"type":"queue.snapshot","timestamp_ms":1,"items":[
            {"id":"q1","prompt":"rope scaling?","specialty":"research","status":"drafted",
             "origin_task_id":null,"origin_question_id":null,"proposed_by":"operator",
             "episode_id":"e1","consumed_upstreams":[],"created_at_ms":10,
             "approved_at_ms":null,"dispatched_at_ms":null,"drafted_at_ms":20,
             "curated_at_ms":null,"decision":null,"corrected_answer":null}]}"#;
        match parse_frame(raw).unwrap() {
            Frame::QueueSnapshot(items) => {
                assert_eq!(items.len(), 1);
                assert_eq!(items[0].id, "q1");
                assert_eq!(items[0].status, "drafted");
                assert_eq!(items[0].episode_id.as_deref(), Some("e1"));
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn parses_queue_delta() {
        let raw = r#"{"type":"queue.delta","action":"accept","timestamp_ms":1,
            "item":{"id":"q1","prompt":"p","specialty":"s","status":"curated",
            "proposed_by":"operator","consumed_upstreams":[],"created_at_ms":1,"decision":"accept"}}"#;
        match parse_frame(raw).unwrap() {
            Frame::QueueDelta { action, item } => {
                assert_eq!(action, "accept");
                assert_eq!(item.status, "curated");
                assert_eq!(item.decision.as_deref(), Some("accept"));
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn parses_chat_delta_subtask() {
        let raw = r#"{"type":"chat.delta","action":"stream","session_id":"s1","timestamp_ms":1,
            "subtask":{"id":"st1","session_id":"s1","index":0,"specialty":"research",
            "prompt":"","content":"partial","status":"streaming","consumed_upstreams":[]}}"#;
        match parse_frame(raw).unwrap() {
            Frame::ChatDelta {
                action,
                session_id,
                subtask,
                ..
            } => {
                assert_eq!(action, "stream");
                assert_eq!(session_id.as_deref(), Some("s1"));
                let st = subtask.unwrap();
                assert_eq!(st.content, "partial");
                assert_eq!(st.status, "streaming");
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn parses_alert() {
        let raw = r#"{"type":"alert","node_id":"jetson-2","node_name":"jetson-2",
            "field":"temp_celsius","severity":"danger","value":84.0,"threshold":82.0,
            "state":"alerting","fired_at_ms":1,"snoozed_until_ms":null}"#;
        match parse_frame(raw).unwrap() {
            Frame::Alert(a) => {
                assert_eq!(a.field, "temp_celsius");
                assert_eq!(a.severity, "danger");
                assert!((a.value - 84.0).abs() < 1e-9);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn unknown_type_is_other_not_error() {
        match parse_frame(r#"{"type":"gap_marker","node_name":"x"}"#).unwrap() {
            Frame::Other(t) => assert_eq!(t, "gap_marker"),
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn non_json_is_none() {
        assert!(parse_frame("not json").is_none());
        assert!(parse_frame(r#"{"no":"type"}"#).is_none());
    }

    #[test]
    fn severity_grading_matches_thresholds() {
        assert_eq!(severity_for_temp(Some(83.0)), Severity::Danger);
        assert_eq!(severity_for_temp(Some(76.0)), Severity::Warn);
        assert_eq!(severity_for_temp(Some(60.0)), Severity::Ok);
        assert_eq!(severity_for_temp(None), Severity::Ok);
        assert_eq!(
            severity_for_disk(disk_percent(Some(96), Some(100))),
            Severity::Danger
        );
        assert_eq!(
            severity_for_disk(disk_percent(Some(90), Some(100))),
            Severity::Warn
        );
        assert_eq!(
            severity_for_disk(disk_percent(Some(10), Some(100))),
            Severity::Ok
        );
        assert_eq!(
            severity_for_disk(disk_percent(Some(1), Some(0))),
            Severity::Ok
        );
    }

    #[test]
    fn peers_response_handles_self_keyword() {
        let raw = r#"{"peers":[{"node_id":"a","node_name":"pi-alpha","self":true,
            "capabilities":[],"last_seen":null,"specs":null,"stale":false}],"count":1}"#;
        let resp: PeersResponse = serde_json::from_str(raw).unwrap();
        assert_eq!(resp.peers.len(), 1);
        let peer = resp.peers.into_iter().next().unwrap().into_peer();
        assert!(peer.is_self);
        assert_eq!(peer.node_name, "pi-alpha");
    }
}

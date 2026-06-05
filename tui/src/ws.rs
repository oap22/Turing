//! WebSocket subscription task — long-lived, self-healing.
//!
//! Connects to the gateway `/ws` with `Authorization: Bearer`, decodes each
//! text frame via [`crate::model::parse_frame`], and forwards it as an
//! [`EngineEvent::Frame`]. On any disconnect it reports status and reconnects
//! with capped exponential backoff, so the operator surface rides through
//! coordinator restarts without intervention.

use std::time::Duration;

use futures_util::StreamExt;
use tokio::sync::mpsc::Sender;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::header::AUTHORIZATION;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;

use crate::event::{EngineEvent, WsStatus};
use crate::model::parse_frame;

const BACKOFF_START: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(10);

/// Run forever: (re)connect, pump frames, back off on failure. Returns only if
/// the event channel closes (the UI exited).
pub async fn run(ws_url: String, token: String, tx: Sender<EngineEvent>) {
    let mut backoff = BACKOFF_START;
    loop {
        let _ = tx.send(EngineEvent::WsStatus(WsStatus::Connecting)).await;
        match connect_and_pump(&ws_url, &token, &tx).await {
            Ok(()) => {
                // Clean server close — treat as a normal disconnect and retry.
                if tx
                    .send(EngineEvent::WsStatus(WsStatus::Disconnected(
                        "closed".into(),
                    )))
                    .await
                    .is_err()
                {
                    return;
                }
            }
            Err(why) => {
                if tx
                    .send(EngineEvent::WsStatus(WsStatus::Disconnected(why)))
                    .await
                    .is_err()
                {
                    return;
                }
            }
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(BACKOFF_MAX);
    }
}

async fn connect_and_pump(
    ws_url: &str,
    token: &str,
    tx: &Sender<EngineEvent>,
) -> Result<(), String> {
    let mut request = ws_url
        .into_client_request()
        .map_err(|e| format!("bad url: {e}"))?;
    if !token.is_empty() {
        let value = HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|e| format!("bad token: {e}"))?;
        request.headers_mut().insert(AUTHORIZATION, value);
    }

    let (stream, _resp) = tokio_tungstenite::connect_async(request)
        .await
        .map_err(|e| friendly(&e.to_string()))?;
    if tx
        .send(EngineEvent::WsStatus(WsStatus::Connected))
        .await
        .is_err()
    {
        return Ok(());
    }

    let (_write, mut read) = stream.split();
    while let Some(msg) = read.next().await {
        let msg = msg.map_err(|e| friendly(&e.to_string()))?;
        match msg {
            Message::Text(text) => {
                if let Some(frame) = parse_frame(&text) {
                    if tx.send(EngineEvent::Frame(frame)).await.is_err() {
                        return Ok(()); // UI gone
                    }
                }
            }
            Message::Close(_) => return Ok(()),
            Message::Ping(_) | Message::Pong(_) | Message::Binary(_) | Message::Frame(_) => {}
        }
    }
    Ok(())
}

/// Shorten the noisiest tungstenite errors for the one-line status bar.
fn friendly(raw: &str) -> String {
    if raw.contains("401") || raw.to_lowercase().contains("unauthorized") {
        "401 — check the bearer token".into()
    } else if raw.contains("Connection refused") || raw.contains("refused") {
        "connection refused — is the gateway up?".into()
    } else {
        raw.chars().take(60).collect()
    }
}

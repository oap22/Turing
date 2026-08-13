// Rust-side authenticated proxy to the `turing-gateway` HTTP + WS API. The
// bearer token lives only here — the webview sees proxied status/body pairs,
// never the token itself.

use futures_util::StreamExt;
use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Message;

use crate::config::AppConfig;

#[derive(Default)]
pub struct GatewayState {
    started: AtomicBool,
}

#[derive(Serialize)]
pub struct GwResponse {
    pub status: u16,
    pub body: String,
}

#[tauri::command]
pub async fn gateway_fetch(
    config: tauri::State<'_, AppConfig>,
    method: String,
    path: String,
    body: Option<String>,
) -> Result<GwResponse, String> {
    if !path.starts_with('/') {
        return Err("path must start with /".to_string());
    }
    let url = format!("{}{}", config.gateway_base, path);
    let client = reqwest::Client::new();
    let m = reqwest::Method::from_bytes(method.as_bytes()).map_err(|e| e.to_string())?;
    let mut req = client.request(m, &url).timeout(Duration::from_secs(10));
    if !config.gateway_token.is_empty() {
        req = req.header("Authorization", format!("Bearer {}", config.gateway_token));
    }
    if let Some(b) = body {
        req = req.header("Content-Type", "application/json").body(b);
    }
    let resp = req.send().await.map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    Ok(GwResponse { status, body: text })
}

#[derive(Clone, Serialize)]
struct GatewayStatusPayload {
    status: &'static str,
}

#[derive(Clone, Serialize)]
struct GatewayFramePayload(String);

fn ws_url(base: &str) -> String {
    if let Some(rest) = base.strip_prefix("https://") {
        format!("wss://{rest}/ws")
    } else if let Some(rest) = base.strip_prefix("http://") {
        format!("ws://{rest}/ws")
    } else {
        format!("ws://{base}/ws")
    }
}

#[tauri::command]
pub fn gateway_ws_start(
    app: AppHandle,
    state: tauri::State<'_, GatewayState>,
    config: tauri::State<'_, AppConfig>,
) -> Result<(), String> {
    if state.started.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    let base = config.gateway_base.clone();
    let token = config.gateway_token.clone();
    let url = ws_url(&base);

    tauri::async_runtime::spawn(async move {
        let mut backoff = Duration::from_secs(1);
        let max_backoff = Duration::from_secs(30);
        loop {
            let mut request = match url.clone().into_client_request() {
                Ok(r) => r,
                Err(_) => {
                    tokio::time::sleep(backoff).await;
                    continue;
                }
            };
            request.headers_mut().remove("Origin");
            if !token.is_empty() {
                if let Ok(val) = format!("Bearer {token}").parse() {
                    request.headers_mut().insert("Authorization", val);
                }
            }

            match tokio_tungstenite::connect_async(request).await {
                Ok((ws_stream, _)) => {
                    let _ = app.emit("gateway-status", GatewayStatusPayload { status: "open" });
                    backoff = Duration::from_secs(1);
                    let (mut _write, mut read) = ws_stream.split();
                    loop {
                        match read.next().await {
                            Some(Ok(Message::Text(text))) => {
                                let _ = app.emit("gateway-frame", GatewayFramePayload(text));
                            }
                            Some(Ok(Message::Close(_))) | None => break,
                            Some(Ok(_)) => {}
                            Some(Err(_)) => break,
                        }
                    }
                    let _ = app.emit("gateway-status", GatewayStatusPayload { status: "closed" });
                }
                Err(_) => {
                    let _ = app.emit("gateway-status", GatewayStatusPayload { status: "closed" });
                }
            }
            let _ = app.emit(
                "gateway-status",
                GatewayStatusPayload {
                    status: "reconnecting",
                },
            );
            tokio::time::sleep(backoff).await;
            backoff = std::cmp::min(backoff * 2, max_backoff);
        }
    });

    Ok(())
}

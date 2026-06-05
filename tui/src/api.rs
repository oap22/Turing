//! HTTP client for the gateway's REST surface.
//!
//! State arrives over the WebSocket ([`crate::ws`]); this client issues the
//! operator's *actions* (approve/curate, chat submit/thumb, snooze) and the two
//! pulls the gateway does not push: `/peers` (specs) and `/api/events` (initial
//! trace backlog). Every request carries `Authorization: Bearer <token>`.

use anyhow::{Context, Result};
use reqwest::Client;
use serde_json::json;

use crate::model::{EventsResponse, Peer, PeersResponse};

#[derive(Clone)]
pub struct GatewayClient {
    http: Client,
    base: String,
    token: String,
}

impl GatewayClient {
    pub fn new(base: String, token: String) -> Result<Self> {
        let http = Client::builder()
            .user_agent("turing-tui")
            .build()
            .context("building HTTP client")?;
        Ok(Self { http, base, token })
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base, path)
    }

    async fn post(&self, path: &str, body: Option<serde_json::Value>) -> Result<()> {
        let mut req = self.http.post(self.url(path)).bearer_auth(&self.token);
        if let Some(b) = body {
            req = req.json(&b);
        }
        let resp = req.send().await.with_context(|| format!("POST {path}"))?;
        if !resp.status().is_success() {
            anyhow::bail!("POST {path} → {}", resp.status());
        }
        Ok(())
    }

    // ── queue actions ────────────────────────────────────────────────────────
    pub async fn queue_approve(&self, id: &str) -> Result<()> {
        self.post(&format!("/api/queue/approve/{id}"), None).await
    }
    pub async fn queue_accept(&self, id: &str) -> Result<()> {
        self.post(&format!("/api/queue/accept/{id}"), None).await
    }
    pub async fn queue_reject(&self, id: &str) -> Result<()> {
        self.post(&format!("/api/queue/reject/{id}"), None).await
    }
    pub async fn queue_edit(&self, id: &str, corrected: &str) -> Result<()> {
        self.post(
            &format!("/api/queue/edit/{id}"),
            Some(json!({ "corrected_answer": corrected })),
        )
        .await
    }

    // ── chat actions ─────────────────────────────────────────────────────────
    pub async fn chat_submit(&self, prompt: &str, specialty: Option<&str>) -> Result<()> {
        let mut body = json!({ "prompt": prompt });
        if let Some(s) = specialty {
            body["specialty"] = json!(s);
        }
        self.post("/api/chat/submit", Some(body)).await
    }
    pub async fn chat_accept(&self, sid: &str, stid: &str) -> Result<()> {
        self.post(&format!("/api/chat/{sid}/{stid}/accept"), None)
            .await
    }
    pub async fn chat_reject(&self, sid: &str, stid: &str) -> Result<()> {
        self.post(&format!("/api/chat/{sid}/{stid}/reject"), None)
            .await
    }
    pub async fn chat_edit(&self, sid: &str, stid: &str, corrected: &str) -> Result<()> {
        self.post(
            &format!("/api/chat/{sid}/{stid}/edit"),
            Some(json!({ "corrected_answer": corrected })),
        )
        .await
    }

    // ── alerts ───────────────────────────────────────────────────────────────
    pub async fn snooze(&self, node_id: &str, field: &str) -> Result<()> {
        self.post(&format!("/alerts/{node_id}/{field}/snooze"), None)
            .await
    }

    // ── pulls (specs + trace backlog) ────────────────────────────────────────
    pub async fn peers(&self) -> Result<Vec<Peer>> {
        let resp = self
            .http
            .get(self.url("/peers"))
            .bearer_auth(&self.token)
            .send()
            .await
            .context("GET /peers")?
            .error_for_status()
            .context("GET /peers status")?
            .json::<PeersResponse>()
            .await
            .context("decode /peers")?;
        Ok(resp.peers.into_iter().map(|p| p.into_peer()).collect())
    }

    pub async fn recent_events(&self, limit: u32) -> Result<EventsResponse> {
        let resp = self
            .http
            .get(self.url("/api/events"))
            .query(&[("limit", limit.to_string())])
            .bearer_auth(&self.token)
            .send()
            .await
            .context("GET /api/events")?
            .error_for_status()
            .context("GET /api/events status")?
            .json::<EventsResponse>()
            .await
            .context("decode /api/events")?;
        Ok(resp)
    }

    /// Cheap liveness probe used at startup to give a clear error before the UI
    /// takes over the screen.
    pub async fn healthz(&self) -> Result<()> {
        let resp = self
            .http
            .get(self.url("/healthz"))
            .send()
            .await
            .context("GET /healthz")?;
        if !resp.status().is_success() {
            anyhow::bail!("gateway /healthz → {}", resp.status());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_join_is_exact() {
        let c = GatewayClient::new("http://h:8765".into(), "t".into()).unwrap();
        assert_eq!(c.url("/api/queue"), "http://h:8765/api/queue");
        assert_eq!(
            c.url("/alerts/jetson-1/temp_celsius/snooze"),
            "http://h:8765/alerts/jetson-1/temp_celsius/snooze"
        );
    }
}

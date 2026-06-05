//! Command-line / environment configuration.
//!
//! Mirrors the gateway's own env contract: `TURING_GATEWAY_*`. The token is the
//! bearer the gateway checks (`config.gateway_token`); the URL is the gateway's
//! Tailnet origin (default matches `config.gateway_port` = 8765).

use clap::Parser;

#[derive(Parser, Debug, Clone)]
#[command(
    name = "turing-tui",
    about = "Snappy terminal operator surface for the Turing cluster.",
    long_about = "A ratatui front-end over the Turing gateway HTTP/WS API. Mirrors the \
                  webui panes — question queue, chat, fleet specs, message trace, and \
                  hardware alerts — in the terminal. Event-driven render: idle CPU ~0."
)]
pub struct Cli {
    /// Gateway origin, e.g. http://surface.tailnet.ts.net:8765
    #[arg(
        long,
        env = "TURING_GATEWAY_URL",
        default_value = "http://localhost:8765"
    )]
    pub url: String,

    /// Bearer token (gateway `TURING_GATEWAY_TOKEN`). Required by the gateway.
    #[arg(long, env = "TURING_GATEWAY_TOKEN", default_value = "")]
    pub token: String,

    /// Specs poll interval in seconds (the gateway does not push /peers).
    #[arg(long, default_value_t = 4)]
    pub poll_secs: u64,

    /// Print the resolved config and exit (used by tests / smoke checks).
    #[arg(long)]
    pub check: bool,
}

impl Cli {
    /// The WebSocket origin derived from the HTTP `url` (http→ws, https→wss).
    pub fn ws_url(&self) -> String {
        let base = self.url.trim_end_matches('/');
        let ws = if let Some(rest) = base.strip_prefix("https://") {
            format!("wss://{rest}")
        } else if let Some(rest) = base.strip_prefix("http://") {
            format!("ws://{rest}")
        } else {
            // Already a ws/wss origin, or bare host — pass through.
            base.to_string()
        };
        format!("{ws}/ws")
    }

    /// The HTTP origin with no trailing slash.
    pub fn http_base(&self) -> String {
        self.url.trim_end_matches('/').to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cli(url: &str) -> Cli {
        Cli {
            url: url.into(),
            token: "t".into(),
            poll_secs: 4,
            check: false,
        }
    }

    #[test]
    fn ws_url_maps_http_to_ws() {
        assert_eq!(cli("http://host:8765").ws_url(), "ws://host:8765/ws");
        assert_eq!(cli("http://host:8765/").ws_url(), "ws://host:8765/ws");
    }

    #[test]
    fn ws_url_maps_https_to_wss() {
        assert_eq!(
            cli("https://surface.ts.net:8765").ws_url(),
            "wss://surface.ts.net:8765/ws"
        );
    }

    #[test]
    fn http_base_strips_trailing_slash() {
        assert_eq!(cli("http://host:8765/").http_base(), "http://host:8765");
    }
}

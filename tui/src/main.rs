//! `turing-tui` — a snappy terminal operator surface for the Turing cluster.
//!
//! Architecture (all I/O off the render path → idle CPU ~0):
//!   • a WebSocket task ([`ws`]) streams decoded gateway frames,
//!   • a poll task pulls `/peers` (specs) on an interval + the initial trace
//!     backlog,
//!   • operator actions are fired as detached tasks so a slow POST never
//!     stalls the UI,
//!   • the main `select!` redraws only when an input or engine event actually
//!     changed state (the reducers return a `dirty` bool).

mod api;
mod app;
mod cli;
mod event;
mod model;
mod ui;
mod ws;

use anyhow::Result;
use clap::Parser;
use crossterm::event::{Event, EventStream, KeyEventKind};
use futures_util::StreamExt;
use tokio::sync::mpsc;

use crate::api::GatewayClient;
use crate::app::{Action, App};
use crate::cli::Cli;
use crate::event::EngineEvent;

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    if cli.check {
        println!("url      = {}", cli.http_base());
        println!("ws_url   = {}", cli.ws_url());
        println!(
            "token    = {}",
            if cli.token.is_empty() {
                "<empty>"
            } else {
                "<set>"
            }
        );
        println!("poll_secs= {}", cli.poll_secs);
        return Ok(());
    }

    if cli.token.is_empty() {
        eprintln!(
            "error: no gateway token. Set TURING_GATEWAY_TOKEN (or --token).\n\
             It is the coordinator's TURING_GATEWAY_TOKEN (see .env.coordinator)."
        );
        std::process::exit(2);
    }

    let client = GatewayClient::new(cli.http_base(), cli.token.clone())?;

    // Reachability preflight before we take over the screen, so a wrong URL or
    // a down gateway prints a clean error instead of an empty TUI.
    if let Err(e) = client.healthz().await {
        eprintln!("error: cannot reach gateway at {}: {e}", cli.http_base());
        std::process::exit(1);
    }

    let (tx, mut rx) = mpsc::channel::<EngineEvent>(256);

    // WebSocket subscription (live frames).
    tokio::spawn(ws::run(cli.ws_url(), cli.token.clone(), tx.clone()));

    // Specs poll + initial trace backlog.
    {
        let client = client.clone();
        let tx = tx.clone();
        let poll = cli.poll_secs.max(1);
        tokio::spawn(async move {
            if let Ok(ev) = client.recent_events(200).await {
                let _ = tx.send(EngineEvent::TraceBacklog(ev.events)).await;
            }
            let mut interval = tokio::time::interval(std::time::Duration::from_secs(poll));
            loop {
                interval.tick().await;
                match client.peers().await {
                    Ok(peers) => {
                        if tx.send(EngineEvent::Peers(peers)).await.is_err() {
                            break;
                        }
                    }
                    Err(_) => { /* transient; next tick retries */ }
                }
            }
        });
    }

    let mut terminal = ratatui::init();
    let mut app = App::new();
    let mut input = EventStream::new();
    let mut dirty = true;

    let result: Result<()> = loop {
        if dirty {
            if let Err(e) = terminal.draw(|f| ui::draw(f, &app)) {
                break Err(e.into());
            }
            dirty = false;
        }

        tokio::select! {
            maybe = input.next() => {
                if let Some(Ok(ev)) = maybe {
                    dirty |= handle_input(&mut app, ev, &client, &tx);
                }
            },
            engine = rx.recv() => if let Some(ev) = engine {
                dirty |= app.on_engine(ev);
                // Coalesce a burst of engine events (trace storms, snapshot +
                // deltas on reconnect) into a single redraw instead of one
                // draw per frame.
                while let Ok(ev) = rx.try_recv() {
                    dirty |= app.on_engine(ev);
                }
            },
        }

        if app.should_quit {
            break Ok(());
        }
    };

    ratatui::restore();
    result
}

/// Translate a terminal input event into state changes + detached actions.
/// Returns whether a redraw is needed.
fn handle_input(
    app: &mut App,
    ev: Event,
    client: &GatewayClient,
    tx: &mpsc::Sender<EngineEvent>,
) -> bool {
    match ev {
        Event::Key(key) if key.kind != KeyEventKind::Release => {
            let actions = app.on_key(key);
            for action in actions {
                spawn_action(client.clone(), tx.clone(), action);
            }
            true
        }
        Event::Resize(_, _) => true,
        _ => false,
    }
}

/// Fire an operator action without blocking the render loop. The resulting WS
/// delta updates the panes; a failure surfaces as a footer notice.
fn spawn_action(client: GatewayClient, tx: mpsc::Sender<EngineEvent>, action: Action) {
    tokio::spawn(async move {
        let (label, result) = match &action {
            Action::QueueApprove(id) => (format!("approve {id}"), client.queue_approve(id).await),
            Action::QueueAccept(id) => (format!("accept {id}"), client.queue_accept(id).await),
            Action::QueueReject(id) => (format!("reject {id}"), client.queue_reject(id).await),
            Action::QueueEdit { id, text } => {
                (format!("edit {id}"), client.queue_edit(id, text).await)
            }
            Action::ChatSubmit { prompt } => {
                ("submit".to_string(), client.chat_submit(prompt, None).await)
            }
            Action::ChatAccept { sid, stid } => (
                format!("accept {stid}"),
                client.chat_accept(sid, stid).await,
            ),
            Action::ChatReject { sid, stid } => (
                format!("reject {stid}"),
                client.chat_reject(sid, stid).await,
            ),
            Action::ChatEdit { sid, stid, text } => (
                format!("edit {stid}"),
                client.chat_edit(sid, stid, text).await,
            ),
            Action::Snooze { node_id, field } => (
                format!("snooze {node_id}/{field}"),
                client.snooze(node_id, field).await,
            ),
        };
        let notice = match result {
            Ok(()) => format!("✓ {label}"),
            Err(e) => format!("✗ {label}: {e}"),
        };
        let _ = tx.send(EngineEvent::Notice(notice)).await;
    });
}

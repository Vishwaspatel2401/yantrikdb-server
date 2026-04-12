//! YantrikDB witness — vote-only cluster member.
//!
//! A tiny daemon that participates in Raft elections without storing any data.
//! Used to break ties in 2-node clusters (so you don't need 3 full data nodes).
//!
//! Stores only:
//! - current_term
//! - voted_for (per term)
//!
//! Persists this to a small JSON file. Does NOT store memories, embeddings,
//! oplog, or anything else.

use std::path::PathBuf;
use std::sync::Mutex;

use clap::Parser;
use futures::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use tokio::net::{TcpListener, TcpStream};
use tokio_util::codec::Framed;

use yantrikdb_protocol::messages::*;
use yantrikdb_protocol::*;

#[derive(Parser)]
#[command(
    name = "yantrikdb-witness",
    about = "YantrikDB cluster witness — vote-only daemon",
    version
)]
struct Cli {
    /// Witness node ID (must be unique in the cluster)
    #[arg(long)]
    node_id: u32,

    /// Listen port (default 7440)
    #[arg(short, long, default_value = "7440")]
    port: u16,

    /// Cluster shared secret (must match other nodes)
    #[arg(long, env = "YANTRIKDB_CLUSTER_SECRET")]
    cluster_secret: String,

    /// State file path (where current_term and voted_for are persisted)
    #[arg(long, default_value = "./witness.json")]
    state_file: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct WitnessState {
    current_term: u64,
    voted_for: Option<u32>,
}

impl WitnessState {
    fn load(path: &std::path::Path) -> anyhow::Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let content = std::fs::read_to_string(path)?;
        Ok(serde_json::from_str(&content)?)
    }

    fn save(&self, path: &std::path::Path) -> anyhow::Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let content = serde_json::to_string_pretty(self)?;
        std::fs::write(path, content)?;
        Ok(())
    }
}

struct Witness {
    node_id: u32,
    cluster_secret: String,
    state: Mutex<WitnessState>,
    state_path: PathBuf,
}

impl Witness {
    fn current_term(&self) -> u64 {
        self.state.lock().unwrap().current_term
    }

    fn grant_vote(&self, term: u64, candidate_id: u32) -> anyhow::Result<bool> {
        let mut s = self.state.lock().unwrap();

        if term < s.current_term {
            return Ok(false);
        }

        if term > s.current_term {
            s.current_term = term;
            s.voted_for = None;
        }

        if let Some(voted) = s.voted_for {
            if voted != candidate_id {
                return Ok(false);
            }
        }

        s.voted_for = Some(candidate_id);
        s.save(&self.state_path)?;

        tracing::info!(term, candidate_id, "vote granted");
        Ok(true)
    }

    fn observe_term(&self, term: u64) -> anyhow::Result<()> {
        let mut s = self.state.lock().unwrap();
        if term > s.current_term {
            s.current_term = term;
            s.voted_for = None;
            s.save(&self.state_path)?;
        }
        Ok(())
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "yantrikdb_witness=info".into()),
        )
        .init();

    let cli = Cli::parse();

    let state = WitnessState::load(&cli.state_file)?;
    tracing::info!(
        node_id = cli.node_id,
        current_term = state.current_term,
        "witness state loaded"
    );

    let witness = std::sync::Arc::new(Witness {
        node_id: cli.node_id,
        cluster_secret: cli.cluster_secret,
        state: Mutex::new(state),
        state_path: cli.state_file,
    });

    let addr = format!("0.0.0.0:{}", cli.port);
    let listener = TcpListener::bind(&addr).await?;
    tracing::info!(addr = %addr, node_id = cli.node_id, "witness listening");

    loop {
        let (stream, peer_addr) = listener.accept().await?;
        let witness = std::sync::Arc::clone(&witness);
        tokio::spawn(async move {
            tracing::debug!(%peer_addr, "peer connection");
            if let Err(e) = handle_peer(stream, witness).await {
                tracing::warn!(%peer_addr, error = %e, "peer connection error");
            }
        });
    }
}

async fn handle_peer(stream: TcpStream, witness: std::sync::Arc<Witness>) -> anyhow::Result<()> {
    let mut framed = Framed::new(stream, YantrikCodec::new());

    // Phase 1: handshake
    let frame = framed
        .next()
        .await
        .ok_or_else(|| anyhow::anyhow!("connection closed before hello"))??;

    if frame.opcode != OpCode::ClusterHello {
        let err = make_error(
            frame.stream_id,
            error_codes::INVALID_PAYLOAD,
            "expected ClusterHello",
        )?;
        framed.send(err).await?;
        return Ok(());
    }

    let hello: ClusterHello = unpack(&frame.payload)?;

    if hello.cluster_secret != witness.cluster_secret {
        let err = make_error(
            frame.stream_id,
            error_codes::CLUSTER_SECRET_MISMATCH,
            "cluster secret mismatch",
        )?;
        framed.send(err).await?;
        return Ok(());
    }

    // Update term if peer is ahead
    witness.observe_term(hello.current_term)?;

    let resp = ClusterHelloOk {
        node_id: witness.node_id,
        role: "witness".to_string(),
        current_term: witness.current_term(),
        leader_id: None,
        protocol_version: yantrikdb_protocol::messages::PROTOCOL_VERSION,
    };
    let resp_frame = make_frame(OpCode::ClusterHelloOk, frame.stream_id, &resp)?;
    framed.send(resp_frame).await?;

    // Phase 2: handle cluster opcodes (only votes + ping)
    while let Some(result) = framed.next().await {
        let frame = match result {
            Ok(f) => f,
            Err(_) => break,
        };

        let stream_id = frame.stream_id;
        match frame.opcode {
            OpCode::RequestVote => {
                let req: RequestVoteMsg = unpack(&frame.payload)?;
                witness.observe_term(req.term)?;

                // Witnesses don't have a log, so we always grant if term/vote rules allow.
                let granted = witness.grant_vote(req.term, req.candidate_id)?;

                let resp = VoteResponseMsg {
                    term: witness.current_term(),
                    voter_id: witness.node_id,
                    granted,
                    reason: if granted {
                        None
                    } else {
                        Some("already voted this term or stale term".into())
                    },
                };
                let opcode = if granted {
                    OpCode::VoteGranted
                } else {
                    OpCode::VoteDenied
                };
                let resp_frame = make_frame(opcode, stream_id, &resp)?;
                framed.send(resp_frame).await?;
            }
            OpCode::Heartbeat => {
                // Witness doesn't really need heartbeats, but accept them to track term
                let hb: HeartbeatMsg = unpack(&frame.payload)?;
                witness.observe_term(hb.term)?;

                let ack = HeartbeatAckMsg {
                    term: witness.current_term(),
                    follower_id: witness.node_id,
                    follower_role: "witness".to_string(),
                    follower_last_hlc: vec![],
                    follower_last_op_id: String::new(),
                    lag_seconds: 0.0,
                };
                let ack_frame = make_frame(OpCode::HeartbeatAck, stream_id, &ack)?;
                framed.send(ack_frame).await?;
            }
            OpCode::Ping => {
                framed.send(Frame::empty(OpCode::Pong, stream_id)).await?;
            }
            OpCode::ClusterStatus => {
                let status = ClusterStatusResultMsg {
                    current_term: witness.current_term(),
                    leader_id: None,
                    self_id: witness.node_id,
                    self_role: "witness".to_string(),
                    peers: vec![],
                    quorum_size: 0,
                    healthy: true,
                };
                let resp = make_frame(OpCode::ClusterStatusResult, stream_id, &status)?;
                framed.send(resp).await?;
            }
            other => {
                tracing::trace!(?other, "unsupported opcode for witness");
                let err = make_error(
                    stream_id,
                    error_codes::INVALID_PAYLOAD,
                    format!("witness does not support {:?}", other),
                )?;
                framed.send(err).await?;
            }
        }
    }

    Ok(())
}

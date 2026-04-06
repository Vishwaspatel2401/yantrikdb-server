//! Heartbeat loop — leader sends periodic heartbeats to followers,
//! followers monitor for leader liveness.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio_util::sync::CancellationToken;

use crate::cluster::ClusterContext;

/// Run the heartbeat loop. This task does two things depending on role:
/// - **Leader**: send heartbeats to all peers every `heartbeat_interval_ms`
/// - **Follower/ReadReplica**: monitor leader liveness, trigger election on timeout
pub async fn run_heartbeat_loop(ctx: Arc<ClusterContext>, cancel: CancellationToken) {
    let interval = Duration::from_millis(ctx.config.heartbeat_interval_ms);
    let mut tick = tokio::time::interval(interval);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    tracing::info!(
        node_id = ctx.node_id(),
        interval_ms = ctx.config.heartbeat_interval_ms,
        "heartbeat loop started"
    );

    // Initial random delay before any election to prevent split votes when
    // multiple nodes start simultaneously. Each node waits a random amount
    // between [election_timeout, 2*election_timeout).
    use rand::Rng;
    let initial_election_delay_ms = ctx.config.election_timeout_ms
        + rand::thread_rng().gen_range(0..ctx.config.election_timeout_ms);
    let next_election_check = Instant::now() + Duration::from_millis(initial_election_delay_ms);

    let mut next_election_check = next_election_check;

    loop {
        tokio::select! {
            _ = tick.tick() => {}
            _ = cancel.cancelled() => {
                tracing::info!(node_id = ctx.node_id(), "heartbeat loop stopped");
                return;
            }
        }

        if ctx.state.is_leader() {
            send_heartbeats_to_peers(&ctx).await;
        } else {
            // Non-leaders: ping peers to keep registry fresh (peer discovery)
            ping_peers(&ctx).await;

            if matches!(
                ctx.state.leader_role(),
                crate::cluster::LeaderRole::Follower | crate::cluster::LeaderRole::Candidate
            ) && Instant::now() >= next_election_check
            {
                check_leader_liveness(&ctx).await;
                // Reset the deadline with new random jitter for next round
                let jitter = rand::thread_rng().gen_range(0..ctx.config.election_timeout_ms);
                next_election_check =
                    Instant::now() + Duration::from_millis(ctx.config.election_timeout_ms + jitter);
            }
        }
    }
}

/// Non-leader peer discovery — handshake with all peers to keep them marked reachable.
async fn ping_peers(ctx: &Arc<ClusterContext>) {
    let peers = ctx.peers.snapshot();
    for peer in peers {
        let ctx = Arc::clone(ctx);
        let addr = peer.addr.clone();
        tokio::spawn(async move {
            match crate::cluster::client::connect_and_handshake(&addr, &ctx).await {
                Ok(_) => {} // handshake itself updates peer registry
                Err(_) => {
                    ctx.peers.mark_unreachable(&addr);
                }
            }
        });
    }
}

async fn send_heartbeats_to_peers(ctx: &Arc<ClusterContext>) {
    use yantrikdb_protocol::messages::HeartbeatMsg;

    let term = ctx.state.current_term();
    let last_hlc = ctx.last_hlc().unwrap_or_default();
    let last_op_id = ctx.last_op_id().unwrap_or_default();

    let hb = HeartbeatMsg {
        term,
        leader_id: ctx.node_id(),
        leader_last_hlc: last_hlc,
        leader_last_op_id: last_op_id,
    };

    let peers = ctx.peers.snapshot();
    for peer in peers {
        let ctx = Arc::clone(ctx);
        let hb = hb.clone();
        let addr = peer.addr.clone();
        tokio::spawn(async move {
            match send_heartbeat_to(&addr, hb, &ctx).await {
                Ok(_) => {}
                Err(e) => {
                    tracing::trace!(peer = %addr, error = %e, "heartbeat failed");
                    ctx.peers.mark_unreachable(&addr);
                }
            }
        });
    }
}

async fn send_heartbeat_to(
    addr: &str,
    hb: yantrikdb_protocol::messages::HeartbeatMsg,
    ctx: &ClusterContext,
) -> anyhow::Result<()> {
    use crate::cluster::client::CONNECT_TIMEOUT;
    use yantrikdb_protocol::*;

    let mut conn = crate::cluster::client::connect_and_handshake(addr, ctx).await?;

    let frame = make_frame(OpCode::Heartbeat, 0, &hb)?;
    use futures::SinkExt;
    conn.send(frame).await?;

    use futures::StreamExt;
    let resp = tokio::time::timeout(CONNECT_TIMEOUT, conn.next())
        .await?
        .ok_or_else(|| anyhow::anyhow!("no heartbeat ack"))??;

    if resp.opcode == OpCode::HeartbeatAck {
        let ack: yantrikdb_protocol::messages::HeartbeatAckMsg = unpack(&resp.payload)?;
        // If follower has higher term, step down
        if ack.term > ctx.state.current_term() {
            ctx.state.become_follower(ack.term, None)?;
        }
        ctx.peers
            .update_oplog_position(addr, ack.follower_last_hlc, ack.follower_last_op_id);
    }

    Ok(())
}

async fn check_leader_liveness(ctx: &Arc<ClusterContext>) {
    let timeout = Duration::from_millis(ctx.config.election_timeout_ms);

    let should_elect = match ctx.state.time_since_heartbeat() {
        Some(elapsed) if elapsed > timeout => true,
        None => true, // never heard from a leader
        _ => false,
    };

    if !should_elect {
        return;
    }

    // Only voters can run for leader. Read replicas + witnesses skip.
    if !matches!(ctx.state.configured_role, crate::config::NodeRole::Voter) {
        return;
    }

    tracing::warn!(
        node_id = ctx.node_id(),
        "leader timeout — starting election"
    );
    let ctx = Arc::clone(ctx);
    tokio::spawn(async move {
        if let Err(e) = crate::cluster::election::start_election(ctx).await {
            tracing::error!(error = %e, "election failed");
        }
    });
}

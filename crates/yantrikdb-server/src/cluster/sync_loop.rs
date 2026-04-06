//! Oplog sync loop — followers continuously pull ops from the leader.

use std::sync::Arc;
use std::time::Duration;

use futures::{SinkExt, StreamExt};
use tokio_util::sync::CancellationToken;

use yantrikdb_protocol::messages::{OplogPullRequest, OplogPullResult};
use yantrikdb_protocol::*;

use crate::cluster::client::{connect_and_handshake, CONNECT_TIMEOUT};
use crate::cluster::replication::{handle_oplog_apply, update_local_watermark};
use crate::cluster::ClusterContext;

const PULL_INTERVAL: Duration = Duration::from_millis(500);
const PULL_BATCH_SIZE: usize = 500;

/// Run the oplog sync loop. Followers and read replicas pull from the leader.
pub async fn run_sync_loop(ctx: Arc<ClusterContext>, cancel: CancellationToken) {
    let mut tick = tokio::time::interval(PULL_INTERVAL);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    tracing::info!(node_id = ctx.node_id(), "oplog sync loop started");

    loop {
        tokio::select! {
            _ = tick.tick() => {}
            _ = cancel.cancelled() => {
                tracing::info!(node_id = ctx.node_id(), "sync loop stopped");
                return;
            }
        }

        // Only followers and read replicas pull
        if !matches!(
            ctx.state.leader_role(),
            crate::cluster::LeaderRole::Follower | crate::cluster::LeaderRole::ReadOnly
        ) {
            continue;
        }

        // Find current leader's address
        let leader_id = match ctx.state.current_leader() {
            Some(id) => id,
            None => continue, // no leader yet
        };

        let leader_addr = match ctx
            .peers
            .snapshot()
            .into_iter()
            .find(|p| p.node_id == Some(leader_id))
        {
            Some(p) => p.addr,
            None => continue, // leader not in peer list yet
        };

        if let Err(e) = pull_from_leader(&ctx, &leader_addr).await {
            tracing::trace!(leader = %leader_addr, error = %e, "pull failed");
        }
    }
}

async fn pull_from_leader(ctx: &Arc<ClusterContext>, leader_addr: &str) -> anyhow::Result<()> {
    // Find our actor_id (used for exclusion to avoid pulling our own ops)
    let our_actor_id = {
        let engine = ctx.default_engine()?;
        let db = engine.lock().unwrap();
        db.actor_id().to_string()
    };

    // Get current watermark for this leader
    let watermark =
        crate::cluster::replication::get_local_watermark(&ctx.default_engine()?, leader_addr)?;

    let (since_hlc, since_op_id) = match watermark {
        Some((hlc, op_id)) => (Some(hlc), Some(op_id)),
        None => (None, None),
    };

    let req = OplogPullRequest {
        since_hlc,
        since_op_id,
        limit: PULL_BATCH_SIZE,
        exclude_actor: Some(our_actor_id),
    };

    let mut conn = connect_and_handshake(leader_addr, ctx).await?;
    let frame = make_frame(OpCode::OplogPull, 0, &req)?;
    conn.send(frame).await?;

    let resp = tokio::time::timeout(CONNECT_TIMEOUT, conn.next())
        .await?
        .ok_or_else(|| anyhow::anyhow!("no pull response"))??;

    if resp.opcode != OpCode::OplogPullResult {
        anyhow::bail!("unexpected opcode: {:?}", resp.opcode);
    }

    let result: OplogPullResult = unpack(&resp.payload)?;
    if result.ops.is_empty() {
        return Ok(());
    }

    let count = result.ops.len();
    let last_hlc = result.ops.last().map(|o| o.hlc.clone()).unwrap_or_default();
    let last_op_id = result.ops.last().map(|o| o.op_id.clone()).unwrap_or_default();

    let apply = handle_oplog_apply(&ctx.default_engine()?, result.ops)?;

    // Update watermark only if we actually advanced
    if !last_op_id.is_empty() {
        update_local_watermark(&ctx.default_engine()?, leader_addr, &last_hlc, &last_op_id)?;
    }

    if apply.applied > 0 {
        tracing::info!(
            leader = %leader_addr,
            pulled = count,
            applied = apply.applied,
            skipped = apply.skipped,
            "oplog pull"
        );
    }

    Ok(())
}

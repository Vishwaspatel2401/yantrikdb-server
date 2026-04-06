//! Leader election — Raft-style.
//!
//! When a follower hasn't heard from a leader within the election timeout,
//! it transitions to candidate, increments term, votes for self, and asks
//! all peers for their vote. Wins if it collects votes from a majority.

use std::sync::Arc;

use crate::cluster::client::{connect_and_handshake, CONNECT_TIMEOUT};
use crate::cluster::ClusterContext;

use futures::{SinkExt, StreamExt};
use yantrikdb_protocol::messages::{RequestVoteMsg, VoteResponseMsg};
use yantrikdb_protocol::*;

/// Run a single election round. Becomes leader if it wins, otherwise
/// stays as candidate (the next election timeout will retry).
pub async fn start_election(ctx: Arc<ClusterContext>) -> anyhow::Result<()> {
    if !ctx.state.is_voter() {
        return Ok(());
    }

    // Step 1: become candidate (increments term, votes for self)
    let term = ctx.state.become_candidate()?;

    // Step 2: collect our own log position
    let last_log_hlc = ctx.last_hlc()?;
    let last_log_op_id = ctx.last_op_id()?;

    // Step 3: send RequestVote to all voter+witness peers in parallel
    let req = RequestVoteMsg {
        term,
        candidate_id: ctx.node_id(),
        last_log_hlc,
        last_log_op_id,
    };

    let peers = ctx.peers.snapshot();
    let voter_peers: Vec<_> = peers
        .into_iter()
        .filter(|p| {
            matches!(
                p.configured_role,
                crate::config::NodeRole::Voter | crate::config::NodeRole::Witness
            )
        })
        .collect();

    let quorum = ctx.quorum_size();
    let mut votes_for_us = 1usize; // our own vote
    let mut votes_against = 0usize;

    // Spawn parallel vote requests
    let mut tasks = Vec::new();
    for peer in voter_peers {
        let ctx = Arc::clone(&ctx);
        let req = req.clone();
        tasks.push(tokio::spawn(async move {
            request_vote_from(&peer.addr, req, &ctx).await
        }));
    }

    // Collect responses with timeout
    let collection_timeout = std::time::Duration::from_millis(ctx.config.election_timeout_ms);
    let deadline = tokio::time::Instant::now() + collection_timeout;

    for task in tasks {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        match tokio::time::timeout(remaining, task).await {
            Ok(Ok(Ok(resp))) => {
                // If a voter has higher term, step down
                if resp.term > term {
                    ctx.state.become_follower(resp.term, None)?;
                    return Ok(());
                }
                if resp.granted {
                    votes_for_us += 1;
                } else {
                    votes_against += 1;
                }

                if votes_for_us >= quorum {
                    // Win immediately on quorum
                    break;
                }
            }
            Ok(Ok(Err(e))) => {
                tracing::trace!(error = %e, "vote request failed");
                votes_against += 1;
            }
            Ok(Err(e)) => {
                tracing::trace!(error = %e, "vote request task panicked");
                votes_against += 1;
            }
            Err(_) => {
                tracing::trace!("vote request timed out");
                votes_against += 1;
            }
        }
    }

    tracing::info!(
        term,
        votes_for = votes_for_us,
        votes_against,
        quorum,
        "election complete"
    );

    if votes_for_us >= quorum {
        ctx.state.become_leader()?;
        // Immediately send a heartbeat to assert leadership
        // (the heartbeat loop will pick it up on its next tick)
        tracing::info!(term, node_id = ctx.node_id(), "🎉 elected as leader");
    } else {
        tracing::info!(term, "lost election — staying as follower");
        ctx.state.become_follower(term, None)?;
    }

    Ok(())
}

async fn request_vote_from(
    addr: &str,
    req: RequestVoteMsg,
    ctx: &ClusterContext,
) -> anyhow::Result<VoteResponseMsg> {
    let mut conn = connect_and_handshake(addr, ctx).await?;

    let frame = make_frame(OpCode::RequestVote, 0, &req)?;
    conn.send(frame).await?;

    let resp = tokio::time::timeout(CONNECT_TIMEOUT, conn.next())
        .await?
        .ok_or_else(|| anyhow::anyhow!("no vote response"))??;

    if resp.opcode != OpCode::VoteGranted && resp.opcode != OpCode::VoteDenied {
        anyhow::bail!("unexpected vote response opcode: {:?}", resp.opcode);
    }

    let vote: VoteResponseMsg = unpack(&resp.payload)?;
    Ok(vote)
}

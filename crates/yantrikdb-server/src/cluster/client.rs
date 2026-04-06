//! Outbound peer connection client.
//!
//! Helper for cluster background tasks to talk to other peers.

use std::time::Duration;

use futures::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio_util::codec::Framed;

use yantrikdb_protocol::messages::{ClusterHello, ClusterHelloOk};
use yantrikdb_protocol::*;

use crate::cluster::ClusterContext;

pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Connect to a peer and complete the cluster handshake.
/// Returns a Framed connection ready for cluster opcodes.
pub async fn connect_and_handshake(
    addr: &str,
    ctx: &ClusterContext,
) -> anyhow::Result<Framed<TcpStream, YantrikCodec>> {
    let stream = tokio::time::timeout(CONNECT_TIMEOUT, TcpStream::connect(addr))
        .await
        .map_err(|_| anyhow::anyhow!("connect timeout to {}", addr))??;
    let mut framed = Framed::new(stream, YantrikCodec::new());

    // Send hello
    let hello = ClusterHello {
        node_id: ctx.node_id(),
        role: crate::cluster::server::role_string(ctx.state.configured_role),
        current_term: ctx.state.current_term(),
        cluster_secret: ctx.config.cluster_secret.clone().unwrap_or_default(),
        advertise_addr: ctx
            .config
            .advertise_addr
            .clone()
            .unwrap_or_else(|| format!("0.0.0.0:{}", ctx.config.cluster_port)),
    };
    let frame = make_frame(OpCode::ClusterHello, 0, &hello)?;
    framed.send(frame).await?;

    // Wait for hello-ok
    let resp = tokio::time::timeout(CONNECT_TIMEOUT, framed.next())
        .await?
        .ok_or_else(|| anyhow::anyhow!("no hello response"))??;

    if resp.opcode != OpCode::ClusterHelloOk {
        anyhow::bail!("expected ClusterHelloOk, got {:?}", resp.opcode);
    }

    let _ok: ClusterHelloOk = unpack(&resp.payload)?;
    Ok(framed)
}

//! Cluster / replication state machine.
//!
//! Implements Raft-lite leader election: only the election parts of Raft.
//! Log replication is handled by the existing CRDT oplog in yantrikdb-core,
//! which converges naturally without needing a strict log order.
//!
//! Roles:
//! - **Voter** — Full data node that can be elected leader
//! - **ReadReplica** — Consumes oplog, never votes, never accepts writes
//! - **Witness** — Vote-only, no data storage (separate binary)
//! - **Single** — Standalone, no replication

pub mod peers;
pub mod replication;
pub mod state;

pub use peers::{PeerRegistry, PeerStatus};
pub use state::{LeaderRole, NodeState, RaftState};

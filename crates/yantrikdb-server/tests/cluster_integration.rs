//! Integration test: spin up 2 voter nodes + 1 witness, verify election & failover.
//!
//! This test runs the actual `yantrikdb` and `yantrikdb-witness` binaries
//! as subprocesses on the same machine using different ports.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use serde_json::Value;
use tempfile::TempDir;

const SECRET: &str = "test-cluster-secret-multi-node-1234567890";

/// Find the built `yantrikdb` binary.
fn yantrikdb_bin() -> PathBuf {
    target_dir().join(format!("yantrikdb{}", std::env::consts::EXE_SUFFIX))
}

/// Find the built `yantrikdb-witness` binary.
fn witness_bin() -> PathBuf {
    target_dir().join(format!("yantrikdb-witness{}", std::env::consts::EXE_SUFFIX))
}

fn target_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    while p
        .file_name()
        .map(|s| s != "yantrikdb-server")
        .unwrap_or(true)
    {
        if !p.pop() {
            panic!("could not find target dir");
        }
    }
    p.pop(); // crates/
    p.pop(); // workspace root
    p.join("target").join("debug")
}

struct Node {
    child: Child,
    http_port: u16,
}

impl Drop for Node {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn write_config(
    path: &std::path::Path,
    data_dir: &std::path::Path,
    node_id: u32,
    wire_port: u16,
    http_port: u16,
    cluster_port: u16,
    peers: &[(&str, &str)],
) {
    let mut peer_blocks = String::new();
    for (addr, role) in peers {
        peer_blocks.push_str(&format!(
            "\n[[cluster.peers]]\naddr = \"{}\"\nrole = \"{}\"\n",
            addr, role
        ));
    }

    let toml = format!(
        r#"[server]
wire_port = {wire_port}
http_port = {http_port}
data_dir = "{data_dir}"

[embedding]
strategy = "client_only"
dim = 384

[cluster]
node_id = {node_id}
role = "voter"
cluster_port = {cluster_port}
heartbeat_interval_ms = 500
election_timeout_ms = 1500
cluster_secret = "{secret}"
{peer_blocks}"#,
        data_dir = data_dir.display().to_string().replace('\\', "/"),
        secret = SECRET,
    );

    std::fs::write(path, toml).unwrap();
}

fn start_node(
    name: &str,
    data_dir: &std::path::Path,
    config: &std::path::Path,
    http_port: u16,
) -> Node {
    let log = std::fs::File::create(data_dir.join(format!("{}.log", name))).unwrap();
    let child = Command::new(yantrikdb_bin())
        .arg("serve")
        .arg("--config")
        .arg(config)
        .stdout(Stdio::from(log.try_clone().unwrap()))
        .stderr(Stdio::from(log))
        .spawn()
        .expect("failed to spawn yantrikdb");

    Node { child, http_port }
}

fn start_witness(name: &str, data_dir: &std::path::Path, port: u16, node_id: u32) -> Child {
    let log = std::fs::File::create(data_dir.join(format!("{}.log", name))).unwrap();
    Command::new(witness_bin())
        .args(&[
            "--node-id",
            &node_id.to_string(),
            "--port",
            &port.to_string(),
            "--cluster-secret",
            SECRET,
            "--state-file",
        ])
        .arg(data_dir.join(format!("{}-state.json", name)))
        .stdout(Stdio::from(log.try_clone().unwrap()))
        .stderr(Stdio::from(log))
        .spawn()
        .expect("failed to spawn witness")
}

fn pre_create_db(data_dir: &std::path::Path) {
    let status = Command::new(yantrikdb_bin())
        .args(&["db", "--data-dir"])
        .arg(data_dir)
        .args(&["create", "default"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .expect("failed to spawn db create");
    assert!(status.success());
}

fn fetch_cluster(http_port: u16) -> Option<Value> {
    let url = format!("http://127.0.0.1:{}/v1/cluster", http_port);
    reqwest::blocking::Client::new()
        .get(&url)
        .timeout(Duration::from_secs(2))
        .send()
        .ok()
        .and_then(|r| r.json().ok())
}

fn wait_for_election(node: &Node, max_attempts: u32) -> Option<Value> {
    for _ in 0..max_attempts {
        std::thread::sleep(Duration::from_millis(500));
        if let Some(v) = fetch_cluster(node.http_port) {
            if v.get("leader_id").and_then(|x| x.as_u64()).is_some() {
                return Some(v);
            }
        }
    }
    None
}

#[test]
#[ignore = "spawns subprocesses; run with cargo test --test cluster_integration -- --ignored"]
fn three_node_cluster_elects_leader() {
    let tmp = TempDir::new().unwrap();
    let base = tmp.path();

    let n1_dir = base.join("node1");
    let n2_dir = base.join("node2");
    let w_dir = base.join("witness");
    std::fs::create_dir_all(&n1_dir).unwrap();
    std::fs::create_dir_all(&n2_dir).unwrap();
    std::fs::create_dir_all(&w_dir).unwrap();

    pre_create_db(&n1_dir);
    pre_create_db(&n2_dir);

    let n1_cfg = n1_dir.join("config.toml");
    let n2_cfg = n2_dir.join("config.toml");

    write_config(
        &n1_cfg,
        &n1_dir,
        1,
        18437,
        18438,
        18440,
        &[("127.0.0.1:28440", "voter"), ("127.0.0.1:38440", "witness")],
    );
    write_config(
        &n2_cfg,
        &n2_dir,
        2,
        28437,
        28438,
        28440,
        &[("127.0.0.1:18440", "voter"), ("127.0.0.1:38440", "witness")],
    );

    let _witness = start_witness("witness", &w_dir, 38440, 99);
    std::thread::sleep(Duration::from_millis(500));

    let n1 = start_node("node1", &n1_dir, &n1_cfg, 18438);
    let n2 = start_node("node2", &n2_dir, &n2_cfg, 28438);

    // Wait for election
    let n1_status = wait_for_election(&n1, 30).expect("n1 never saw a leader");
    let n2_status = wait_for_election(&n2, 30).expect("n2 never saw a leader");

    let leader1 = n1_status["leader_id"].as_u64().unwrap();
    let leader2 = n2_status["leader_id"].as_u64().unwrap();

    // Both should agree on leader
    assert_eq!(leader1, leader2, "nodes disagree on leader");
    assert!(leader1 == 1 || leader1 == 2, "leader is not 1 or 2");

    // At least one node should be healthy (the test guarantees a healthy node via wait_for_election)
    let n1_healthy = n1_status["healthy"].as_bool().unwrap_or(false);
    let n2_healthy = n2_status["healthy"].as_bool().unwrap_or(false);
    assert!(n1_healthy || n2_healthy, "neither node is healthy");

    // Exactly one node accepts writes (the leader)
    let n1_writes = n1_status["accepts_writes"].as_bool().unwrap_or(false);
    let n2_writes = n2_status["accepts_writes"].as_bool().unwrap_or(false);
    assert!(
        n1_writes ^ n2_writes,
        "exactly one node should accept writes"
    );

    // The writeable node should be the leader
    let writeable_id = if n1_writes { 1 } else { 2 };
    assert_eq!(writeable_id as u64, leader1, "writeable node != leader");

    println!("✓ leader elected: node {}", leader1);
    println!("✓ both nodes agree");
    println!("✓ read-only enforced on follower");

    // Cleanup happens via Drop
}

#[test]
#[ignore = "spawns subprocesses"]
fn write_to_follower_is_rejected() {
    let tmp = TempDir::new().unwrap();
    let base = tmp.path();
    let n1_dir = base.join("node1");
    let n2_dir = base.join("node2");
    let w_dir = base.join("witness");
    std::fs::create_dir_all(&n1_dir).unwrap();
    std::fs::create_dir_all(&n2_dir).unwrap();
    std::fs::create_dir_all(&w_dir).unwrap();
    pre_create_db(&n1_dir);
    pre_create_db(&n2_dir);

    let n1_cfg = n1_dir.join("config.toml");
    let n2_cfg = n2_dir.join("config.toml");
    write_config(
        &n1_cfg,
        &n1_dir,
        1,
        19437,
        19438,
        19440,
        &[("127.0.0.1:29440", "voter"), ("127.0.0.1:39440", "witness")],
    );
    write_config(
        &n2_cfg,
        &n2_dir,
        2,
        29437,
        29438,
        29440,
        &[("127.0.0.1:19440", "voter"), ("127.0.0.1:39440", "witness")],
    );

    let _witness = start_witness("witness", &w_dir, 39440, 99);
    std::thread::sleep(Duration::from_millis(500));

    let n1 = start_node("node1", &n1_dir, &n1_cfg, 19438);
    let n2 = start_node("node2", &n2_dir, &n2_cfg, 29438);

    let _ = wait_for_election(&n1, 30).expect("no leader");
    let _ = wait_for_election(&n2, 30).expect("no leader");

    let s1 = fetch_cluster(n1.http_port).unwrap();
    let leader = s1["leader_id"].as_u64().unwrap();
    let follower_port = if leader == 1 { 29438 } else { 19438 };

    // Try to write to the follower using the cluster master token
    let url = format!("http://127.0.0.1:{}/v1/remember", follower_port);
    let resp = reqwest::blocking::Client::new()
        .post(&url)
        .header("Authorization", format!("Bearer {}", SECRET))
        .json(&serde_json::json!({"text": "should fail", "importance": 0.5}))
        .send()
        .unwrap();

    assert_eq!(resp.status(), 503, "write to follower should be 503");
    let body: Value = resp.json().unwrap();
    assert!(
        body["error"].as_str().unwrap().contains("read-only"),
        "expected read-only error, got: {:?}",
        body
    );

    println!("✓ writes correctly rejected on follower");
}

//! Shared types for all ML backends.
//!
//! These types are backend-agnostic — they appear in the `LLMBackend` and
//! `STTBackend` trait signatures and are used throughout the companion.

use serde::{Deserialize, Serialize};

// ── Chat messages ──────────────────────────────────────────────────────

/// A single chat message.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

impl ChatMessage {
    pub fn system(content: impl Into<String>) -> Self {
        Self {
            role: "system".into(),
            content: content.into(),
        }
    }

    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: "user".into(),
            content: content.into(),
        }
    }

    pub fn assistant(content: impl Into<String>) -> Self {
        Self {
            role: "assistant".into(),
            content: content.into(),
        }
    }
}

/// A parsed tool call from model output.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCall {
    pub name: String,
    pub arguments: serde_json::Value,
}

// ── Generation config ──────────────────────────────────────────────────

/// Configuration for text generation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GenerationConfig {
    /// Maximum number of tokens to generate.
    pub max_tokens: usize,
    /// Sampling temperature (0.0 = greedy/argmax).
    pub temperature: f64,
    /// Top-p (nucleus) sampling threshold.
    pub top_p: Option<f64>,
    /// Top-k sampling (number of highest-probability tokens to keep).
    pub top_k: Option<usize>,
    /// Repetition penalty (1.0 = no penalty).
    pub repeat_penalty: f32,
    /// Window size for repetition penalty.
    pub repeat_last_n: usize,
    /// Random seed for sampling.
    pub seed: u64,
    /// Stop sequences — generation halts when any of these is produced.
    pub stop: Vec<String>,
}

impl Default for GenerationConfig {
    fn default() -> Self {
        Self {
            max_tokens: 512,
            temperature: 0.7,
            top_p: Some(0.9),
            top_k: None,
            repeat_penalty: 1.1,
            repeat_last_n: 64,
            seed: 42,
            stop: vec![],
        }
    }
}

impl GenerationConfig {
    /// Greedy decoding (argmax, no randomness).
    pub fn greedy() -> Self {
        Self {
            temperature: 0.0,
            top_p: None,
            top_k: None,
            ..Default::default()
        }
    }
}

// ── LLM response ───────────────────────────────────────────────────────

/// Response from a generation call.
#[derive(Debug, Clone)]
pub struct LLMResponse {
    /// The generated text (full output).
    pub text: String,
    /// Number of prompt tokens processed.
    pub prompt_tokens: usize,
    /// Number of tokens generated.
    pub completion_tokens: usize,
    /// Any tool calls parsed from the output.
    pub tool_calls: Vec<ToolCall>,
    /// Stop reason: "stop", "length", or "eos".
    pub stop_reason: String,
}

// ── STT result ─────────────────────────────────────────────────────────

/// Transcription result.
#[derive(Debug, Clone)]
pub struct TranscribeResult {
    pub text: String,
    pub tokens: usize,
}

// ── Voice params ───────────────────────────────────────────────────────

/// Bond-adaptive voice parameters.
#[derive(Debug, Clone)]
pub struct VoiceParams {
    /// Speech rate multiplier: 1.0 = normal, 0.5 = half speed, 2.0 = double speed.
    pub rate: f32,
    /// Pitch: 1.0 = normal, 0.5 = lower, 2.0 = higher.
    pub pitch: f32,
    /// Volume: 1.0 = full, 0.5 = half.
    pub volume: f32,
}

impl Default for VoiceParams {
    fn default() -> Self {
        Self {
            rate: 1.0,
            pitch: 1.0,
            volume: 1.0,
        }
    }
}

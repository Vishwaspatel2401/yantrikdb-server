//! Backend traits for pluggable ML inference.
//!
//! `LLMBackend` and `STTBackend` abstract over different inference engines
//! (candle, llama.cpp, external API) so the companion can use any backend
//! via `Box<dyn LLMBackend>`.

use anyhow::Result;

use crate::types::{ChatMessage, GenerationConfig, LLMResponse, TranscribeResult};

// ── LLM Backend ────────────────────────────────────────────────────────

/// Trait for pluggable LLM inference backends.
///
/// Implementations: `CandleLLM` (candle GGUF), `LlamaCppLLM` (llama.cpp),
/// `ApiLLM` (OpenAI-compatible HTTP API).
///
/// Uses `&mut dyn FnMut(&str)` for streaming to keep the trait object-safe.
pub trait LLMBackend: Send + Sync {
    /// Non-streaming chat completion.
    fn chat(&self, messages: &[ChatMessage], config: &GenerationConfig) -> Result<LLMResponse>;

    /// Streaming chat completion — calls `on_token` for each decoded text fragment.
    fn chat_streaming(
        &self,
        messages: &[ChatMessage],
        config: &GenerationConfig,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<LLMResponse>;

    /// Count tokens in a text string (for prompt budget calculations).
    fn count_tokens(&self, text: &str) -> Result<usize>;

    /// Human-readable backend name (e.g. "candle", "llama.cpp", "api").
    fn backend_name(&self) -> &str;
}

// ── STT Backend ────────────────────────────────────────────────────────

/// Trait for pluggable speech-to-text backends.
///
/// Implementations: `CandleWhisper` (candle Whisper), `ApiSTT` (cloud speech API).
pub trait STTBackend: Send + Sync {
    /// Transcribe 16kHz mono f32 PCM audio to text.
    fn transcribe(&self, pcm_16khz_mono: &[f32]) -> Result<TranscribeResult>;

    /// Expected audio sample rate (always 16000 for Whisper-based backends).
    fn sample_rate(&self) -> u32;

    /// Human-readable backend name (e.g. "candle-whisper", "api").
    fn backend_name(&self) -> &str;
}

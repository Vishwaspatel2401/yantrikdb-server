//! API-based LLM backend — sends requests to OpenAI-compatible HTTP endpoints.
//!
//! Uses `ureq` for synchronous HTTP. Supports both streaming (SSE) and
//! non-streaming modes.
//!
//! ```rust,ignore
//! let llm = ApiLLM::new("https://api.openai.com/v1", Some("sk-..."), "gpt-4o-mini");
//! let resp = llm.chat(&messages, &config)?;
//! ```

use std::io::{BufRead, BufReader};

use anyhow::{Context, Result};

use crate::chat_template;
use crate::traits::LLMBackend;
use crate::types::{ChatMessage, GenerationConfig, LLMResponse};

/// OpenAI-compatible API LLM backend.
pub struct ApiLLM {
    base_url: String,
    api_key: Option<String>,
    model: String,
}

impl ApiLLM {
    /// Create a new API backend.
    ///
    /// - `base_url`: Base URL without trailing slash (e.g. "https://api.openai.com/v1")
    /// - `api_key`: Optional Bearer token for authentication
    /// - `model`: Model name to send in the request (e.g. "gpt-4o-mini")
    pub fn new(base_url: impl Into<String>, api_key: Option<String>, model: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            api_key,
            model: model.into(),
        }
    }

    fn build_request_body(
        &self,
        messages: &[ChatMessage],
        config: &GenerationConfig,
        stream: bool,
    ) -> serde_json::Value {
        let msgs: Vec<serde_json::Value> = messages
            .iter()
            .map(|m| {
                serde_json::json!({
                    "role": m.role,
                    "content": m.content,
                })
            })
            .collect();

        let mut body = serde_json::json!({
            "model": self.model,
            "messages": msgs,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "stream": stream,
        });

        if let Some(p) = config.top_p {
            body["top_p"] = serde_json::json!(p);
        }

        if !config.stop.is_empty() {
            body["stop"] = serde_json::json!(config.stop);
        }

        if config.repeat_penalty != 1.0 {
            body["frequency_penalty"] = serde_json::json!((config.repeat_penalty - 1.0).clamp(-2.0, 2.0));
        }

        body
    }

    fn endpoint_url(&self) -> String {
        format!("{}/chat/completions", self.base_url.trim_end_matches('/'))
    }

    fn send_request(&self, body: &serde_json::Value) -> Result<ureq::Body> {
        let url = self.endpoint_url();
        let body_str = serde_json::to_string(body)?;

        let agent = ureq::Agent::new_with_config(
            ureq::config::Config::builder()
                .timeout_global(Some(std::time::Duration::from_secs(120)))
                .build()
        );

        let mut req = agent.post(&url)
            .header("Content-Type", "application/json");

        if let Some(ref key) = self.api_key {
            req = req.header("Authorization", &format!("Bearer {key}"));
        }

        let resp = req
            .send(body_str.as_bytes())
            .context("API request failed")?;

        Ok(resp.into_body())
    }
}

impl LLMBackend for ApiLLM {
    fn chat(&self, messages: &[ChatMessage], config: &GenerationConfig) -> Result<LLMResponse> {
        let body = self.build_request_body(messages, config, false);
        let mut resp_body = self.send_request(&body)?;

        let json: serde_json::Value = resp_body.read_json()?;

        // Parse OpenAI response format
        let text = json["choices"][0]["message"]["content"]
            .as_str()
            .unwrap_or("")
            .to_string();

        let prompt_tokens = json["usage"]["prompt_tokens"]
            .as_u64()
            .unwrap_or(0) as usize;
        let completion_tokens = json["usage"]["completion_tokens"]
            .as_u64()
            .unwrap_or(0) as usize;

        let stop_reason = json["choices"][0]["finish_reason"]
            .as_str()
            .unwrap_or("stop")
            .to_string();

        let tool_calls = chat_template::parse_tool_calls(&text);

        Ok(LLMResponse {
            text,
            prompt_tokens,
            completion_tokens,
            tool_calls,
            stop_reason,
        })
    }

    fn chat_streaming(
        &self,
        messages: &[ChatMessage],
        config: &GenerationConfig,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<LLMResponse> {
        let body = self.build_request_body(messages, config, true);
        let resp_body = self.send_request(&body)?;

        let reader = BufReader::new(resp_body.into_reader());

        let mut full_text = String::new();
        let mut stop_reason = "stop".to_string();

        for line_result in reader.lines() {
            let line: String = line_result.context("reading SSE line")?;

            // SSE format: "data: {...}" or "data: [DONE]"
            let data = if let Some(stripped) = line.strip_prefix("data: ") {
                stripped
            } else {
                continue;
            };

            if data == "[DONE]" {
                break;
            }

            let chunk: serde_json::Value = match serde_json::from_str(data) {
                Ok(v) => v,
                Err(_) => continue,
            };

            // Extract delta content
            if let Some(content) = chunk["choices"][0]["delta"]["content"].as_str() {
                on_token(content);
                full_text.push_str(content);
            }

            // Check finish reason
            if let Some(reason) = chunk["choices"][0]["finish_reason"].as_str() {
                stop_reason = reason.to_string();
            }
        }

        let tool_calls = chat_template::parse_tool_calls(&full_text);

        Ok(LLMResponse {
            text: full_text,
            prompt_tokens: 0, // SSE doesn't always report token counts
            completion_tokens: 0,
            tool_calls,
            stop_reason,
        })
    }

    fn count_tokens(&self, text: &str) -> Result<usize> {
        // API backends don't have local tokenizers — return an estimate.
        // ~4 chars per token is a reasonable approximation for English.
        Ok(text.len() / 4)
    }

    fn backend_name(&self) -> &str {
        "api"
    }
}

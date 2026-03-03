//! API-based LLM backend — sends requests to OpenAI-compatible HTTP endpoints.
//!
//! Uses `ureq` for synchronous HTTP. Supports both streaming (SSE) and
//! non-streaming modes, including native OpenAI tool calling.
//!
//! ```rust,ignore
//! let llm = ApiLLM::new("https://api.openai.com/v1", Some("sk-..."), "gpt-4o-mini");
//! let resp = llm.chat(&messages, &config, Some(&tools))?;
//! ```

use std::collections::HashMap;
use std::io::{BufRead, BufReader};

use anyhow::{Context, Result};

use crate::chat_template;
use crate::traits::LLMBackend;
use crate::types::{ApiToolCall, ApiToolCallFunction, ChatMessage, GenerationConfig, LLMResponse, ToolCall};

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

    /// Serialize a ChatMessage to JSON, including tool_calls/tool_call_id/name when present.
    fn serialize_message(m: &ChatMessage) -> serde_json::Value {
        let mut msg = serde_json::json!({ "role": m.role });

        // Content can be null for assistant messages that only have tool_calls
        if m.role == "assistant" && m.content.is_empty() && m.tool_calls.is_some() {
            msg["content"] = serde_json::Value::Null;
        } else {
            msg["content"] = serde_json::json!(m.content);
        }

        if let Some(ref calls) = m.tool_calls {
            msg["tool_calls"] = serde_json::json!(calls);
        }
        if let Some(ref id) = m.tool_call_id {
            msg["tool_call_id"] = serde_json::json!(id);
        }
        if let Some(ref name) = m.name {
            msg["name"] = serde_json::json!(name);
        }

        msg
    }

    fn build_request_body(
        &self,
        messages: &[ChatMessage],
        config: &GenerationConfig,
        tools: Option<&[serde_json::Value]>,
        stream: bool,
    ) -> serde_json::Value {
        let msgs: Vec<serde_json::Value> = messages
            .iter()
            .map(Self::serialize_message)
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

        // Native tool calling — pass tools array to API
        if let Some(tools) = tools {
            if !tools.is_empty() {
                body["tools"] = serde_json::json!(tools);
            }
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

    /// Parse native tool_calls from the response JSON.
    fn parse_api_tool_calls(message: &serde_json::Value) -> Vec<ApiToolCall> {
        let Some(calls) = message["tool_calls"].as_array() else {
            return Vec::new();
        };

        calls
            .iter()
            .filter_map(|tc| {
                let id = tc["id"].as_str()?.to_string();
                let call_type = tc["type"].as_str().unwrap_or("function").to_string();
                let name = tc["function"]["name"].as_str()?.to_string();
                let arguments = tc["function"]["arguments"].as_str().unwrap_or("{}").to_string();
                Some(ApiToolCall {
                    id,
                    call_type,
                    function: ApiToolCallFunction { name, arguments },
                })
            })
            .collect()
    }
}

impl LLMBackend for ApiLLM {
    fn chat(
        &self,
        messages: &[ChatMessage],
        config: &GenerationConfig,
        tools: Option<&[serde_json::Value]>,
    ) -> Result<LLMResponse> {
        let body = self.build_request_body(messages, config, tools, false);
        let mut resp_body = self.send_request(&body)?;

        let json: serde_json::Value = resp_body.read_json()?;

        let message = &json["choices"][0]["message"];

        // Parse text content
        let text = message["content"]
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

        // Parse native tool calls from API response
        let api_tool_calls = Self::parse_api_tool_calls(message);

        // Convert API tool calls to internal ToolCall format
        let tool_calls = if !api_tool_calls.is_empty() {
            api_tool_calls
                .iter()
                .filter_map(ToolCall::from_api)
                .collect()
        } else {
            // Fallback: parse from text content (for backends that don't support native tool calling)
            chat_template::parse_tool_calls(&text)
        };

        Ok(LLMResponse {
            text,
            prompt_tokens,
            completion_tokens,
            tool_calls,
            api_tool_calls,
            stop_reason,
        })
    }

    fn chat_streaming(
        &self,
        messages: &[ChatMessage],
        config: &GenerationConfig,
        tools: Option<&[serde_json::Value]>,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<LLMResponse> {
        let body = self.build_request_body(messages, config, tools, true);
        let resp_body = self.send_request(&body)?;

        let reader = BufReader::new(resp_body.into_reader());

        let mut full_text = String::new();
        let mut stop_reason = "stop".to_string();

        // Accumulate streamed tool call deltas by index.
        // SSE chunks: delta.tool_calls[{index, id?, function:{name?, arguments?}}]
        let mut tc_ids: HashMap<usize, String> = HashMap::new();
        let mut tc_names: HashMap<usize, String> = HashMap::new();
        let mut tc_args: HashMap<usize, String> = HashMap::new();

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

            let delta = &chunk["choices"][0]["delta"];

            // Extract delta content (text)
            if let Some(content) = delta["content"].as_str() {
                on_token(content);
                full_text.push_str(content);
            }

            // Accumulate tool call deltas
            if let Some(tool_calls) = delta["tool_calls"].as_array() {
                for tc_delta in tool_calls {
                    let idx = tc_delta["index"].as_u64().unwrap_or(0) as usize;
                    if let Some(id) = tc_delta["id"].as_str() {
                        tc_ids.insert(idx, id.to_string());
                    }
                    if let Some(name) = tc_delta["function"]["name"].as_str() {
                        tc_names.entry(idx).or_default().push_str(name);
                    }
                    if let Some(args) = tc_delta["function"]["arguments"].as_str() {
                        tc_args.entry(idx).or_default().push_str(args);
                    }
                }
            }

            // Check finish reason
            if let Some(reason) = chunk["choices"][0]["finish_reason"].as_str() {
                stop_reason = reason.to_string();
            }
        }

        // Build complete API tool calls from accumulated deltas
        let mut api_tool_calls = Vec::new();
        let max_idx = tc_names.keys().copied().max().unwrap_or(0);
        for idx in 0..=max_idx {
            if let Some(name) = tc_names.get(&idx) {
                let id = tc_ids.get(&idx).cloned().unwrap_or_else(|| format!("call_{idx}"));
                let arguments = tc_args.get(&idx).cloned().unwrap_or_else(|| "{}".to_string());
                api_tool_calls.push(ApiToolCall {
                    id,
                    call_type: "function".to_string(),
                    function: ApiToolCallFunction {
                        name: name.clone(),
                        arguments,
                    },
                });
            }
        }

        // Convert to internal format
        let tool_calls = if !api_tool_calls.is_empty() {
            api_tool_calls
                .iter()
                .filter_map(ToolCall::from_api)
                .collect()
        } else {
            chat_template::parse_tool_calls(&full_text)
        };

        Ok(LLMResponse {
            text: full_text,
            prompt_tokens: 0, // SSE doesn't always report token counts
            completion_tokens: 0,
            tool_calls,
            api_tool_calls,
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

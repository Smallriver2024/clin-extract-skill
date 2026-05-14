# API Configuration Guide

How to configure LLM API access for the extraction pipeline. Covers supported
providers, model selection, concurrency tuning, and security practices.

## Supported providers

The generated scripts use the OpenAI-compatible chat completions API, which is
supported by many providers:

| Provider | Base URL | Notes |
|----------|----------|-------|
| DashScope (Alibaba) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen series, good for Chinese medical text |
| OpenAI | `https://api.openai.com/v1` | GPT-4o, GPT-4o-mini |
| DeepSeek | `https://api.deepseek.com/v1` | deepseek-chat, good cost-performance |
| Together AI | `https://api.together.xyz/v1` | Many open models |
| Groq | `https://api.groq.com/openai/v1` | Fast inference |
| Ollama (local) | `http://localhost:11434/v1` | Local models, no API cost |
| vLLM (self-hosted) | `http://<host>:8000/v1` | Self-hosted open models |

## Model selection by task

| Stage | Priority | Recommended models | Tokens per call (approx.) |
|-------|----------|-------------------|--------------------------|
| Prescreen | Speed + Cost | `qwen-flash`, `gpt-4o-mini`, `deepseek-chat` | ~500 input + ~100 output |
| Main extraction | Balance | `qwen3.5-plus`, `gpt-4o`, `deepseek-chat` | ~2000 input + ~1000 output |
| Arbitration | Quality | `qwen3-max`, `gpt-4o`, `deepseek-reasoner` | ~2000 input + ~1000 output |

### Budget estimation

For a pipeline processing N articles:
- Prescreen: N calls (each cheap)
- Main extraction: N calls (for screened-in articles, roughly 20-50% of N)
- Arbitration: ~10-20% of extracted calls

Example: 10,000 PubMed hits → ~10,000 prescreen calls → ~3,000 extract calls → ~300 arbitration calls

## API key configuration

### Method 1: Environment variable (recommended)

```bash
export LLM_API_KEY="sk-your-key-here"
export LLM_API_KEYS="sk-key1,sk-key2,sk-key3"  # multiple keys
export LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
```

The generated script reads from these environment variables.

### Method 2: Config file

```bash
# ~/.clin-extract/config.json
{
  "api_keys": ["sk-key1", "sk-key2"],
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "primary_model": "qwen3.5-plus",
  "arbitration_model": "qwen3-max"
}
```

### Method 3: Command-line arguments

```bash
python3 extract_main.py \
  --api-key sk-xxx \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --primary-model qwen3.5-plus
```

**Security rule**: Never write actual API keys into generated scripts. Always use
a method that keeps keys out of version control.

## Concurrency tuning

The pipeline uses ThreadPoolExecutor for concurrent API calls. Key parameters:

| Parameter | Default | Guidance |
|-----------|---------|----------|
| `max_workers` | 15 | Number of concurrent threads. Set based on API rate limits. |
| `global_rps` | 8.0 | Requests per second across all workers. Check provider limits. |
| `request_timeout` | 180 | Seconds before a single request times out. Increase for slow models. |
| `max_retries` | 6 | Retries on transient failures (429, 502, 503). |
| `backoff_base` | 2.0 | Exponential backoff multiplier. |

### Provider rate limits

| Provider | Free tier RPS | Paid tier RPS |
|----------|-------------|--------------|
| DashScope | ~5 | ~20 |
| OpenAI | ~3 | ~50+ |
| DeepSeek | ~5 | ~10 |

## Retry and error handling

The generated scripts handle these error scenarios:

- **429 (rate limit)** — Exponential backoff with jitter
- **502/503 (server error)** — Retry with backoff
- **Timeout** — Retry with same parameters
- **Invalid JSON response** — Retry once, then mark as review-needed
- **Schema validation failure** — Attempt normalization, then flag

## Multi-key rotation

When multiple API keys are provided, the scripts rotate through them to:

1. Stay within per-key rate limits
2. Handle key-level quota exhaustion
3. Increase total throughput

Keys are cycled via `itertools.cycle` with a thread-local session pool per key.

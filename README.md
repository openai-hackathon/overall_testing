## Client-side scheduling demo

See [client-side-scheduling-demo.md](client-side-scheduling-demo.md) for setup, automatic verification, and two interactive Codex sessions.

## On GCP
Machine 1: vllm-router + responses_converter.py
Machine 2: codex exec

## Locally running the Full Stack

Start in this order. Each component needs its own terminal.

---

### Terminal 1 — vLLM Router
```bash
cd /Users/ryanchen/github/overall_testing/router

./target/release/vllm-router \
  --host 127.0.0.1 \
  --port 30000 \
  --worker-urls https://s9930703--vllm-serve-serve.modal.run \
  --policy round_robin \
  --worker-startup-timeout-secs 180 \
  --worker-startup-check-interval 10
```

Wait until you see:
```
Router ready | workers: ["https://s9930703--vllm-serve-serve.modal.run"]
```

---

### Terminal 2 — responses_converter
```bash
cd /Users/ryanchen/github/overall_testing

python3 responses_converter.py \
  --port 60002 \
  --upstream http://127.0.0.1:30000/v1
```

Wait until you see:
```
responses-converter  127.0.0.1:60002  →  http://127.0.0.1:30000/v1
```

---

### Terminal 3 — Codex agent
```bash
cd /Users/ryanchen/github/overall_testing/codex/codex-rs
export OPENAI_API_KEY=dummy

./target/debug/codex exec \
  --skip-git-repo-check \
  -C /Users/ryanchen/github/overall_testing \
  -c 'model_providers.modal={name="Modal via Router",base_url="http://127.0.0.1:60002/v1",env_key="OPENAI_API_KEY",wire_api="responses"}' \
  -c 'model_provider="modal"' \
  -c 'model="local"' \
  'YOUR PROMPT HERE'
```

---

### Quick health check (optional)
```bash
# Is the router up?
curl -s http://127.0.0.1:30000/health

# Is the converter up?
curl -s http://127.0.0.1:60002/health

# Full chain test (bypasses Codex)
curl -s -m 120 http://127.0.0.1:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"hi"}],"max_tokens":32,"chat_template_kwargs":{"enable_thinking":false}}'
```

---

### Notes
- **First run each session**: Modal cold-starts in 60–100 s — the router will wait
- **Rebuild router** (if you modify it): `cargo build --release` in the router directory
- **Modal stays warm** for a few minutes after first request; subsequent calls are ~1 s
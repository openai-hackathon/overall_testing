## GCP Two-Machine Setup — Full Working Guide

---

### Architecture

```
Machine 2 (e2-medium)          Machine 1 (e2-small)
┌─────────────────┐            ┌──────────────────────────────┐
│  codex exec     │──:60002───▶│ responses_converter.py       │
│                 │            │   │ localhost:30000           │
└─────────────────┘            │   ▼                          │
                               │ vllm-router ─────────────────┼──▶ Modal (vLLM)
                               └──────────────────────────────┘
```

---

### Pre-flight — On Your Mac

Push the repo to GitHub (only needed once):
```bash
cd /path/to/overall_testing
git push
```

---

### GCP Console Setup

**1. Create Machine 1 (Router + Converter):**

| Field         | Value                           |
| ------------- | ------------------------------- |
| Name          | `machine-1-router`              |
| Region / Zone | `us-central1` / `us-central1-a` |
| Machine type  | `e2-small` (2 vCPU, 2 GB)       |
| Boot disk     | Ubuntu 22.04 LTS, **30 GB**     |
| Firewall      | Allow HTTP + HTTPS              |
| External IP   | Ephemeral (default)             |

**2. Create Machine 2 (Codex):**

| Field         | Value                           |
| ------------- | ------------------------------- |
| Name          | `machine-2-codex`               |
| Region / Zone | `us-central1` / `us-central1-a` |
| Machine type  | `e2-medium` (2 vCPU, 4 GB)      |
| Boot disk     | Ubuntu 22.04 LTS, **100 GB**    |
| Firewall      | Allow HTTP + HTTPS              |
| External IP   | Ephemeral (default)             |

> Machine 2 needs 100 GB — the codex-rs Rust build alone takes ~10–15 GB of artifacts.

**3. Open port 60002 between VMs** — run in Cloud Shell:
```bash
gcloud compute firewall-rules create allow-converter-internal \
  --network=default \
  --allow=tcp:60002 \
  --source-ranges=10.128.0.0/9 \
  --description="Allow Codex machine to reach converter"
```

**4. Note Machine 1's Internal IP** from Compute Engine → VM Instances (e.g. `10.128.0.3`). You'll need it on Machine 2.

---

### Machine 1 Setup

SSH into `machine-1-router`:

```bash
# 1. Install dependencies
sudo apt update && sudo apt install -y git python3 python3-pip \
  build-essential pkg-config libssl-dev

# 2. Add swap (prevents OOM during Rust build)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile

# 3. Install Rust
curl https://sh.rustup.rs -sSf | sh -s -- -y
source $HOME/.cargo/env

# 4. Clone repo
git clone https://github.com/YOUR_USER/overall_testing.git

# 5. Build router (~5–10 min)
cd ~/overall_testing/router
CARGO_BUILD_JOBS=2 cargo build --release
cd ..

# 6. Install Python deps for converter
pip3 install fastapi uvicorn httpx --break-system-packages
```

**Run both services — open two SSH tabs to Machine 1:**

**SSH Tab 1 — Router:**
```bash
cd ~/overall_testing/router
./target/release/vllm-router \
  --host 0.0.0.0 \
  --port 30000 \
  --worker-urls https://s9930703--vllm-serve-serve.modal.run \
  --policy round_robin \
  --worker-startup-timeout-secs 180 \
  --worker-startup-check-interval 10
```
Wait for: `Router ready | workers: [...]`

**SSH Tab 2 — Converter:**
```bash
cd ~/overall_testing
python3 responses_converter.py \
  --port 60002 \
  --upstream http://127.0.0.1:30000/v1
```
Wait for: `responses-converter  0.0.0.0:60002  →  http://127.0.0.1:30000/v1`

> If SSH drops, both processes die — just re-open the tabs and rerun the two commands above.

---

### Machine 2 Setup

SSH into `machine-2-codex`:

```bash
# 1. Install dependencies
sudo apt update && sudo apt install -y git build-essential \
  pkg-config libssl-dev cloud-guest-utils

# 2. Add swap (codex-rs build is very heavy)
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile

# 3. Install Rust
curl https://sh.rustup.rs -sSf | sh -s -- -y
source $HOME/.cargo/env

# 4. Clone repo
git clone https://github.com/YOUR_USER/overall_testing.git

# 5. Build codex binary only (~15–30 min)
cd ~/overall_testing/codex/codex-rs
CARGO_BUILD_JOBS=2 cargo build --bin codex
```

> If disk fills up during build: In GCP Console resize the disk → then run `sudo growpart /dev/sda 1 && sudo resize2fs /dev/sda1`

---

### Run Codex

Replace `MACHINE1_INTERNAL_IP` with Machine 1's actual internal IP:

```bash
cd ~/overall_testing/codex/codex-rs
export OPENAI_API_KEY=dummy

./target/debug/codex exec \
  --skip-git-repo-check \
  -C ~/overall_testing \
  -c 'model_providers.modal={name="Modal via Router",base_url="http://MACHINE1_INTERNAL_IP:60002/v1",env_key="OPENAI_API_KEY",wire_api="responses"}' \
  -c 'model_provider="modal"' \
  -c 'model="local"' \
  'YOUR PROMPT HERE'
```

---

### Verify Chain (from Machine 2 before running Codex)

```bash
curl -s http://MACHINE1_INTERNAL_IP:60002/health
# Expected: {"status":"ok"}
```

# Worker deploy — Jetson Orin Nano Super (ADR 0009, issue #259)

The four **research workers** are **Jetson Orin Nano Super Developer Kits**
(8 GB unified LPDDR5, 6-core Arm A78AE + 1024-core Ampere GPU). They run
**quantized local inference only**, never run Discord, and never hold the shell
safety gate. The fleet is **homogeneous** — all four run the **same base model +
quant**, because the Phase 0 `ai-ml-generalist` adapter is replicated ×4 and the
adapter base must match the worker base (see [generalist strategy](../src/turing/coordinator/flywheel/generalist.py)).

## Pinned model

| Setting | Value |
|---|---|
| Base model | **Qwen2.5-7B-Instruct** |
| Quantization | **Q4_K_M** (≈ 4.7 GB weights) |
| Ollama tag | `qwen2.5:7b-instruct-q4_K_M` |
| Power mode | **MAXN SUPER** (`sudo nvpmodel -m 2` then `sudo jetson_clocks`) |

Pinned in [`jetson-worker.env`](jetson-worker.env) (`TURING_OLLAMA_MODEL`), applied
identically on all four nodes.

### Why this model

- **Fit.** The realistic envelope for 8 GB unified (CPU+GPU shared) memory under
  MAXN SUPER is **3–8 B at Q4**. Q4_K_M weights for a 7 B model are ≈ 4.7 GB,
  leaving headroom for the KV cache, the LoRA adapter, and the OS within 8 GB —
  whereas an 8 B at higher precision, or a 13 B at any quant, does not fit (which
  is also why the deferred automated judge must be a cloud-API call, not local).
- **Quality on the target domain.** Qwen2.5-7B-Instruct is a strong **AI/ML
  generalist** with solid technical/reasoning performance for its size — the
  Phase 0 target expertise area — and is well supported by both Ollama (Q4_K_M
  GGUF) and the H100 LoRA path, so the base is identical end to end.
- **Homogeneity.** A single pinned base lets one trained adapter replicate to all
  four workers and any node take any question (fault tolerance).

## Setup (per node, identical)

```bash
# 1. Max performance power mode (8 GB Orin Nano Super: mode 2 = MAXN SUPER)
sudo nvpmodel -m 2
sudo jetson_clocks

# 2. Pull the pinned model
ollama pull qwen2.5:7b-instruct-q4_K_M

# 3. Point the worker at it
sudo cp deploy/jetson-worker.env /etc/turing/worker.env   # set TURING_NATS_URL to the coordinator

# 4. Install + start the worker unit
sudo cp deploy/turing-worker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now turing-worker
```

## On-device verification + benchmark (operator)

The acceptance criteria below require a **real Jetson** to confirm fit and record
throughput. Run [`scripts/jetson_model_bench.sh`](scripts/jetson_model_bench.sh)
on one node and paste the numbers into the table — **do not estimate them**.

```bash
deploy/scripts/jetson_model_bench.sh qwen2.5:7b-instruct-q4_K_M
```

### Deploy notes — to be filled on first deploy

| Metric | Value | How |
|---|---|---|
| Loads + runs within 8 GB shared memory | _(pending hardware)_ | `tegrastats` peak RAM during inference |
| Tokens/s (eval rate) | _(pending hardware)_ | `ollama run --verbose` eval rate |
| Memory headroom at idle / under load | _(pending hardware)_ | `tegrastats` RAM used / 8 GB |
| Base model identical across all 4 nodes | _(pending hardware)_ | `ollama list` on each node |

## Acceptance checklist

| Acceptance criterion (#259) | Status |
|---|---|
| A specific base model + Q4 quant selected and pinned in worker config for all 4 Jetsons | ✅ pinned (`jetson-worker.env`) |
| Verified to load + run inference within 8 GB on a real Orin Nano Super | operator (hardware) — use the bench script |
| Tokens/s + memory headroom recorded in deploy notes | operator (hardware) — fill the table above |
| Base model identical across all four workers | ✅ pinned identically; confirm with `ollama list` |

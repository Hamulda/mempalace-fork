# M1 Runtime Benchmark — MacBook Air M1 8GB

## Purpose

Validate MemPalace performance and memory safety on MacBook Air M1 8GB unified memory.
Measure whether the system is safe and useful under actual hardware constraints.

## Hardware Constraints

| Resource | Value |
|----------|-------|
| Chip | Apple Silicon M1 |
| RAM | 8GB Unified Memory Architecture (UMA) |
| Python | 3.14 |
| Storage | LanceDB |
| Search | FTS5 (keyword) + vector hybrid |
| Embedding | fastembed (fallback) / MLX Metal (daemon) |

## Run Commands

### 1. Doctor (system check)

```bash
python scripts/m1_runtime_doctor.py
python scripts/m1_runtime_doctor.py --json  # machine-readable
```

Expected output keys:
- `python_version`, `python_executable`
- `proc_rss_mb`, `available_mem_mb`
- `swap_detected` — must be `false`
- `lancedb_version`, `pyarrow_version`
- `chromadb_in_modules` — must be `false`
- `palace_path`, `lance_collection_count`, `fts5_count`, `symbol_index_stats`

### 2. Benchmark (synthetic fixture)

```bash
python scripts/m1_rag_benchmark.py \
  --fixture synthetic-small \
  --concurrency 1 \
  --duration-seconds 20
```

Must complete in <30s with no swap triggered.

### 3. Benchmark (project path)

```bash
python scripts/m1_rag_benchmark.py \
  --project-path /path/to/your/project \
  --concurrency 1 \
  --duration-seconds 60
```

### 4. Benchmark (mine + search)

```bash
python scripts/m1_rag_benchmark.py --fixture synthetic-small --mine --queries \
  --concurrency 1 \
  --duration-seconds 30
```

### 5. Concurrent load test

```bash
python scripts/m1_rag_benchmark.py \
  --project-path /path/to/project \
  --mine \
  --concurrency 4 \
  --duration-seconds 60
```

## Acceptable Values on M1 Air 8GB

### Doctor

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Process RSS | <200 MB | 200–400 MB | >400 MB |
| Available memory | >2000 MB | 1000–2000 MB | <1000 MB |
| Swap used | 0 MB | 0 MB | >0 MB |
| ChromaDB in sys.modules | false | — | true |

### Benchmark (synthetic-small, 1 concurrency)

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Mine wall time | <5 s | 5–15 s | >15 s |
| Search p50 latency | <500 ms | 500–2000 ms | >2000 ms |
| Search p95 latency | <2000 ms | 2–5 s | >5 s |
| RSS delta | <100 MB | 100–300 MB | >300 MB |
| Errors | 0 | 1–2 | >2 |
| Zero-result queries | <20% | 20–50% | >50% |

### Benchmark (real project, 4 concurrency)

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Mine wall time | <30 s | 30–120 s | >120 s |
| Swap triggered | false | — | true |
| Process RSS peak | <6000 MB | 6000–7500 MB | >7500 MB |

## ABORT Conditions

Stop benchmark immediately and report if:
1. `swap_detected` becomes `true` at any point
2. Process RSS exceeds 6GB
3. Available memory drops below 1GB
4. ChromaDB appears in `sys.modules`
5. Benchmark does not complete within `--duration-seconds`

## If Swap is Detected

1. **Stop all non-essential processes** — other apps consuming RAM
2. **Restart the benchmark** — after freeing memory
3. **Check for memory leaks** — if swap recurs on repeated runs
4. **Do not continue** — swap on M1 8GB degrades performance dramatically
5. **Report** — file issue with `swap_warning: true` in the report JSON

## Output

Benchmark writes JSON report to `probe_runtime/benchmark_report.json` containing:
- `wall_time_s`, `files_processed`, `chunks_inserted`
- `rss_before_mb`, `rss_after_mb`
- `p50_latency_ms`, `p95_latency_ms`
- `error_count`, `zero_result_count`
- `chromadb_in_modules`
- `swap_warning`
- `output_path`

## Reranker

The benchmark does NOT load the reranker by default.
To test reranker performance:

```bash
python scripts/m1_rag_benchmark.py \
  --fixture synthetic-small \
  --queries \
  --rerank \
  --concurrency 1
```

The `--rerank` flag signals the benchmark to warm up the BGE reranker-v2-m3.
Without `--rerank`, reranker is never loaded — preserving M1 8GB memory budget.

## Tests

```bash
pytest tests/test_m1_runtime_doctor.py tests/test_m1_rag_benchmark_smoke.py -v
```

Tests verify:
- Scripts import without heavy model loading
- Doctor returns JSON with all required keys
- Synthetic-small fixture completes within 30s
- No ChromaDB import during benchmark
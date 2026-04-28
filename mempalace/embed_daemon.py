"""
MemPalace Embedding Daemon.

Loads the fastembed ONNX model once, serves embedding requests
via Unix domain socket. All MemPalace processes share one model instance.

Usage:
    python -m mempalace.embed_daemon
    mempalace embed-daemon start
    mempalace embed-daemon stop
    mempalace embed-daemon status
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Bounded worker pool — prevents thread-per-connection storm on M1/8GB
_bg_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="embed_client")

SOCKET_PATH = os.path.expanduser("~/.mempalace/embed.sock")
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
CACHE_DIR = os.path.expanduser("~/.cache/fastembed")

# M1 8GB safety: chunk embeddings to avoid memory pressure during batch inference.
# 32 texts × ~256 tokens × embedding_dim ≈ 32MB peak per chunk with MLX.
# This prevents OOM when 6 parallel Claude Code sessions each send large batches.
MAX_BATCH = 32  # baseline — adaptive sizing via _get_embed_batch_size()


def _get_embed_batch_size() -> int:
    """
    Return embedding batch size adapted to current memory pressure.

    CRITICAL (>90% RAM used): 8  — minimal batch, prevent OOM
    WARN (70-90%): 16            — reduced batch
    NOMINAL (<70%): 64           — full speed, safe for 85MB model on 8GB M1
    """
    try:
        from mempalace.memory_guard import MemoryGuard, MemoryPressure
        guard = MemoryGuard.get()
        if guard.pressure == MemoryPressure.CRITICAL:
            return 8
        elif guard.pressure == MemoryPressure.WARN:
            return 16
    except Exception:
        pass
    # Fallback: use env var or default 64 for nominal
    return int(os.environ.get("MEMPALACE_EMBED_BATCH", "64"))


def _create_embedding_model():
    """
    Vytvoří embedding model s prioritou:
    1. mlx-embeddings (nativní Apple Silicon, unified memory)
    2. fastembed + CoreML EP (ONNX bridge přes ANE)
    3. fastembed CPU (fallback)
    """
    import platform
    is_apple_silicon = (
        platform.system() == "Darwin" and
        platform.machine() == "arm64"
    )

    if is_apple_silicon:
        # Pokus 1: mlx-embeddings (nativní)
        try:
            return _create_mlx_model()
        except Exception as e:
            logger.info("mlx-embeddings nedostupné (%s), zkouším CoreML ONNX...", e)

        # Pokus 2: fastembed + CoreML EP (stávající implementace)
        try:
            return _create_coreml_model()
        except Exception as e:
            logger.info("CoreML EP selhal (%s), fallback na CPU...", e)

    # Pokus 3: CPU fallback
    return _create_cpu_model()


def _create_mlx_model():
    """
    Nomic-embed-text v1.5 MLX (360M params, 256-dim).
    ~85MB RAM, native Apple Silicon Metal, 512 token context.
    """
    from mlx_embeddings.utils import load as mlx_load
    import numpy as np
    import mlx.core as mx

    MODEL_ID = "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M"

    logger.info("Loading MLX embedding model %s...", MODEL_ID)
    model, tokenizer = mlx_load(MODEL_ID)

    class MLXEmbeddingWrapper:
        DIMS = 256  # Matryoshka truncation

        def __init__(self, m, tok):
            self._model = m
            self._tokenizer = tok
            self._warmup()

        def _warmup(self):
            self._embed_batch(["warmup"])
            try:
                mx.metal.clear_cache()
            except Exception:
                pass
            logger.info("MLX embedding model ready (dims=%d)", self.DIMS)

        def _embed_batch(self, texts: list[str]) -> np.ndarray:
            inputs = self._tokenizer.batch_encode_plus(
                texts,
                return_tensors="mlx",
                padding=True,
                truncation=True,
                max_length=512,
            )
            outputs = self._model(
                inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
            )
            embeddings = np.array(outputs.text_embeds)
            embeddings = embeddings[:, : self.DIMS]
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            return embeddings / (norms + 1e-9)

        def embed(self, texts):
            result = self._embed_batch(list(texts))
            # After each batch, clear Metal cache to prevent memory buildup
            try:
                mx.metal.clear_cache()
            except Exception:
                pass
            return iter(result)

    return MLXEmbeddingWrapper(model, tokenizer)


def _create_coreml_model():
    """CoreML EP přes fastembed (stávající implementace)."""
    from fastembed import TextEmbedding
    cache_dir = CACHE_DIR
    model = TextEmbedding(
        model_name=EMBED_MODEL,
        cache_dir=cache_dir,
        providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
    )
    logger.info("Compiling model for CoreML/ANE (first run only, ~10s)...")
    list(model.embed(["CoreML warmup"]))
    logger.info("CoreML model ready – inference now runs on ANE/Metal")
    return model


def _create_cpu_model():
    """CPU fallback."""
    from fastembed import TextEmbedding
    cache_dir = CACHE_DIR
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=cache_dir)
    list(model.embed(["CPU warmup"]))
    logger.info("fastembed CPU mode (žádná GPU/ANE akcelerace)")
    return model


def get_socket_path() -> str:
    return os.environ.get("MEMPALACE_EMBED_SOCK", SOCKET_PATH)


def get_pid_path() -> str:
    """PID file lives next to the socket, not at a hardcoded path."""
    return get_socket_path().replace(".sock", ".pid")


def _daemon_sanitize_embeddings(
    embeddings: list[list[float]], *, dims: int = 256
) -> list[list[float]]:
    """
    Ensure all embeddings are finite before sending to client.

    Replaces sparse NaN/Inf with 0.0 and renormalizes.
    Raises RuntimeError if an embedding is all-invalid (cannot be repaired).
    """
    repaired = 0
    for emb in embeddings:
        has_bad = any(not math.isfinite(v) for v in emb)
        if not has_bad:
            continue
        # Replace bad values with 0.0
        cleaned = [0.0 if not math.isfinite(v) else v for v in emb]
        norm = math.sqrt(sum(v * v for v in cleaned))
        if norm > 1e-9:
            factor = 1.0 / norm
            for idx in range(len(cleaned)):
                cleaned[idx] *= factor
            repaired += 1
            logger.debug("Daemon repaired NaN/Inf embedding [%d values]", sum(1 for v in emb if not math.isfinite(v)))
        else:
            raise RuntimeError(
                f"Daemon produced degenerate embedding (all-zero or all-NaN). "
                "The MLX model failed on this input."
            )
    if repaired:
        logger.info("Daemon repaired %d NaN/Inf embedding(s)", repaired)
    return embeddings


def _handle_client(conn: socket.socket, model) -> None:
    """Handle a single client request in a dedicated thread."""
    from mempalace.memory_guard import MemoryGuard

    guard = MemoryGuard.get()

    if guard.should_pause_writes():
        logger.warning("Memory pressure CRITICAL – pausing embedding requests")
        guard.wait_for_nominal(timeout=15.0)
    elif guard.should_throttle():
        # Malé zpoždění aby GC mohl uvolnit paměť
        time.sleep(0.1)

    try:
        # Read message length (4 bytes big-endian)
        raw_len = b""
        while len(raw_len) < 4:
            chunk = conn.recv(4 - len(raw_len))
            if not chunk:
                return
            raw_len += chunk
        msg_len = int.from_bytes(raw_len, "big")

        # Read message body
        data = b""
        while len(data) < msg_len:
            chunk = conn.recv(min(65536, msg_len - len(data)))
            if not chunk:
                return
            data += chunk

        request = json.loads(data.decode("utf-8"))
        texts: List[str] = request.get("texts", [])

        if not texts:
            response = {"embeddings": [], "error": None}
        else:
            # Chunk to adaptive batch size to prevent memory exhaustion on M1 8GB.
            # Dynamic sizing via _get_embed_batch_size() adapts to memory pressure.
            all_embeddings = []
            batch_size = _get_embed_batch_size()
            for i in range(0, len(texts), batch_size):
                chunk = texts[i:i + batch_size]
                chunk_embs = [emb.tolist() for emb in model.embed(chunk)]
                all_embeddings.extend(chunk_embs)
            # Daemon-side finite check: repair sparse NaN/Inf before JSON response
            all_embeddings = _daemon_sanitize_embeddings(all_embeddings)
            response = {"embeddings": all_embeddings, "error": None}

        payload = json.dumps(response).encode("utf-8")
        conn.sendall(len(payload).to_bytes(4, "big") + payload)

    except Exception as e:
        try:
            err = json.dumps({"embeddings": [], "error": str(e)}).encode("utf-8")
            conn.sendall(len(err).to_bytes(4, "big") + err)
        except Exception as send_err:
            logger.warning("handle_client: failed to send error response: %s", send_err)
    finally:
        conn.close()


def _send_socket(payload: dict, timeout: float = 30.0) -> dict:
    """Send JSON payload via unix socket, return parsed response."""
    import socket as _sock
    sock = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(get_socket_path())
        encoded = json.dumps(payload).encode("utf-8")
        sock.sendall(len(encoded).to_bytes(4, "big") + encoded)
        raw_len = b""
        while len(raw_len) < 4:
            chunk = sock.recv(4 - len(raw_len))
            if not chunk:
                raise ConnectionError("Daemon closed connection")
            raw_len += chunk
        msg_len = int.from_bytes(raw_len, "big")
        if msg_len > 10_000_000:
            raise RuntimeError(f"Response too large: {msg_len} bytes")
        data = b""
        while len(data) < msg_len:
            chunk = sock.recv(min(65536, msg_len - len(data)))
            if not chunk:
                raise ConnectionError("Daemon closed connection mid-message")
            data += chunk
        return json.loads(data.decode("utf-8"))
    finally:
        sock.close()


def run_embed_doctor() -> bool:
    """Run protocol validation on the embed daemon. Returns True if healthy."""
    import math
    import time

    print("=== MemPalace Embed Daemon Doctor ===\n")

    sock_path = get_socket_path()
    pid_path = get_pid_path()

    # 1. Socket exists
    if not Path(sock_path).exists():
        print(f"FAIL: Socket not found: {sock_path}")
        return False
    print(f"OK   socket exists: {sock_path}")

    # 2. PID file
    if Path(pid_path).exists():
        try:
            pid = int(Path(pid_path).read_text())
            print(f"OK   PID file: {pid}")
        except Exception:
            print("WARN PID file unreadable")
    else:
        print("WARN no PID file")

    # 3. Process alive (PID from pid file)
    alive = False
    if Path(pid_path).exists():
        try:
            pid = int(Path(pid_path).read_text())
            os.kill(pid, 0)
            alive = True
            print(f"OK   process alive (PID {pid})")
        except ProcessLookupError:
            print("FAIL process not running (stale PID)")
            return False
        except Exception as e:
            print(f"WARN cannot check process: {e}")

    # 4. Empty batch probe
    print("\n--- Protocol Tests ---")
    try:
        resp = _send_socket({"texts": []})
        if isinstance(resp, dict) and "embeddings" in resp:
            print("OK   empty batch → valid JSON with embeddings key")
        else:
            print(f"FAIL empty batch returned unexpected structure: {type(resp)}")
            return False
    except Exception as e:
        print(f"FAIL empty batch probe failed: {e}")
        return False

    # 5. Single embedding
    try:
        t0 = time.monotonic()
        resp = _send_socket({"texts": ["hello"]})
        latency_1 = time.monotonic() - t0
        if not isinstance(resp, dict) or "embeddings" not in resp:
            print(f"FAIL single embedding returned no embeddings key")
            return False
        embeds = resp["embeddings"]
        if len(embeds) != 1:
            print(f"FAIL expected 1 embedding, got {len(embeds)}")
            return False
        vec = embeds[0]
        if len(vec) != 256:
            print(f"FAIL dimension {len(vec)} != 256 (possible 384-dim model leak)")
            return False
        if not all(math.isfinite(x) for x in vec):
            print(f"FAIL vector contains NaN/Inf")
            return False
        norm = math.sqrt(sum(x * x for x in vec))
        if norm < 1e-6:
            print(f"FAIL zero-norm vector (norm={norm:.2e})")
            return False
        print(f"OK   1 embedding: dim=256 norm={norm:.4f} latency={latency_1*1000:.1f}ms")
    except Exception as e:
        print(f"FAIL single embedding failed: {e}")
        return False

    # 6. Batch 10
    try:
        t0 = time.monotonic()
        resp = _send_socket({"texts": [f"text{i}" for i in range(10)]})
        latency_10 = time.monotonic() - t0
        embeds = resp["embeddings"]
        if len(embeds) != 10:
            print(f"FAIL batch 10: expected 10, got {len(embeds)}")
            return False
        for i, vec in enumerate(embeds):
            if len(vec) != 256:
                print(f"FAIL batch 10: vector[{i}] dim={len(vec)}")
                return False
            if not all(math.isfinite(x) for x in vec):
                print(f"FAIL batch 10: vector[{i}] contains NaN/Inf")
                return False
        print(f"OK   batch 10: 10 embeddings, latency={latency_10*1000:.1f}ms")
    except Exception as e:
        print(f"FAIL batch 10 failed: {e}")
        return False

    # 7. Batch 100
    try:
        t0 = time.monotonic()
        resp = _send_socket({"texts": [f"short text {i}" for i in range(100)]})
        latency_100 = time.monotonic() - t0
        embeds = resp["embeddings"]
        if len(embeds) != 100:
            print(f"FAIL batch 100: expected 100, got {len(embeds)}")
            return False
        for i, vec in enumerate(embeds):
            if len(vec) != 256:
                print(f"FAIL batch 100: vector[{i}] dim={len(vec)}")
                return False
            if not all(math.isfinite(x) for x in vec):
                print(f"FAIL batch 100: vector[{i}] contains NaN/Inf")
                return False
        print(f"OK   batch 100: 100 embeddings, latency={latency_100*1000:.1f}ms")
    except Exception as e:
        print(f"FAIL batch 100 failed: {e}")
        return False

    print("\n=== All checks passed ===")
    return True


def run_daemon() -> None:
    """Main daemon loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [embed-daemon] %(message)s",
        stream=sys.stdout,
    )

    import platform
    is_apple_silicon = (
        platform.system() == "Darwin" and platform.machine() == "arm64"
    )

    # Detect model type for logging
    model_type = "unknown"
    if is_apple_silicon:
        try:
            import mlx_embeddings
            model_type = "MLX (Apple Silicon)"
        except Exception:
            model_type = "CoreML/CPU"
    else:
        model_type = "CPU/fastembed"

    logger.info("Loading %s embedding model %s...", model_type, EMBED_MODEL)
    t0 = time.monotonic()
    try:
        model = _create_embedding_model()
    except Exception as e:
        logger.error("Model load failed: %s — exiting", e)
        sys.exit(1)
    warmup_ms = (time.monotonic() - t0) * 1000
    logger.info(
        "Model loaded and warmed up (%.0fms, type=%s) at %s",
        warmup_ms, model_type,
        get_socket_path(),
    )

    sock_path = get_socket_path()
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)

    # Remove stale socket
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    os.chmod(sock_path, 0o600)
    server.listen(32)

    # Write PID file next to socket so stop/status can find it regardless of custom sock path
    pid_path = get_pid_path()
    Path(pid_path).write_text(str(os.getpid()))

    logger.info("Embedding daemon ready at %s (PID %d)", sock_path, os.getpid())
    print("READY", flush=True)

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        server.close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        try:
            os.unlink(pid_path)
        except FileNotFoundError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while True:
            conn, _ = server.accept()
            _bg_executor.submit(_handle_client, conn, model)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        _bg_executor.shutdown(wait=True)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        try:
            os.unlink(pid_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    run_daemon()

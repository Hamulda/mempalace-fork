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
import os
import signal
import socket
import sys
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
    ModernBERT-embed-base 4-bit MLX.
    MTEB ~61.17 (256-dim Matryoshka truncation vs full 768-dim = 62.62),
    8192 token context, ~85MB RAM, nativní Apple Silicon.
    """
    from mlx_embeddings import load, generate
    import numpy as np

    MODEL_ID = "mlx-community/nomicai-modernbert-embed-base-4bit"

    logger.info("Loading modernbert-embed-base 4-bit MLX...")
    model, tokenizer = load(MODEL_ID)

    class ModernBERTWrapper:
        """
        Wrapper kompatibilní s fastembed .embed() API.
        Podporuje Matryoshka dims – používáme 256 místo 768
        pro 3x menší LanceDB storage bez výrazné ztráty kvality.
        """
        DIMS = 256  # Matryoshka: 256 dims = MTEB 61.17 vs 768 dims = 62.62

        def __init__(self, m, tok):
            self._model = m
            self._tokenizer = tok
            # Warm-up
            self._embed_batch(["warmup"])
            logger.info(
                "✅ ModernBERT-embed 4-bit MLX ready "
                "(dims=%d, context=8192, RAM~85MB)",
                self.DIMS,
            )

        def _embed_batch(self, texts: list[str]) -> np.ndarray:
            output = generate(self._model, self._tokenizer, texts=texts)
            # Force MLX to synchronize GPU→CPU transfer before clearing cache
            try:
                import mlx.core as mx
                mx.eval(output.text_embeds)
            except Exception:
                pass  # mx.eval not available on all MLX builds
            embeddings = np.array(output.text_embeds)
            # Matryoshka truncation: prvních DIMS dimenzí + L2 normalizace
            embeddings = embeddings[:, : self.DIMS]
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            return embeddings / (norms + 1e-9)

        def embed(self, texts: list[str]):
            """Kompatibilní s fastembed API."""
            result = self._embed_batch(list(texts))
            # Return iterator of numpy arrays (each with .tolist() method)
            return iter(result)

    wrapper = ModernBERTWrapper(model, tokenizer)

    # Post-load cache hygiene: evict KV cache after warmup to free ~0.75GB
    try:
        import mlx.core as mx
        mx.eval(wrapper._embed_batch(["post_load"]))
        mx.metal.clear_cache()
    except Exception:
        pass  # Only available on MLX with Metal backend

    return wrapper


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
            response = {"embeddings": all_embeddings, "error": None}

        payload = json.dumps(response).encode("utf-8")
        conn.sendall(len(payload).to_bytes(4, "big") + payload)

    except Exception as e:
        try:
            err = json.dumps({"embeddings": [], "error": str(e)}).encode("utf-8")
            conn.sendall(len(err).to_bytes(4, "big") + err)
        except Exception:
            pass
    finally:
        conn.close()


def run_daemon() -> None:
    """Main daemon loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [embed-daemon] %(message)s",
        stream=sys.stdout,
    )

    # Load fastembed model with optimal provider for platform
    logger.info("Loading fastembed model %s...", EMBED_MODEL)
    model = _create_embedding_model()
    logger.info("Model loaded and warmed up.")

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

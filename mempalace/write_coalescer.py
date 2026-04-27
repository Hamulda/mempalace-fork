"""
Write coalescing for MemPalace LanceDB backend.
Shromažďuje write requesty v časovém okně a zpracuje je jako batch.
"""
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends.lance import LanceCollection

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingWrite:
    documents: list[str]
    ids: list[str]
    metadatas: list[dict]
    result_event: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


class WriteCoalescer:
    """
    Thread-safe write coalescer.

    Princip:
    - Každý add() volání zařadí write request do fronty
    - Background worker čeká MAX window_ms milliseconds
    - Poté zpracuje všechny čekající requesty jako jeden batch
    - Každý caller dostane notifikaci přes Event
    """

    def __init__(
        self,
        collection: "LanceCollection",
        window_ms: int = 500,
    ):
        self._collection = collection
        self._window_s = window_ms / 1000.0
        self._pending: list[PendingWrite] = []
        self._lock = threading.Lock()
        self._flush_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    def enqueue(
        self,
        documents: list[str],
        ids: list[str],
        metadatas: list[dict],
    ) -> None:
        """
        Zařadí write request do fronty a čeká na zpracování.
        Blokuje caller vlákno dokud není batch proveden.
        """
        write = PendingWrite(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
        )

        with self._lock:
            self._pending.append(write)
            self._schedule_flush()

        # Čekej na zpracování (max 10s – ochrana před deadlock)
        if not write.result_event.wait(timeout=10.0):
            raise TimeoutError("Write coalescer timeout after 10s")

        if write.error:
            raise write.error

    def _schedule_flush(self):
        """Naplánuje flush pokud žádný nečeká."""
        with self._timer_lock:
            if self._flush_timer is None:
                self._flush_timer = threading.Timer(
                    self._window_s, self._flush
                )
                self._flush_timer.daemon = True
                self._flush_timer.start()

    def _flush(self):
        """Zpracuje všechny čekající write requesty jako jeden batch."""
        with self._timer_lock:
            self._flush_timer = None

        with self._lock:
            if not self._pending:
                return
            batch = self._pending[:]
            self._pending.clear()

        if len(batch) > 1:
            logger.info(
                "WriteCoalescer: merged %d write requests into 1 batch transaction",
                len(batch)
            )

        # Spoj všechny requesty do jednoho batch
        all_docs = []
        all_ids = []
        all_metas = []
        for w in batch:
            all_docs.extend(w.documents)
            all_ids.extend(w.ids)
            all_metas.extend(w.metadatas)

        # Proveď batch write (přímé volání _do_add bypasses coalescer)
        try:
            self._collection._do_add(
                documents=all_docs,
                ids=all_ids,
                metadatas=all_metas,
            )
        except Exception as e:
            for w in batch:
                w.error = e

        # Notifikuj všechny čekající callery
        for w in batch:
            w.result_event.set()
"""Where an uploaded photo waits between being accepted by one instance and processed by a worker on another.

With JOB_BACKEND=local the photo never leaves the accepting instance's memory and none of this is used. With
JOB_BACKEND=cloudtasks the job is picked up by whichever instance Cloud Tasks chooses, and a task body is limited to
100 KB, so the photo goes to a Cloud Storage bucket (JOB_PAYLOAD_BUCKET) keyed by job id. The worker deletes it as
soon as it is done, and the bucket should also have a one-day lifecycle rule so nothing lingers if a worker dies.
Photos of a farmer's crop can be personal data: keep the bucket private, in the same region as the service.
"""

import logging
import threading

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = "job-payloads/"


class MemoryPayloadStore:
    """Tests, and single-instance use of the cloud job path."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, job_id: str, data: bytes) -> None:
        with self._lock:
            self._data[job_id] = data

    def get(self, job_id: str) -> bytes | None:
        with self._lock:
            return self._data.get(job_id)

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._data.pop(job_id, None)

    def __len__(self) -> int:
        return len(self._data)


class GcsPayloadStore:
    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket)

    def put(self, job_id: str, data: bytes) -> None:
        self._bucket.blob(_PREFIX + job_id).upload_from_string(data, content_type="application/octet-stream")

    def get(self, job_id: str) -> bytes | None:
        from google.api_core.exceptions import NotFound

        try:
            return self._bucket.blob(_PREFIX + job_id).download_as_bytes()
        except NotFound:
            return None

    def delete(self, job_id: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._bucket.blob(_PREFIX + job_id).delete()
        except NotFound:
            pass
        except Exception:  # noqa: BLE001 - the lifecycle rule removes it anyway
            logger.warning("could not delete job payload %s", job_id, exc_info=True)


_store = None
_lock = threading.Lock()


def get_payload_store():
    global _store
    with _lock:
        if _store is None:
            bucket = get_settings().job_payload_bucket
            _store = GcsPayloadStore(bucket) if bucket else MemoryPayloadStore()
        return _store


def use_payload_store(store) -> None:
    """Tests: install a store (or None to go back to config)."""
    global _store
    with _lock:
        _store = store

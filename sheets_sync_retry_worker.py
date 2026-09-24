"""One-shot worker for a scheduled Render Cron Job.

The durable queue and retry/backoff logic live in webhook_server_viewer.py.
This entrypoint intentionally emits only counts, never payloads or credentials.
"""

import sys

from webhook_server_viewer import process_sheets_sync_queue


def run_once():
    result = process_sheets_sync_queue(limit=20)
    if not isinstance(result, dict) or result.get("ok") is not True:
        print("No se pudo procesar la cola; seguirá pendiente para la próxima ejecución.", flush=True)
        return 1
    attempted = int(result.get("attempted", 0) or 0)
    synced = int(result.get("synced", 0) or 0)
    print(f"Cola revisada: {attempted} pendientes; {synced} sincronizados.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run_once())

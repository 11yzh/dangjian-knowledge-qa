import logging
import time
from .core import STATE, today, engine, run
from .knowledge import work_one
from scripts.maintenance import backup, housekeeping


def serve():
    last = 0
    while True:
        try:
            active = work_one()
            if time.monotonic() - last > 45 or not last:
                housekeeping()
                prefix = today().strftime("%Y%m%d")
                if not any((STATE / "backups").glob(prefix + "-*.zip")):
                    backup()
                last = time.monotonic()
            if not active:
                time.sleep(1)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            logging.error("任务进程：%s", type(exc).__name__)
            time.sleep(3)


if __name__ == "__main__":
    with engine().connect() as lease:
        if run(lease, "SELECT pg_try_advisory_lock(673106)").scalar_one():
            lease.commit()
            serve()

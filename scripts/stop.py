"""Stop the web server and document worker belonging to this checkout."""

from pathlib import Path
import psutil
from app.core import ROOT


def stop():
    selected = []
    for proc in psutil.process_iter(["name"]):
        try:
            if not (proc.info["name"] or "").lower().startswith("python"):
                continue
            args = proc.cmdline()[1:]
            is_web = args[:3] == ["-m", "uvicorn", "app.main:app"]
            is_worker = args == ["-m", "app.worker"]
            if (is_web or is_worker) and Path(proc.cwd()).resolve() == ROOT:
                selected.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
            continue
    for proc in selected:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, running = psutil.wait_procs(selected, timeout=8)
    if running:
        raise RuntimeError("部分项目进程仍在运行，请稍后重试。")
    print(f"已停止本项目的 {len(selected)} 个应用进程。")


if __name__ == "__main__":
    stop()

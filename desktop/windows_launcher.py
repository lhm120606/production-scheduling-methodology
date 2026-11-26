from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def wait_for_server(url: str, timeout_seconds: float = 20.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/health", timeout=1.0) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def copy_seed_once(root: Path, data_root: Path) -> None:
    work_root = data_root / "work"
    db_target = work_root / "data" / "guogang_schedule.db"
    upload_target = work_root / "uploads"
    output_target = data_root / "outputs"
    db_target.parent.mkdir(parents=True, exist_ok=True)
    upload_target.mkdir(parents=True, exist_ok=True)
    output_target.mkdir(parents=True, exist_ok=True)

    seed = root / "seed"
    db_seed = seed / "work" / "data" / "guogang_schedule.db"
    if not db_target.exists() and db_seed.exists():
        shutil.copy2(db_seed, db_target)

    upload_seed = seed / "work" / "uploads"
    if upload_seed.exists() and not any(upload_target.iterdir()):
        for item in upload_seed.iterdir():
            target = upload_target / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

    output_seed = seed / "outputs"
    if output_seed.exists() and not any(output_target.iterdir()):
        for item in output_seed.iterdir():
            if item.is_file():
                shutil.copy2(item, output_target / item.name)


def main() -> int:
    root = resource_root()
    documents = Path.home() / "Documents"
    data_root = documents / "国刚铸模滚动排产小工具数据"
    copy_seed_once(root, data_root)

    env = os.environ.copy()
    env["GUOGANG_BASE_DIR"] = str(root)
    env["GUOGANG_DATA_DIR"] = str(data_root / "work")
    env["GUOGANG_OUTPUT_DIR"] = str(data_root / "outputs")

    url = "http://127.0.0.1:8000/"
    server = root / "guogang-server.exe"
    if not server.exists():
        server = root / "guogang-server"

    if not wait_for_server(url, timeout_seconds=1.0):
        if not server.exists():
            raise RuntimeError(f"找不到后端程序：{server}")
        log_file = data_root / "server.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as log:
            subprocess.Popen([str(server)], cwd=str(root), env=env, stdout=log, stderr=log)
        if not wait_for_server(url):
            webbrowser.open(str(log_file))
            return 1

    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

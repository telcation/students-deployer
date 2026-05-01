from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import textwrap
import tempfile

import config


APP_USER = "appuser"
APP_GROUP = "appuser"


def run(cmd: list[str], cwd: Path | None = None, *, check: bool = True) -> None:
    print(f"[flask_runtime] run: {' '.join(cmd)}")
    subprocess.run(cmd, check=check, cwd=str(cwd) if cwd else None)


def purge_systemd_unit(service_name: str) -> None:
    """
    重要:
    - unit 本体だけでなく drop-in (/etc/systemd/system/<name>.service.d/override.conf 等) も消す
    - drop-in が残ると、古い ExecStart/port が優先されて 502 の原因になる
    """
    unit_path = Path(config.SYSTEMD_DIR) / f"{service_name}.service"
    dropin_dir = Path(config.SYSTEMD_DIR) / f"{service_name}.service.d"

    # stop/disable は失敗しても続行（存在しないケースを許容）
    run(["sudo", "-n", "systemctl", "stop", service_name], check=False)
    run(["sudo", "-n", "systemctl", "disable", service_name], check=False)
    run(["sudo", "-n", "systemctl", "reset-failed", service_name], check=False)

    # unit/drop-in を物理削除（ここが最重要）
    run(["sudo", "-n", "rm", "-f", str(unit_path)], check=False)
    run(["sudo", "-n", "rm", "-rf", str(dropin_dir)], check=False)

    run(["sudo", "-n", "systemctl", "daemon-reload"], check=False)


def copy_template_if_exists(template_dir: Path, app_dir: Path) -> None:
    if not template_dir.exists():
        return
    for src in template_dir.rglob("*"):
        rel = src.relative_to(template_dir)
        dst = app_dir / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _resolve_python_bin(python_version: str | None) -> str:
    ver = (python_version or config.DEFAULT_PYTHON_VERSION).strip()
    python_bin = config.PYTHON_BINS.get(ver, "python3")
    if python_bin != "python3" and not Path(python_bin).exists():
        raise FileNotFoundError(f"Python interpreter not found: {python_bin}")
    return python_bin


def create_venv_and_install(app_dir: Path, venv_dir: Path, python_bin: str) -> None:
    if not venv_dir.exists():
        run([python_bin, "-m", "venv", str(venv_dir)])
    pip = venv_dir / "bin" / "pip"
    run([str(pip), "install", "--upgrade", "pip"])
    run([str(pip), "install", "flask", "gunicorn"])

    req = config.FLASK_TEMPLATE_DIR / "requirements.txt"
    if req.exists():
        run([str(pip), "install", "-r", str(req)])


def render_systemd_unit(service_name: str, app_dir: Path, venv_dir: Path, port: int) -> str:
    # Nginx の X-Forwarded-Prefix を SCRIPT_NAME に反映する共通ラッパ経由で起動
    # - wsgi_prefix.py は基盤側に1つだけ置き、全受講者アプリで共通利用する
    # - 受講者アプリ本体は APP_IMPORT (= "app:app") として読み込む
    exec_start = (
        f"{venv_dir}/bin/gunicorn --bind 127.0.0.1:{port} --workers 1 "
        f"--timeout {config.GUNICORN_TIMEOUT} "
        f"--access-logfile {app_dir}/logs/access.log "
        f"--error-logfile {app_dir}/logs/error.log "
        f"--pythonpath {config.WSGI_PREFIX_DIR} "
        f"{config.WSGI_PREFIX_ENTRYPOINT}"
    )

    return textwrap.dedent(f"""
        [Unit]
        Description=Flask student app ({service_name})
        After=network.target

        [Service]
        Type=simple
        User={APP_USER}
        Group={APP_GROUP}
        WorkingDirectory={app_dir}
        ExecStart={exec_start}
        Restart=always
        Environment=PYTHONUNBUFFERED=1

        [Install]
        WantedBy=multi-user.target
    """).strip() + "\n"

def render_streamlit_systemd_unit(
    service_name: str,
    app_dir: Path,
    venv_dir: Path,
    port: int,
    term: str,
    student_id: str,
    app_name: str,
) -> str:
    base_url_path = f"f/{term}/{student_id}/{app_name}"
    exec_start = (
        f"{venv_dir}/bin/streamlit run {app_dir}/app.py "
        f"--server.address 127.0.0.1 "
        f"--server.port {port} "
        f"--server.headless true "
        f"--server.enableCORS false "
        f"--server.enableXsrfProtection false "
        f"--server.baseUrlPath {base_url_path}"
    )

    return textwrap.dedent(f"""
        [Unit]
        Description=Streamlit student app ({service_name})
        After=network.target

        [Service]
        Type=simple
        User={APP_USER}
        Group={APP_GROUP}
        WorkingDirectory={app_dir}
        ExecStart={exec_start}
        Restart=always
        Environment=PYTHONUNBUFFERED=1

        [Install]
        WantedBy=multi-user.target
    """).strip() + "\n"


def setup_streamlit_runtime(
    term: str,
    student_id: str,
    app_name: str,
    port: int,
) -> None:
    app_dir = Path(config.BASE_DIR_FLASK) / term / student_id / app_name
    venv_dir = app_dir / "venv"
    log_dir = app_dir / "logs"

    app_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    pip = venv_dir / "bin" / "pip"
    if not pip.exists():
        raise RuntimeError(f"venv が見つかりません: {venv_dir}")

    run([str(pip), "install", "streamlit"])

    req = app_dir / "requirements.txt"
    if req.exists():
        run([str(pip), "install", "-r", str(req)])

    service_name = f"flask_{term}_{student_id}_{app_name}"
    unit_text = render_streamlit_systemd_unit(
        service_name,
        app_dir,
        venv_dir,
        port,
        term,
        student_id,
        app_name,
    )

    purge_systemd_unit(service_name)

    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(unit_text)
        tmp_unit = f.name

    unit_path = Path(config.SYSTEMD_DIR) / f"{service_name}.service"
    run(["sudo", "-n", "install", "-m", "644", tmp_unit, str(unit_path)])
    run(["sudo", "-n", "systemctl", "daemon-reload"])
    run(["sudo", "-n", "systemctl", "enable", service_name])
    run(["sudo", "-n", "systemctl", "restart", service_name])


def setup_flask_runtime(
    term: str,
    student_id: str,
    app_name: str,
    port: int,
    *,
    python_version: str | None = None,
) -> None:
    app_dir = Path(config.BASE_DIR_FLASK) / term / student_id / app_name
    venv_dir = app_dir / "venv"
    log_dir = app_dir / "logs"

    app_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # gunicorn が logs/*.log に書けるように所有者/権限を揃える
    run(["sudo", "-n", "chown", "-R", f"{APP_USER}:{APP_GROUP}", str(log_dir)], check=False)
    run(["sudo", "-n", "chmod", "755", str(log_dir)], check=False)

    copy_template_if_exists(config.FLASK_TEMPLATE_DIR, app_dir)

    python_bin = _resolve_python_bin(python_version)
    create_venv_and_install(app_dir, venv_dir, python_bin)

    service_name = f"flask_{term}_{student_id}_{app_name}"
    unit_text = render_systemd_unit(service_name, app_dir, venv_dir, port)

    # ★ 最重要：同名サービスの drop-in を含めて完全削除してから作り直す
    purge_systemd_unit(service_name)

    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(unit_text)
        tmp_unit = f.name

    unit_path = Path(config.SYSTEMD_DIR) / f"{service_name}.service"
    run(["sudo", "-n", "install", "-m", "644", tmp_unit, str(unit_path)])

    run(["sudo", "-n", "systemctl", "daemon-reload"])
    run(["sudo", "-n", "systemctl", "enable", service_name])
    run(["sudo", "-n", "systemctl", "restart", service_name])

    print(f"[flask_runtime] Flask アプリ起動完了: {service_name}")

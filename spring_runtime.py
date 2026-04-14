from __future__ import annotations

from pathlib import Path
import subprocess
import textwrap
import tempfile
import shutil

import config


APP_USER = "appuser"
APP_GROUP = "appuser"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=False,
    )


def _systemd_unit_path(service_name: str) -> Path:
    return Path(config.SYSTEMD_DIR) / f"{service_name}.service"


def _systemd_dropin_dir(service_name: str) -> Path:
    # /etc/systemd/system/<name>.service.d/
    return Path(config.SYSTEMD_DIR) / f"{service_name}.service.d"


def purge_systemd_unit(service_name: str) -> None:
    """
    重要：
    - unit 本体だけでなく drop-in（override.conf）も消す
    - これをやらないと、過去の ExecStart が残って port がズレる
    """
    # stop/disable は失敗しても進める
    run(["sudo", "-n", "systemctl", "stop", service_name], check=False)
    run(["sudo", "-n", "systemctl", "disable", service_name], check=False)
    run(["sudo", "-n", "systemctl", "reset-failed", service_name], check=False)

    unit_path = _systemd_unit_path(service_name)
    dropin_dir = _systemd_dropin_dir(service_name)

    # unit / drop-in を物理削除
    try:
        run(["sudo", "-n", "rm", "-f", str(unit_path)], check=False)
    except Exception:
        pass

    # drop-in はディレクトリごと消す（ここが最重要）
    try:
        run(["sudo", "-n", "rm", "-rf", str(dropin_dir)], check=False)
    except Exception:
        pass

    run(["sudo", "-n", "systemctl", "daemon-reload"], check=False)


def render_systemd_unit(
    service_name: str,
    app_dir: Path,
    jar_path: Path,
    port: int,
    context_path: str,
    java_bin: str = "/usr/bin/java",
) -> str:
    exec_start = f"{java_bin} -jar {jar_path} --server.port={port} --server.servlet.context-path={context_path}"

    return textwrap.dedent(f"""
        [Unit]
        Description=Spring Boot student app ({service_name})
        After=network.target

        [Service]
        User={APP_USER}
        Group={APP_GROUP}
        WorkingDirectory={app_dir}
        ExecStart={exec_start}
        Restart=on-failure
        SuccessExitStatus=143
        Environment=SPRING_PROFILES_ACTIVE=prod

        [Install]
        WantedBy=multi-user.target
    """).strip() + "\n"


def _detect_java_bin(java_release: str | None = None) -> str:
    """
    config.JAVA_HOMES を優先して java を選ぶ。
    無ければ /usr/bin/java。
    """
    java_bin = "/usr/bin/java"
    jr = (java_release or getattr(config, "DEFAULT_JAVA_RELEASE", None))
    java_home = ""
    if jr is not None:
        java_home = getattr(config, "JAVA_HOMES", {}).get(str(jr), "")
    if java_home:
        cand = Path(java_home) / "bin" / "java"
        if cand.exists():
            return str(cand)
    return java_bin


def _install_fresh_unit(service_name: str, unit_text: str) -> None:
    """
    unit を「必ず新規状態」で入れる。
    - drop-in が残っていると ExecStart が上書きされるため、先に purge する
    """
    purge_systemd_unit(service_name)

    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(unit_text)
        tmp_unit = f.name

    unit_path = _systemd_unit_path(service_name)
    run(["sudo", "-n", "install", "-m", "644", tmp_unit, str(unit_path)])

    run(["sudo", "-n", "systemctl", "daemon-reload"])
    run(["sudo", "-n", "systemctl", "enable", service_name])


def setup_spring_runtime(
    term: str,
    student_id: str,
    app_name: str,
    port: int,
    jar_name: str = "app.jar",
    java_release: str | None = None,
    build_tool: str | None = None,
) -> None:
    context_path = f"/b/{term}/{student_id}/{app_name}"
    app_dir = Path(config.BASE_DIR_SPRING) / term / student_id / app_name
    app_dir.mkdir(parents=True, exist_ok=True)

    _ = (build_tool or getattr(config, "DEFAULT_SPRING_BUILD_TOOL", None))  # 互換のため保持

    jar_path = app_dir / jar_name
    if not jar_path.exists():
        raise RuntimeError(f"Spring Boot jar not found: {jar_path}")

    service_name = f"spring_{term}_{student_id}_{app_name}"
    java_bin = _detect_java_bin(java_release)

    unit_text = render_systemd_unit(service_name, app_dir, jar_path, port, context_path, java_bin)

    _install_fresh_unit(service_name, unit_text)
    run(["sudo", "-n", "systemctl", "start", service_name])

    print(f"[spring_runtime] Spring Boot 起動完了: {service_name} (127.0.0.1:{port})")


def first_deploy_spring(term: str, student_id: str, app_name: str, port: int, jar_name: str = "app.jar") -> None:
    """
    初回デプロイ（講師側）:
    - 公開枠（ディレクトリ・systemd unit）を作成し、URLを確定させるための準備を行う。
    - jar が無い場合は unit は作るが start はしない（受講者のアップロード待ち）。

    重要：
    - 既存の drop-in を残すと port がズレるので、必ず purge してから unit を入れる。
    """
    context_path = f"/b/{term}/{student_id}/{app_name}"
    app_dir = Path(getattr(config, "STUDENTS_BASE_DIR", config.BASE_DIR_SPRING)) / term / student_id / app_name
    app_dir.mkdir(parents=True, exist_ok=True)

    jar_path = app_dir / jar_name
    service_name = f"spring_{term}_{student_id}_{app_name}"

    java_bin = _detect_java_bin(getattr(config, "DEFAULT_JAVA_RELEASE", None))
    unit_text = render_systemd_unit(service_name, app_dir, jar_path, port, context_path, java_bin)

    _install_fresh_unit(service_name, unit_text)

    if jar_path.exists():
        run(["sudo", "-n", "systemctl", "start", service_name])
        print(f"[spring_runtime] Spring Boot 初回デプロイ完了: {service_name} (127.0.0.1:{port})")
    else:
        print(f"[spring_runtime] jar 未アップロードのため start をスキップ: {jar_path}")

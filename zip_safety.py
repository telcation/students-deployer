# zip_safety.py (extracted from students_redeploy.py; no behavior change)
from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable, List, Tuple

import config  # type: ignore

# Safety limits (even when check OFF)
MAX_UPLOAD_BYTES = int(getattr(config, "MAX_UPLOAD_BYTES", 200 * 1024 * 1024))
MAX_ZIP_FILES = int(getattr(config, "MAX_ZIP_FILES", 3000))
MAX_ZIP_TOTAL_BYTES = int(getattr(config, "MAX_ZIP_TOTAL_BYTES", 300 * 1024 * 1024))
MAX_SINGLE_FILE_BYTES = int(getattr(config, "MAX_SINGLE_FILE_BYTES", 30 * 1024 * 1024))

FORBIDDEN_TOP_DIRS = {
    "venv", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".idea", ".vscode", "node_modules", "dist", "build", ".git"
}
FORBIDDEN_EXTS = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".so", ".dylib",
    ".pem", ".key",
}
FORBIDDEN_EXTS_IN_ZIP = set(FORBIDDEN_EXTS) | {".jar"}  # zip提出内jar混入は禁止（事故りやすい）

@dataclass
class ZipScan:
    names: List[str]
    top_level_dirs: List[str]
    file_count: int
    total_bytes: int


def _is_forbidden_ext(path: str, forbidden: Iterable[str]) -> bool:
    ext = Path(path).suffix.lower()
    return ext in set(forbidden)


def _scan_zip(zip_path: Path) -> Tuple[ZipScan, List[str]]:
    issues: List[str] = []

    if not zip_path.exists():
        return ZipScan([], [], 0, 0), ["zipファイルが存在しません。"]

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except Exception:
        return ZipScan([], [], 0, 0), ["zipファイルが壊れています。"]

    names: List[str] = []
    top_dirs: set[str] = set()
    file_count = 0
    total = 0

    with zf:
        for info in zf.infolist():
            name = info.filename

            # skip folders and mac junk
            if not name or name.endswith("/") or name.startswith("__MACOSX/"):
                continue

            # traversal / absolute path
            if name.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", name):
                issues.append(f"zip内に絶対パスが含まれています: {name}")
                continue
            if ".." in Path(name).parts:
                issues.append(f"zip内に '..' が含まれています（危険）: {name}")
                continue

            # forbidden top dirs
            top = name.split("/")[0].lower()
            top_dirs.add(top)
            if top in FORBIDDEN_TOP_DIRS:
                issues.append(f"不要物が含まれています: {top}/")

            # forbidden extension (strict for zip)
            if _is_forbidden_ext(name, FORBIDDEN_EXTS_IN_ZIP):
                issues.append(f"禁止拡張子が含まれています: {name}")

            file_count += 1
            total += int(info.file_size or 0)
            names.append(name)

            if int(info.file_size or 0) > MAX_SINGLE_FILE_BYTES:
                issues.append(f"単体ファイルが大きすぎます（>{MAX_SINGLE_FILE_BYTES//1024//1024}MB）: {name}")

    if file_count > MAX_ZIP_FILES:
        issues.append(f"ファイル数が多すぎます（上限 {MAX_ZIP_FILES}）: {file_count}")

    if total > MAX_ZIP_TOTAL_BYTES:
        issues.append(f"zip展開後の合計サイズが大きすぎます（上限 {MAX_ZIP_TOTAL_BYTES//1024//1024}MB）: {total//1024//1024}MB")

    return ZipScan(names=names, top_level_dirs=sorted(top_dirs), file_count=file_count, total_bytes=total), issues


def _zip_is_single_folder(scan: ZipScan) -> bool:
    # ignore empty
    if not scan.names:
        return False
    # if every file has a "/" and top level is 1 dir -> likely 1-level deep
    top = {n.split("/")[0] for n in scan.names if "/" in n}
    no_slash = any("/" not in n for n in scan.names)
    return (len(top) == 1) and (not no_slash)


def _basic_file_safety_check(path: Path, filename: str) -> List[str]:
    issues: List[str] = []
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        issues.append(f"アップロードサイズが大きすぎます（上限 {MAX_UPLOAD_BYTES//1024//1024}MB）。")
    if _is_forbidden_ext(filename, FORBIDDEN_EXTS):
        issues.append(f"禁止拡張子です: {filename}")
    return issues



def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            name = member.filename
            if not name or name.endswith("/") or name.startswith("__MACOSX/"):
                continue

            # traversal / absolute
            if name.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", name):
                continue
            if ".." in Path(name).parts:
                continue

            top = name.split("/")[0].lower()
            if top in {"__macosx"} | FORBIDDEN_TOP_DIRS:
                continue

            out_path = (dest_dir / name).resolve()
            if not str(out_path).startswith(str(dest_root) + os.sep):
                continue

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member, "r") as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)



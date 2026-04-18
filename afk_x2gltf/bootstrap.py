from __future__ import annotations

import importlib
import io
import json
import os
import platform
import shutil
import sys
import sysconfig
import types
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

ProgressCb = Callable[[str, int, int], None] | None

VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "assimp"
MARKER_FILE = VENDOR_DIR / ".installed.json"

GITHUB_RELEASES_API = "https://api.github.com/repos/assimp/assimp/releases"
PINNED_TAG = "v6.0.2"
PINNED_ASSET = "windows-x64-v6.0.2.zip"
USE_LATEST = False

_USER_AGENT = "X2glTF-Bootstrap/1.0 (+https://github.com/assimp/assimp)"


class AssimpBootstrapError(RuntimeError):
    pass


def ensure_assimp(progress: ProgressCb = None, force: bool = False) -> Path | None:
    install_distutils_shim()
    if not _is_windows():
        _try_register_dll_dir()
        return None

    if not force:
        existing = _find_local_dll()
        if existing is not None:
            _register_dll_dir(existing.parent)
            return existing

        if _pyassimp_loadable():
            return None

    dll_path = _download_and_extract(progress)
    _register_dll_dir(dll_path.parent)
    return dll_path


def install_distutils_shim() -> None:
    if "distutils.sysconfig" in sys.modules and "distutils" in sys.modules:
        return
    try:
        import distutils.sysconfig  # noqa: F401

        return
    except ModuleNotFoundError:
        pass
    try:
        import setuptools  # noqa: F401

        importlib.import_module("distutils.sysconfig")
        return
    except Exception:
        pass

    distutils_mod = types.ModuleType("distutils")
    distutils_mod.__path__ = []
    sysconfig_mod = types.ModuleType("distutils.sysconfig")

    def get_python_lib(plat_specific: int = 0, standard_lib: int = 0, prefix: str | None = None) -> str:
        scheme = "platlib" if plat_specific else "purelib"
        vars_ = {"base": prefix} if prefix else None
        return sysconfig.get_path(scheme, vars=vars_)

    def get_python_inc(plat_specific: int = 0, prefix: str | None = None) -> str:
        vars_ = {"base": prefix} if prefix else None
        return sysconfig.get_path("include", vars=vars_)

    def get_config_var(name: str):
        return sysconfig.get_config_var(name)

    def get_config_vars(*names: str):
        return sysconfig.get_config_vars(*names)

    sysconfig_mod.get_python_lib = get_python_lib
    sysconfig_mod.get_python_inc = get_python_inc
    sysconfig_mod.get_config_var = get_config_var
    sysconfig_mod.get_config_vars = get_config_vars

    distutils_mod.sysconfig = sysconfig_mod
    sys.modules.setdefault("distutils", distutils_mod)
    sys.modules.setdefault("distutils.sysconfig", sysconfig_mod)


def register_existing_dll() -> Path | None:
    if not _is_windows():
        return None
    existing = _find_local_dll()
    if existing is not None:
        _register_dll_dir(existing.parent)
    return existing


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _find_local_dll() -> Path | None:
    if not VENDOR_DIR.exists():
        return None
    for candidate in VENDOR_DIR.glob("assimp*.dll"):
        return candidate
    return None


def _pyassimp_loadable() -> bool:
    try:
        import pyassimp
        from pyassimp import _assimp_lib

        _ = _assimp_lib
        _ = pyassimp.load
        return True
    except Exception:
        return False


def _register_dll_dir(directory: Path) -> None:
    directory = directory.resolve()
    if not directory.exists():
        return
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
    add_dll = getattr(os, "add_dll_directory", None)
    if add_dll is not None:
        try:
            add_dll(str(directory))
        except (FileNotFoundError, OSError):
            pass
    _patch_pyassimp_search_dirs(directory)


def _try_register_dll_dir() -> None:
    if VENDOR_DIR.exists():
        _patch_pyassimp_search_dirs(VENDOR_DIR)


def _patch_pyassimp_search_dirs(directory: Path) -> None:
    try:
        from pyassimp import helper

        extra = str(directory)
        if hasattr(helper, "additional_dirs"):
            if extra not in helper.additional_dirs:
                helper.additional_dirs.insert(0, extra)
    except Exception:
        pass


def _download_and_extract(progress: ProgressCb) -> Path:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    url, tag = _resolve_download_url()
    _emit(progress, f"resolving assimp release: {tag}", 0, 0)

    archive_bytes = _http_get(url, progress, label=f"downloading {tag}")

    extracted = _extract_assimp_dll(archive_bytes, VENDOR_DIR)
    if extracted is None:
        raise AssimpBootstrapError(
            f"no assimp dll found inside archive ({url})"
        )

    MARKER_FILE.write_text(
        json.dumps(
            {
                "tag": tag,
                "source_url": url,
                "dll": extracted.name,
                "python": platform.python_version(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _emit(progress, f"installed: {extracted}", 1, 1)
    return extracted


def _resolve_download_url() -> tuple[str, str]:
    arch = platform.machine().lower()
    is_x86 = arch in {"x86", "i386", "i686"}

    if USE_LATEST:
        try:
            req = urllib.request.Request(
                f"{GITHUB_RELEASES_API}/latest",
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            tag = payload.get("tag_name", PINNED_TAG)
            assets = payload.get("assets", [])
            wanted = "windows-x86" if is_x86 else "windows-x64"
            for asset in assets:
                name = asset.get("name", "")
                if name.startswith(wanted) and name.endswith(".zip"):
                    return asset["browser_download_url"], tag
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass

    pinned_asset = (
        f"windows-x86-{PINNED_TAG}.zip" if is_x86 else PINNED_ASSET
    )
    pinned = (
        f"https://github.com/assimp/assimp/releases/download/{PINNED_TAG}/{pinned_asset}"
    )
    return pinned, PINNED_TAG


def _http_get(url: str, progress: ProgressCb, *, label: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            chunk_size = 1 << 15
            buffer = io.BytesIO()
            done = 0
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                buffer.write(chunk)
                done += len(chunk)
                _emit(progress, label, done, total)
            return buffer.getvalue()
    except urllib.error.URLError as exc:
        raise AssimpBootstrapError(f"download failed: {url}: {exc}") from exc


def _extract_assimp_dll(archive_bytes: bytes, dest_dir: Path) -> Path | None:
    wanted_basenames = {
        "assimp.exe",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "concrt140.dll",
        "draco.dll",
    }
    extracted_dll: Path | None = None
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = Path(info.filename).name
            base_lower = base.lower()
            is_assimp_dll = base_lower.startswith("assimp") and base_lower.endswith(".dll")
            is_wanted = base_lower in wanted_basenames or is_assimp_dll
            if not is_wanted:
                continue
            target = dest_dir / base
            try:
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except PermissionError:
                if not target.exists():
                    raise
            if is_assimp_dll and extracted_dll is None:
                extracted_dll = target
    return extracted_dll


def _emit(progress: ProgressCb, label: str, done: int, total: int) -> None:
    if progress is not None:
        try:
            progress(label, done, total)
        except Exception:
            pass


def cli_install(force: bool = False) -> int:
    def stdout_progress(label: str, done: int, total: int) -> None:
        if total > 0:
            pct = done * 100 // total
            sys.stdout.write(f"\r[{pct:3d}%] {label} ({done}/{total} bytes)   ")
        else:
            sys.stdout.write(f"\r{label}   ")
        sys.stdout.flush()
        if total > 0 and done >= total:
            sys.stdout.write("\n")

    try:
        path = ensure_assimp(progress=stdout_progress, force=force)
    except AssimpBootstrapError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    if path is None:
        print("assimp 已可用（系统已存在或非 Windows 平台）")
    else:
        print(f"assimp DLL 安装完成: {path}")
    return 0

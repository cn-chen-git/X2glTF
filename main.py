from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path


APP_NAME = "X2glTF"


def _resource_path(rel: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).resolve().parent / rel


def _launch_desktop(host: str, port: int) -> None:
    import os

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--disable-features=TranslateUI --disable-logging",
    )

    from PySide6.QtCore import QUrl, Qt
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QMainWindow

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("X2glTF")

    icon_path = _resource_path("assets/icon.ico")
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        app.setWindowIcon(icon)

    profile = QWebEngineProfile.defaultProfile()
    settings = profile.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, False)

    view = QWebEngineView()
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    view.setUrl(QUrl(f"http://{host}:{port}/"))

    window = QMainWindow()
    window.setWindowTitle(f"{APP_NAME} — DirectX .x → glTF/GLB")
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.setCentralWidget(view)
    window.resize(1180, 820)
    window.setMinimumSize(920, 640)
    window.show()

    app.exec()


def _start_server(host: str, port: int) -> tuple[str, int]:
    from backend.server import run

    return run(host=host, port=port)[:2]


def _ensure_assimp_quietly() -> None:
    from afk_x2gltf.bootstrap import (
        AssimpBootstrapError,
        ensure_assimp,
        register_existing_dll,
    )

    if register_existing_dll() is not None:
        return
    try:
        ensure_assimp()
    except AssimpBootstrapError as exc:
        print(f"警告：Assimp 自动安装失败：{exc}", file=sys.stderr)


def _build_cli_parser() -> argparse.ArgumentParser:
    from afk_x2gltf.config import (
        DEFAULT_INPUT_DIR,
        DEFAULT_OUTPUT_DIR,
        AxisUp,
        OutputFormat,
    )

    p = argparse.ArgumentParser(description="X2glTF: DirectX .x 批量转 glTF/GLB")
    p.add_argument("--cli", action="store_true", help="命令行模式（默认启动桌面 GUI）")
    p.add_argument("--serve", action="store_true", help="仅启动 HTTP 服务，不开窗口")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--install-assimp", action="store_true", help="安装 Assimp 后退出")
    p.add_argument("--reinstall-assimp", action="store_true", help="强制重装 Assimp")

    p.add_argument(
        "-i", "--input", type=Path, default=DEFAULT_INPUT_DIR,
        help=f"输入目录（默认: {DEFAULT_INPUT_DIR}）",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录（默认: {DEFAULT_OUTPUT_DIR}）",
    )
    p.add_argument(
        "-f", "--format",
        choices=[OutputFormat.GLB.value, OutputFormat.GLTF.value],
        default=OutputFormat.GLB.value,
    )
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--no-overwrite", action="store_true")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument(
        "--axis",
        choices=[AxisUp.Y_UP.value, AxisUp.Z_UP.value, AxisUp.KEEP.value],
        default=AxisUp.Y_UP.value,
    )
    p.add_argument("--flip-handedness", action="store_true", help="强制再翻转手性（默认不翻转，Assimp 的 .x 导入器已完成）")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--no-join-vertices", action="store_true")
    p.add_argument("--gen-normals", action="store_true")
    p.add_argument("--gen-smooth-normals", action="store_true")
    p.add_argument("--calc-tangent", action="store_true")
    p.add_argument("--no-triangulate", action="store_true")
    p.add_argument("--no-limit-bone-weights", action="store_true")
    p.add_argument("--keep-anim", default="")
    return p


def _run_cli(args: argparse.Namespace) -> int:
    from afk_x2gltf.config import AxisUp, ConvertConfig, OutputFormat
    from afk_x2gltf.converter import BatchConverter

    if not args.input.exists():
        print(f"error: input dir not found: {args.input}", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=True)

    cfg = ConvertConfig(
        input_dir=args.input,
        output_dir=args.output,
        output_format=OutputFormat(args.format),
        recursive=not args.no_recursive,
        overwrite=not args.no_overwrite,
        axis_up=AxisUp(args.axis),
        flip_handedness=args.flip_handedness,
        global_scale=args.scale,
        join_identical_vertices=not args.no_join_vertices,
        generate_normals=args.gen_normals,
        generate_smooth_normals=args.gen_smooth_normals,
        calc_tangent_space=args.calc_tangent,
        triangulate=not args.no_triangulate,
        limit_bone_weights=not args.no_limit_bone_weights,
        workers=args.workers,
        keep_single_animation=args.keep_anim or None,
        report_path=args.output / "_convert_report.json",
    )

    def on_progress(done: int, total: int, src: str, msg: str) -> None:
        print(f"[{done}/{total}] {Path(src).name} -> {msg}")

    results = BatchConverter(cfg).run(progress=on_progress)
    ok = sum(1 for r in results if r.ok)
    failed = len(results) - ok
    print(f"\n完成：成功 {ok}，失败 {failed}，总计 {len(results)}")
    return 0 if failed == 0 else 1


def _cli_install(force: bool) -> int:
    from afk_x2gltf.bootstrap import cli_install

    return cli_install(force=force)


def main() -> int:
    parser = _build_cli_parser()
    args = parser.parse_args()

    if args.install_assimp or args.reinstall_assimp:
        return _cli_install(force=args.reinstall_assimp)

    if args.cli:
        _ensure_assimp_quietly()
        return _run_cli(args)

    if args.serve:
        host, port = _start_server(args.host, args.port)
        print(f"serving http://{host}:{port}  (Ctrl+C 退出)")
        threading.Event().wait()
        return 0

    _ensure_assimp_quietly()
    host, port = _start_server(args.host, args.port or 0)
    _launch_desktop(host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

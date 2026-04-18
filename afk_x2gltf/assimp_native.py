from __future__ import annotations

import ctypes
import os
import sys
from ctypes import c_char_p, c_uint, c_void_p
from pathlib import Path

from afk_x2gltf.bootstrap import VENDOR_DIR, register_existing_dll


class AssimpProcess:
    CalcTangentSpace = 0x1
    JoinIdenticalVertices = 0x2
    MakeLeftHanded = 0x4
    Triangulate = 0x8
    RemoveComponent = 0x10
    GenNormals = 0x20
    GenSmoothNormals = 0x40
    SplitLargeMeshes = 0x80
    PreTransformVertices = 0x100
    LimitBoneWeights = 0x200
    ValidateDataStructure = 0x400
    ImproveCacheLocality = 0x800
    RemoveRedundantMaterials = 0x1000
    FixInfacingNormals = 0x2000
    PopulateArmatureData = 0x4000
    SortByPType = 0x8000
    FindDegenerates = 0x10000
    FindInvalidData = 0x20000
    GenUVCoords = 0x40000
    TransformUVCoords = 0x80000
    FindInstances = 0x100000
    OptimizeMeshes = 0x200000
    OptimizeGraph = 0x400000
    FlipUVs = 0x800000
    FlipWindingOrder = 0x1000000
    SplitByBoneCount = 0x2000000
    Debone = 0x4000000
    GlobalScale = 0x8000000
    EmbedTextures = 0x10000000
    ForceGenNormals = 0x20000000
    DropNormals = 0x40000000
    GenBoundingBoxes = 0x80000000

    ConvertToLeftHanded = MakeLeftHanded | FlipUVs | FlipWindingOrder


class AssimpError(RuntimeError):
    pass


class AssimpLib:
    _instance: "AssimpLib | None" = None

    def __init__(self) -> None:
        register_existing_dll()
        dll = self._load_dll()
        self._dll = dll

        dll.aiImportFile.argtypes = [c_char_p, c_uint]
        dll.aiImportFile.restype = c_void_p

        dll.aiImportFileExWithProperties.argtypes = [
            c_char_p,
            c_uint,
            c_void_p,
            c_void_p,
        ]
        dll.aiImportFileExWithProperties.restype = c_void_p

        dll.aiApplyPostProcessing.argtypes = [c_void_p, c_uint]
        dll.aiApplyPostProcessing.restype = c_void_p

        dll.aiExportScene.argtypes = [c_void_p, c_char_p, c_char_p, c_uint]
        dll.aiExportScene.restype = c_uint

        dll.aiReleaseImport.argtypes = [c_void_p]
        dll.aiReleaseImport.restype = None

        dll.aiGetErrorString.argtypes = []
        dll.aiGetErrorString.restype = c_char_p

        dll.aiGetExportFormatCount.argtypes = []
        dll.aiGetExportFormatCount.restype = ctypes.c_size_t

    @classmethod
    def instance(cls) -> "AssimpLib":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _load_dll() -> ctypes.CDLL:
        candidates: list[Path] = []
        if sys.platform.startswith("win"):
            candidates.extend(VENDOR_DIR.glob("assimp*.dll"))
        else:
            candidates.extend(VENDOR_DIR.glob("libassimp*"))

        env_lib = os.environ.get("ASSIMP_LIB")
        if env_lib:
            candidates.insert(0, Path(env_lib))

        for path in candidates:
            try:
                return ctypes.CDLL(str(path))
            except OSError:
                continue

        try:
            return ctypes.CDLL("assimp-vc143-mt.dll" if sys.platform.startswith("win") else "libassimp.so")
        except OSError as exc:
            raise AssimpError(
                "failed to load Assimp dynamic library; "
                "run: python main.py --install-assimp"
            ) from exc

    def import_file(self, path: str, flags: int = 0) -> c_void_p:
        scene = self._dll.aiImportFile(path.encode("utf-8"), c_uint(flags))
        if not scene:
            err = self._dll.aiGetErrorString() or b""
            raise AssimpError(
                f"aiImportFile failed for {path}: {err.decode('utf-8', 'replace')}"
            )
        return scene

    def export_scene(
        self,
        scene: c_void_p,
        format_id: str,
        output_path: str,
        post_flags: int = 0,
    ) -> None:
        rc = self._dll.aiExportScene(
            scene,
            format_id.encode("ascii"),
            output_path.encode("utf-8"),
            c_uint(post_flags),
        )
        if rc != 0:
            err = self._dll.aiGetErrorString() or b""
            raise AssimpError(
                f"aiExportScene({format_id}) failed for {output_path}: "
                f"code={rc} msg={err.decode('utf-8', 'replace')}"
            )

    def release(self, scene: c_void_p) -> None:
        if scene:
            self._dll.aiReleaseImport(scene)

    def convert(
        self,
        src: str,
        dst: str,
        format_id: str,
        import_flags: int = 0,
        export_flags: int = 0,
    ) -> None:
        scene = self.import_file(src, import_flags)
        try:
            self.export_scene(scene, format_id, dst, export_flags)
        finally:
            self.release(scene)

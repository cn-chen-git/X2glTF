from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "models_in"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "models_out"


class OutputFormat(StrEnum):
    GLB = "glb"
    GLTF = "gltf"


class AxisUp(StrEnum):
    Y_UP = "y_up"
    Z_UP = "z_up"
    KEEP = "keep"


@dataclass(slots=True)
class ConvertConfig:
    input_dir: Path
    output_dir: Path
    output_format: OutputFormat = OutputFormat.GLB
    recursive: bool = True
    overwrite: bool = True

    axis_up: AxisUp = AxisUp.Y_UP
    flip_handedness: bool = False
    global_scale: float = 1.0

    join_identical_vertices: bool = True
    generate_normals: bool = False
    generate_smooth_normals: bool = False
    calc_tangent_space: bool = False
    triangulate: bool = True
    limit_bone_weights: bool = True
    improve_cache_locality: bool = False

    keep_single_animation: str | None = None

    embed_textures: bool = True
    copy_textures_for_gltf: bool = True

    workers: int = 4
    report_path: Path | None = None

    def iter_inputs(self) -> list[Path]:
        if not self.input_dir.exists():
            return []
        pattern = "**/*.x" if self.recursive else "*.x"
        return sorted(p for p in self.input_dir.glob(pattern) if p.is_file())

    def resolve_output(self, src: Path) -> Path:
        rel = src.relative_to(self.input_dir).with_suffix("")
        ext = f".{self.output_format.value}"
        return (self.output_dir / rel).with_suffix(ext)

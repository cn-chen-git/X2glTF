from afk_x2gltf.bootstrap import (
    AssimpBootstrapError,
    ensure_assimp,
    install_distutils_shim,
    register_existing_dll,
)
from afk_x2gltf.config import (
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    AxisUp,
    ConvertConfig,
    OutputFormat,
)

install_distutils_shim()
register_existing_dll()

from afk_x2gltf.converter import BatchConverter, ConvertResult

__all__ = [
    "AssimpBootstrapError",
    "AxisUp",
    "BatchConverter",
    "ConvertConfig",
    "ConvertResult",
    "DEFAULT_INPUT_DIR",
    "DEFAULT_OUTPUT_DIR",
    "OutputFormat",
    "ensure_assimp",
]

from __future__ import annotations

import json
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable

from pygltflib import GLTF2

from afk_x2gltf.assimp_native import AssimpError, AssimpLib, AssimpProcess
from afk_x2gltf.config import AxisUp, ConvertConfig, OutputFormat


ProgressCallback = Callable[[int, int, str, str], None]

_ASSIMP_LOCK = Lock()


@dataclass(slots=True)
class ConvertResult:
    source: Path
    target: Path | None
    ok: bool
    message: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BatchConverter:
    config: ConvertConfig

    def run(self, progress: ProgressCallback | None = None) -> list[ConvertResult]:
        inputs = self.config.iter_inputs()
        total = len(inputs)
        results: list[ConvertResult] = []

        if total == 0:
            if progress:
                progress(0, 0, "", "no .x files found")
            return results

        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        workers = max(1, self.config.workers)
        if workers == 1:
            for i, src in enumerate(inputs, 1):
                result = self._convert_one(src, i)
                results.append(result)
                if progress:
                    progress(i, total, str(src), "ok" if result.ok else result.message)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self._convert_one, src, i): (i, src)
                    for i, src in enumerate(inputs, 1)
                }
                done = 0
                for fut in as_completed(future_map):
                    i, src = future_map[fut]
                    result = fut.result()
                    results.append(result)
                    done += 1
                    if progress:
                        progress(done, total, str(src), "ok" if result.ok else result.message)

        results.sort(key=lambda r: str(r.source))

        if self.config.report_path:
            self._write_report(results, self.config.report_path)

        return results

    def _convert_one(self, src: Path, index: int) -> ConvertResult:
        target = self.config.resolve_output(src)
        if target.exists() and not self.config.overwrite:
            return ConvertResult(src, target, True, "skipped (exists)")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            warnings: list[str] = []

            with _ASSIMP_LOCK:
                lib = AssimpLib.instance()
                import_flags = self._build_import_flags()
                format_id = (
                    "glb2" if self.config.output_format is OutputFormat.GLB else "gltf2"
                )
                lib.convert(
                    str(src),
                    str(target),
                    format_id=format_id,
                    import_flags=import_flags,
                    export_flags=0,
                )

            if not target.exists():
                return ConvertResult(src, target, False, "export produced no file")

            self._postprocess_gltf(target, src, index, warnings)

            if (
                self.config.output_format is OutputFormat.GLTF
                and self.config.copy_textures_for_gltf
            ):
                self._copy_sibling_textures(src, target.parent, warnings)

            return ConvertResult(src, target, True, "ok", warnings)

        except AssimpError as exc:
            return ConvertResult(src, target, False, str(exc))
        except Exception as exc:
            tb = traceback.format_exc(limit=3)
            return ConvertResult(src, target, False, f"{exc.__class__.__name__}: {exc}\n{tb}")

    def _build_import_flags(self) -> int:
        flags = 0
        cfg = self.config
        if cfg.triangulate:
            flags |= AssimpProcess.Triangulate
        if cfg.join_identical_vertices:
            flags |= AssimpProcess.JoinIdenticalVertices
        if cfg.generate_smooth_normals:
            flags |= AssimpProcess.GenSmoothNormals
        elif cfg.generate_normals:
            flags |= AssimpProcess.GenNormals
        if cfg.calc_tangent_space:
            flags |= AssimpProcess.CalcTangentSpace
        if cfg.limit_bone_weights:
            flags |= AssimpProcess.LimitBoneWeights
        if cfg.improve_cache_locality:
            flags |= AssimpProcess.ImproveCacheLocality
        if cfg.flip_handedness:
            flags |= AssimpProcess.ConvertToLeftHanded
        if cfg.embed_textures:
            flags |= AssimpProcess.EmbedTextures
        flags |= AssimpProcess.SortByPType
        flags |= AssimpProcess.ValidateDataStructure
        flags |= AssimpProcess.PopulateArmatureData
        return flags

    def _postprocess_gltf(
        self,
        target: Path,
        src: Path,
        index: int,
        warnings: list[str],
    ) -> None:
        cfg = self.config
        is_glb = target.suffix.lower() == ".glb"
        needs_embed = cfg.embed_textures and is_glb and self._has_external_images(target)
        needs_anim_fix = self._has_bad_animation_timestamps(target)
        needs_alpha_strip = self._has_opaque_rgba_texture(target)
        needs_skin_expand = self._has_unlisted_animated_bones(target)
        needs_post = (
            cfg.axis_up is AxisUp.Z_UP
            or cfg.global_scale != 1.0
            or cfg.keep_single_animation
            or needs_embed
            or needs_anim_fix
            or needs_alpha_strip
            or needs_skin_expand
        )
        if not needs_post:
            return

        gltf = GLTF2().load(str(target))
        if gltf is None:
            warnings.append("post-process skipped: gltf load failed")
            return

        if needs_anim_fix:
            self._fix_animation_data(gltf, warnings)

        if needs_skin_expand:
            self._expand_skin_joints(gltf, warnings)

        self._strip_opaque_texture_alpha(gltf, warnings)

        if cfg.keep_single_animation and gltf.animations:
            target_name = cfg.keep_single_animation
            kept = [a for a in gltf.animations if a.name == target_name]
            if kept:
                gltf.animations = kept
            else:
                warnings.append(f"animation '{target_name}' not found, kept all")

        if cfg.global_scale != 1.0 or cfg.axis_up is AxisUp.Z_UP:
            self._wrap_root_transform(gltf, cfg)

        if needs_embed:
            self._embed_external_images(gltf, src, warnings)

        if is_glb:
            gltf.save_binary(str(target))
        else:
            gltf.save_json(str(target))

    @staticmethod
    def _has_bad_animation_timestamps(target: Path) -> bool:
        import math
        import struct

        try:
            gltf = GLTF2().load(str(target))
        except Exception:
            return False
        if not gltf or not gltf.animations:
            return False
        blob = gltf.binary_blob() or b""
        for anim in gltf.animations:
            for s in anim.samplers:
                acc = gltf.accessors[s.input]
                if acc.componentType != 5126 or acc.type != "SCALAR":
                    continue
                bv = gltf.bufferViews[acc.bufferView]
                start = bv.byteOffset + (acc.byteOffset or 0)
                probe = min(acc.count, 4)
                for i in range(probe):
                    (v,) = struct.unpack_from("<f", blob, start + i * 4)
                    if not math.isfinite(v):
                        return True
        return False

    @staticmethod
    def _fix_animation_data(gltf: GLTF2, warnings: list[str]) -> None:
        import math
        import struct

        from pygltflib import Accessor, BufferView

        if not gltf.animations:
            return

        skinned_nodes = {n for n in range(len(gltf.nodes)) if gltf.nodes[n].skin is not None}

        fps = 30.0
        blob = bytearray(gltf.binary_blob() or b"")

        time_cache: dict[tuple[int, float], int] = {}

        def _make_time_accessor(count: int) -> int:
            key = (count, fps)
            if key in time_cache:
                return time_cache[key]
            data = bytearray()
            for i in range(count):
                data += struct.pack("<f", i / fps)
            pad = (4 - len(blob) % 4) % 4
            if pad:
                blob.extend(b"\x00" * pad)
            offset = len(blob)
            blob.extend(data)
            bv = BufferView(buffer=0, byteOffset=offset, byteLength=len(data))
            gltf.bufferViews.append(bv)
            bv_idx = len(gltf.bufferViews) - 1
            acc = Accessor(
                bufferView=bv_idx,
                componentType=5126,
                count=count,
                type="SCALAR",
                min=[0.0],
                max=[(count - 1) / fps if count > 1 else 0.0],
            )
            gltf.accessors.append(acc)
            acc_idx = len(gltf.accessors) - 1
            time_cache[key] = acc_idx
            return acc_idx

        def _is_sampler_bad(s) -> bool:
            acc = gltf.accessors[s.input]
            if acc.componentType != 5126 or acc.type != "SCALAR":
                return False
            bv = gltf.bufferViews[acc.bufferView]
            start = bv.byteOffset + (acc.byteOffset or 0)
            probe = min(acc.count, 4)
            for i in range(probe):
                (v,) = struct.unpack_from("<f", bytes(blob), start + i * 4)
                if not math.isfinite(v):
                    return True
            return False

        dropped_skinned = 0
        fixed_samplers = 0
        for anim in gltf.animations:
            kept_channels = []
            for ch in anim.channels:
                if ch.target.node in skinned_nodes:
                    dropped_skinned += 1
                    continue
                kept_channels.append(ch)
            anim.channels = kept_channels

            for s in anim.samplers:
                if _is_sampler_bad(s):
                    count = gltf.accessors[s.input].count
                    s.input = _make_time_accessor(count)
                    fixed_samplers += 1

        existing_buf = gltf.buffers[0] if gltf.buffers else None
        if existing_buf is not None:
            existing_buf.byteLength = len(blob)
        gltf.set_binary_blob(bytes(blob))

        if dropped_skinned:
            warnings.append(
                f"dropped {dropped_skinned} invalid TRS channels targeting skinned meshes"
            )
        if fixed_samplers:
            warnings.append(
                f"regenerated {fixed_samplers} animation timestamp accessor(s) @{fps:.0f}fps (NaN detected)"
            )

    @staticmethod
    def _has_unlisted_animated_bones(target: Path) -> bool:
        try:
            gltf = GLTF2().load(str(target))
        except Exception:
            return False
        if not gltf or not gltf.skins or not gltf.animations:
            return False
        skin_mesh_nodes = {i for i, n in enumerate(gltf.nodes) if n.skin is not None}
        animated: set[int] = set()
        for anim in gltf.animations:
            for ch in anim.channels:
                if ch.target.node is not None and ch.target.node not in skin_mesh_nodes:
                    animated.add(ch.target.node)
        for skin in gltf.skins:
            joint_set = set(skin.joints)
            if animated - joint_set:
                return True
        return False

    @staticmethod
    def _expand_skin_joints(gltf: GLTF2, warnings: list[str]) -> None:
        import struct

        import numpy as np
        from pygltflib import Accessor, BufferView

        if not gltf.skins or not gltf.animations:
            return

        parent: dict[int, int] = {}
        for i, n in enumerate(gltf.nodes):
            for c in n.children or []:
                parent[c] = i

        skin_mesh_nodes = {i for i, n in enumerate(gltf.nodes) if n.skin is not None}

        def has_skin_mesh_child(n_idx: int) -> bool:
            return any(c in skin_mesh_nodes for c in (gltf.nodes[n_idx].children or []))

        def walk_up_until_boundary(j: int) -> int:
            cur = j
            while True:
                p = parent.get(cur)
                if p is None or p in skin_mesh_nodes or has_skin_mesh_child(p):
                    return cur
                cur = p

        animated_bones: set[int] = set()
        for anim in gltf.animations:
            for ch in anim.channels:
                if ch.target.node is not None and ch.target.node not in skin_mesh_nodes:
                    animated_bones.add(ch.target.node)

        def local_matrix(node) -> np.ndarray:
            if node.matrix:
                return np.array(node.matrix, dtype=np.float64).reshape(4, 4).T
            t = np.eye(4, dtype=np.float64)
            if node.translation:
                t[:3, 3] = node.translation
            r = np.eye(4, dtype=np.float64)
            if node.rotation:
                x, y, z, w = node.rotation
                r[:3, :3] = [
                    [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                    [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                    [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
                ]
            s = np.eye(4, dtype=np.float64)
            if node.scale:
                s[0, 0], s[1, 1], s[2, 2] = node.scale
            return t @ r @ s

        def world_matrix(idx: int) -> np.ndarray:
            m = local_matrix(gltf.nodes[idx])
            cur = parent.get(idx)
            while cur is not None:
                m = local_matrix(gltf.nodes[cur]) @ m
                cur = parent.get(cur)
            return m

        blob = bytearray(gltf.binary_blob() or b"")
        total_added = 0

        for skin_idx, skin in enumerate(gltf.skins):
            existing_joints = list(skin.joints)
            existing_set = set(existing_joints)

            roots: set[int] = {walk_up_until_boundary(j) for j in existing_joints}
            skeleton_root = next(iter(roots)) if len(roots) == 1 else None

            needed = set(existing_joints)
            for j in existing_joints:
                cur = parent.get(j)
                while cur is not None and cur != parent.get(skeleton_root) and cur not in skin_mesh_nodes:
                    needed.add(cur)
                    if cur == skeleton_root:
                        break
                    cur = parent.get(cur)
            needed |= animated_bones - skin_mesh_nodes

            if skeleton_root is not None:
                descendants: set[int] = set()
                stack = [skeleton_root]
                while stack:
                    n = stack.pop()
                    descendants.add(n)
                    stack.extend(gltf.nodes[n].children or [])
                needed &= descendants

            new_bones = sorted(n for n in needed if n not in existing_set)
            if not new_bones:
                if skeleton_root is not None and skin.skeleton is None:
                    skin.skeleton = skeleton_root
                continue

            existing_ibms: list[tuple[float, ...]] = []
            if skin.inverseBindMatrices is not None:
                acc = gltf.accessors[skin.inverseBindMatrices]
                bv = gltf.bufferViews[acc.bufferView]
                start = bv.byteOffset + (acc.byteOffset or 0)
                for i in range(acc.count):
                    existing_ibms.append(struct.unpack_from("<16f", bytes(blob), start + i * 64))
            else:
                for j in existing_joints:
                    try:
                        ibm = np.linalg.inv(world_matrix(j))
                    except np.linalg.LinAlgError:
                        ibm = np.eye(4, dtype=np.float64)
                    existing_ibms.append(tuple(ibm.T.flatten()))

            new_ibms: list[tuple[float, ...]] = []
            for nb in new_bones:
                try:
                    ibm = np.linalg.inv(world_matrix(nb))
                except np.linalg.LinAlgError:
                    ibm = np.eye(4, dtype=np.float64)
                new_ibms.append(tuple(ibm.T.flatten()))

            buf_data = bytearray()
            for m in existing_ibms:
                for v in m:
                    buf_data += struct.pack("<f", v)
            for m in new_ibms:
                for v in m:
                    buf_data += struct.pack("<f", v)

            pad = (4 - len(blob) % 4) % 4
            if pad:
                blob.extend(b"\x00" * pad)
            offset = len(blob)
            blob.extend(buf_data)

            bv = BufferView(buffer=0, byteOffset=offset, byteLength=len(buf_data))
            gltf.bufferViews.append(bv)
            acc = Accessor(
                bufferView=len(gltf.bufferViews) - 1,
                componentType=5126,
                count=len(existing_joints) + len(new_bones),
                type="MAT4",
            )
            gltf.accessors.append(acc)

            skin.joints = existing_joints + new_bones
            skin.inverseBindMatrices = len(gltf.accessors) - 1
            if skeleton_root is not None and skin.skeleton is None:
                skin.skeleton = skeleton_root

            total_added += len(new_bones)

        if total_added:
            if gltf.buffers:
                gltf.buffers[0].byteLength = len(blob)
            gltf.set_binary_blob(bytes(blob))
            warnings.append(f"expanded skin joints: +{total_added} animated bone(s)")

    @staticmethod
    def _has_external_images(target: Path) -> bool:
        try:
            gltf = GLTF2().load(str(target))
        except Exception:
            return False
        if not gltf or not gltf.images:
            return False
        for img in gltf.images:
            if img.uri and img.bufferView is None:
                return True
        return False

    @staticmethod
    def _collect_opaque_base_textures(gltf: GLTF2) -> list[tuple[int, "object"]]:
        found: list[tuple[int, object]] = []
        if not gltf.images or not gltf.materials:
            return found
        for mat in gltf.materials:
            alpha_mode = (mat.alphaMode or "OPAQUE").upper()
            if alpha_mode != "OPAQUE":
                continue
            pbr = mat.pbrMetallicRoughness
            if pbr and pbr.baseColorTexture is not None:
                tex_idx = pbr.baseColorTexture.index
                if tex_idx is not None and 0 <= tex_idx < len(gltf.textures):
                    src = gltf.textures[tex_idx].source
                    if src is not None:
                        found.append((src, mat))
        return found

    @staticmethod
    def _has_opaque_rgba_texture(target: Path) -> bool:
        try:
            from io import BytesIO
            from PIL import Image as PILImage

            gltf = GLTF2().load(str(target))
        except Exception:
            return False
        if not gltf:
            return False
        opaque_refs = BatchConverter._collect_opaque_base_textures(gltf)
        if not opaque_refs:
            return False

        blob = gltf.binary_blob() or b""
        seen: set[int] = set()
        for i, _ in opaque_refs:
            if i in seen:
                continue
            seen.add(i)
            img = gltf.images[i]
            if img.bufferView is None:
                continue
            bv = gltf.bufferViews[img.bufferView]
            data = bytes(blob[bv.byteOffset : bv.byteOffset + bv.byteLength])
            try:
                with PILImage.open(BytesIO(data)) as im:
                    if im.mode in ("RGBA", "LA") or "A" in im.mode:
                        return True
            except Exception:
                continue
        return False

    @staticmethod
    def _classify_alpha(alpha_img) -> str:
        hist = alpha_img.histogram()
        total = sum(hist) or 1
        fully_transparent = sum(hist[0:16])
        fully_opaque = sum(hist[240:256])
        partial = total - fully_transparent - fully_opaque

        if fully_opaque / total >= 0.99 and fully_transparent / total <= 0.001:
            return "strip"
        if partial / total >= 0.03:
            return "blend"
        return "mask"

    @staticmethod
    def _strip_opaque_texture_alpha(gltf: GLTF2, warnings: list[str]) -> None:
        from io import BytesIO
        from PIL import Image as PILImage

        from pygltflib import BufferView

        opaque_refs = BatchConverter._collect_opaque_base_textures(gltf)
        if not opaque_refs:
            return

        blob = bytearray(gltf.binary_blob() or b"")
        stats = {"strip": 0, "mask": 0, "blend": 0}
        processed_imgs: dict[int, tuple[str, int]] = {}

        for img_idx, mat in opaque_refs:
            if img_idx in processed_imgs:
                decision, new_bv = processed_imgs[img_idx]
            else:
                img = gltf.images[img_idx]
                if img.bufferView is None:
                    continue
                bv = gltf.bufferViews[img.bufferView]
                data = bytes(blob[bv.byteOffset : bv.byteOffset + bv.byteLength])
                try:
                    with PILImage.open(BytesIO(data)) as im:
                        mode = im.mode
                        if mode not in ("RGBA", "LA") and "A" not in mode:
                            continue
                        rgba = im.convert("RGBA")
                        decision = BatchConverter._classify_alpha(rgba.getchannel("A"))
                        if decision == "strip":
                            rgb = rgba.convert("RGB")
                            out = BytesIO()
                            rgb.save(out, format="PNG", optimize=True)
                            new_data = out.getvalue()
                        else:
                            out = BytesIO()
                            rgba.save(out, format="PNG", optimize=True)
                            new_data = out.getvalue()
                except Exception as exc:
                    warnings.append(f"alpha handling failed on image[{img_idx}]: {exc}")
                    continue

                pad = (4 - len(blob) % 4) % 4
                if pad:
                    blob.extend(b"\x00" * pad)
                offset = len(blob)
                blob.extend(new_data)
                new_bv = BufferView(
                    buffer=0, byteOffset=offset, byteLength=len(new_data)
                )
                gltf.bufferViews.append(new_bv)
                new_bv_idx = len(gltf.bufferViews) - 1
                img.bufferView = new_bv_idx
                img.mimeType = "image/png"
                processed_imgs[img_idx] = (decision, new_bv_idx)
                stats[decision] += 1

            match decision:
                case "mask":
                    mat.alphaMode = "MASK"
                    if mat.alphaCutoff is None:
                        mat.alphaCutoff = 0.5
                case "blend":
                    mat.alphaMode = "BLEND"
                case _:
                    pass

        if gltf.buffers:
            gltf.buffers[0].byteLength = len(blob)
        gltf.set_binary_blob(bytes(blob))

        summary = [f"{v} {k}" for k, v in stats.items() if v]
        if summary:
            warnings.append(f"texture alpha handling: {', '.join(summary)}")

    @staticmethod
    def _embed_external_images(gltf: GLTF2, src: Path, warnings: list[str]) -> None:
        from pygltflib import BufferView

        tex_exts = [".png", ".jpg", ".jpeg", ".bmp", ".tga", ".dds", ".tif", ".tiff"]
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".bmp": "image/bmp",
            ".tga": "image/x-tga",
            ".dds": "image/vnd-ms.dds",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }

        src_dir = src.parent
        siblings: dict[str, Path] = {}
        for p in src_dir.iterdir():
            if p.is_file() and p.suffix.lower() in tex_exts:
                siblings[p.name.lower()] = p
                siblings.setdefault(p.stem.lower(), p)

        for img in gltf.images or []:
            if not img.uri or img.bufferView is not None:
                continue
            wanted = img.uri.replace("\\", "/").split("/")[-1]
            candidate: Path | None = siblings.get(wanted.lower())
            if candidate is None:
                stem = Path(wanted).stem.lower()
                candidate = siblings.get(stem)
                if candidate is None:
                    for ext in tex_exts:
                        p = src_dir / f"{stem}{ext}"
                        if p.exists():
                            candidate = p
                            break
            if candidate is None or not candidate.exists():
                warnings.append(f"texture not found for embedding: {img.uri}")
                continue
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                warnings.append(f"read texture failed: {candidate.name}: {exc}")
                continue

            ext = candidate.suffix.lower()
            if ext == ".tga":
                warnings.append(f"TGA not broadly supported by glTF viewers: {candidate.name}")
            if ext == ".bmp":
                try:
                    from PIL import Image as PILImage
                    from io import BytesIO

                    with PILImage.open(BytesIO(data)) as im:
                        buf = BytesIO()
                        im.convert("RGBA").save(buf, format="PNG")
                        data = buf.getvalue()
                        ext = ".png"
                except Exception as exc:
                    warnings.append(f"BMP->PNG failed, keeping raw: {candidate.name}: {exc}")

            mime = mime_map.get(ext, "image/png")

            buf_index = 0
            if gltf.buffers:
                existing = gltf.buffers[buf_index]
                existing_bytes = gltf.binary_blob() or b""
                offset = len(existing_bytes)
                new_blob = existing_bytes + data
                pad = (4 - len(new_blob) % 4) % 4
                if pad:
                    new_blob += b"\x00" * pad
                gltf.set_binary_blob(new_blob)
                existing.byteLength = len(new_blob)
            else:
                from pygltflib import Buffer

                gltf.buffers = [Buffer(byteLength=len(data))]
                gltf.set_binary_blob(data)
                offset = 0

            bv = BufferView(
                buffer=buf_index,
                byteOffset=offset,
                byteLength=len(data),
            )
            gltf.bufferViews.append(bv)
            img.bufferView = len(gltf.bufferViews) - 1
            img.mimeType = mime
            img.uri = None
            img.name = img.name or candidate.stem

    @staticmethod
    def _wrap_root_transform(gltf: GLTF2, cfg: ConvertConfig) -> None:
        from pygltflib import Node

        if not gltf.scenes:
            return
        scene = gltf.scenes[gltf.scene or 0]
        if not scene.nodes:
            return

        scale = [cfg.global_scale] * 3
        rotation = None
        if cfg.axis_up is AxisUp.Z_UP:
            rotation = [-0.7071068, 0.0, 0.0, 0.7071068]

        wrapper = Node(
            name="X2glTF_Root",
            children=list(scene.nodes),
            scale=scale if cfg.global_scale != 1.0 else None,
            rotation=rotation,
        )
        gltf.nodes.append(wrapper)
        scene.nodes = [len(gltf.nodes) - 1]

    @staticmethod
    def _copy_sibling_textures(src: Path, out_dir: Path, warnings: list[str]) -> None:
        exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".dds", ".tif", ".tiff"}
        for sibling in src.parent.iterdir():
            if sibling.is_file() and sibling.suffix.lower() in exts:
                dst = out_dir / sibling.name
                if dst.exists():
                    continue
                try:
                    shutil.copy2(sibling, dst)
                except OSError as exc:
                    warnings.append(f"copy texture failed: {sibling.name}: {exc}")

    @staticmethod
    def _write_report(results: list[ConvertResult], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "total": len(results),
            "ok": sum(1 for r in results if r.ok),
            "failed": sum(1 for r in results if not r.ok),
            "items": [
                {
                    "source": str(r.source),
                    "target": str(r.target) if r.target else None,
                    "ok": r.ok,
                    "message": r.message,
                    "warnings": r.warnings,
                }
                for r in results
            ],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

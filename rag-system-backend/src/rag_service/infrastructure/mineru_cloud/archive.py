"""将官网 ZIP 包装成 RAGFlow 可读取的目录，不重写识别内容和坐标。

所有操作在内存中完成，不把不可信 ZIP 解压到文件系统；仅调整内容列表的文件名。
"""

import json
import stat
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from rag_service.infrastructure.mineru_cloud.client import MinerUError


def safe_path(name: str) -> PurePosixPath:
    """拒绝绝对路径、父目录和跨平台路径，保护后续 RAGFlow 解压与图片读取。"""
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise MinerUError("MINERU_UNSAFE_ARCHIVE_PATH")
    return path


def compatible_zip(raw: bytes) -> bytes:
    """保留单文档目录中的所有文件字节；拒绝缺失图片、歧义列表和过大的压缩包。"""
    try:
        with ZipFile(BytesIO(raw)) as source:
            infos = source.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > 256 * 1024 * 1024:
                raise MinerUError("MINERU_ARCHIVE_TOO_LARGE")
            names: set[str] = set()
            for info in infos:
                safe_path(info.filename)
                if info.filename in names or stat.S_ISLNK(info.external_attr >> 16):
                    raise MinerUError("MINERU_UNSAFE_ARCHIVE_PATH")
                if info.flag_bits & 1:
                    raise MinerUError("MINERU_ENCRYPTED_ARCHIVE")
                names.add(info.filename)
            candidates = [
                i for i in infos if not i.is_dir()
                and (i.filename.endswith("_content_list.json")
                     or PurePosixPath(i.filename).name == "content_list.json")
            ]
            if len(candidates) != 1:
                raise MinerUError("MINERU_CONTENT_LIST_MISSING_OR_AMBIGUOUS")
            content = candidates[0]
            root = PurePosixPath(content.filename).parent
            records = json.loads(source.read(content))
            if not isinstance(records, list) or not records:
                raise MinerUError("MINERU_EMPTY_CONTENT")
            for record in records:
                if not isinstance(record, dict):
                    raise MinerUError("MINERU_INVALID_CONTENT")
                for field in ("img_path", "table_img_path", "equation_img_path"):
                    name = record.get(field)
                    if name:
                        if not isinstance(name, str) or str(root / safe_path(name)) not in names:
                            raise MinerUError("MINERU_IMAGE_MISSING")
            output = BytesIO()
            written: set[str] = set()
            with ZipFile(output, "w") as target:
                for info in infos:
                    if info.is_dir():
                        continue
                    path = PurePosixPath(info.filename)
                    # 官方单文档 ZIP 的内容列表与 images 目录位于同一根目录。
                    # 不丢弃其他目录；不兼容的结构明确报错，避免静默漏掉图片。
                    if not path.is_relative_to(root):
                        raise MinerUError("MINERU_INCOMPATIBLE_ARCHIVE_LAYOUT")
                    name = str(path.relative_to(root))
                    if info == content:
                        name = "content_list.json"
                    if name in written:
                        raise MinerUError("MINERU_UNSAFE_ARCHIVE_PATH")
                    written.add(name)
                    target.writestr(name, source.read(info))
            return output.getvalue()
    except (BadZipFile, ValueError, UnicodeError, RuntimeError):
        raise MinerUError("MINERU_INVALID_ARCHIVE") from None

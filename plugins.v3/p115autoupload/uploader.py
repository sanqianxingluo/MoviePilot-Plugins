"""115 网盘上传核心：路径映射、远端查重与文件上传。"""

from __future__ import annotations

import os
import posixpath
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.sdk.logging import logger

# 视频文件后缀，用于影片级查重判断
VIDEO_SUFFIXES = {
    ".mkv",
    ".mp4",
    ".ts",
    ".m2ts",
    ".iso",
    ".avi",
    ".wmv",
    ".mov",
    ".flv",
    ".rmvb",
}

# 下载或临时文件后缀，不参与上传
SKIP_SUFFIXES = {".part", ".tmp", ".!qb", ".downloading"}

# 远端目录列表缓存的分页大小
PAGE_SIZE = 1000

# 上传失败重试次数
RETRY = 3


class P115PeerCache:
    """按目录缓存 115 网盘的子目录与文件清单。"""

    def __init__(self, client: Any) -> None:
        """保存 115 客户端并初始化目录缓存。"""
        self.client = client
        self.dirs: Dict[int, Dict[str, int]] = {}
        self.files: Dict[int, Dict[str, int]] = {}

    def _load(self, cid: int) -> None:
        """读取指定目录的全部子项并写入缓存。"""
        dirs: Dict[str, int] = {}
        files: Dict[str, int] = {}
        offset = 0
        while True:
            resp = self.client.fs_files(
                {"cid": cid, "limit": PAGE_SIZE, "offset": offset, "show_dir": 1}
            )
            data = resp.get("data") or []
            for item in data:
                name = item.get("n") or ""
                if not name:
                    continue
                if item.get("fid"):
                    files[name] = int(item.get("s") or 0)
                elif item.get("cid"):
                    dirs[name] = int(item["cid"])
            if len(data) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            time.sleep(0.15)
        self.dirs[cid] = dirs
        self.files[cid] = files

    def invalidate(self, cid: int) -> None:
        """丢弃指定目录的缓存，使下次读取重新拉取。"""
        self.dirs.pop(cid, None)
        self.files.pop(cid, None)

    def dirs_of(self, cid: int) -> Dict[str, int]:
        """返回目录下的子目录映射。"""
        if cid not in self.dirs:
            self._load(cid)
        return self.dirs[cid]

    def files_of(self, cid: int) -> Dict[str, int]:
        """返回目录下的文件映射（文件名到大小）。"""
        if cid not in self.files:
            self._load(cid)
        return self.files[cid]

    def find(self, parts: List[str]) -> Optional[int]:
        """解析远端目录路径，不存在时返回 None。"""
        pid = 0
        for part in parts:
            did = self.dirs_of(pid).get(part)
            if did is None:
                return None
            pid = did
        return pid

    def ensure(self, parts: List[str]) -> int:
        """确保远端目录存在并返回目录 ID。"""
        pid = 0
        for part in parts:
            did = self.dirs_of(pid).get(part)
            if did is None:
                resp = self.client.fs_mkdir(part, pid=pid)
                self.invalidate(pid)
                did = self.dirs_of(pid).get(part)
                if did is None:
                    # 目录可能刚被创建但列表尚未刷新，重试一次
                    self.invalidate(pid)
                    did = self.dirs_of(pid).get(part)
                if did is None:
                    raise RuntimeError(
                        f"无法创建远端目录 {part}: {str(resp)[:200]}"
                    )
            pid = did
        return pid


class P115Uploader:
    """把本地媒体库文件按配置映射镜像上传到 115 网盘。"""

    def __init__(
        self,
        cookie: str,
        mappings: str,
        dedup: bool = True,
        include_sidecar: bool = True,
    ) -> None:
        """保存配置并延迟创建 115 客户端。"""
        self.cookie = cookie
        self.mappings = self._parse_mappings(mappings)
        self.dedup = dedup
        self.include_sidecar = include_sidecar
        self.client: Any = None
        self.cache: Optional[P115PeerCache] = None

    @staticmethod
    def _parse_mappings(raw: str) -> List[Tuple[str, str]]:
        """解析“本地前缀 => 115 前缀”映射，按前缀长度降序排列。"""
        result: List[Tuple[str, str]] = []
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" in line:
                local, remote = line.split("=>", 1)
            elif "->" in line:
                local, remote = line.split("->", 1)
            else:
                continue
            local = os.path.normpath(local.strip())
            remote = "/" + remote.strip().strip("/")
            if local and remote and remote != "/":
                result.append((local, remote))
        result.sort(key=lambda item: len(item[0]), reverse=True)
        return result

    def _remote_path(self, local_path: str) -> Optional[str]:
        """把本地路径转换为 115 路径，未命中映射时返回 None。"""
        path = os.path.normpath(local_path)
        for local_prefix, remote_prefix in self.mappings:
            if path == local_prefix or path.startswith(local_prefix + os.sep):
                rel = os.path.relpath(path, local_prefix).replace(os.sep, "/")
                return posixpath.join(remote_prefix, rel) if rel != "." else remote_prefix
        return None

    def _ensure_client(self) -> None:
        """按需创建 115 客户端与目录缓存。"""
        if self.client is not None and self.cache is not None:
            return
        from p115client import P115Client

        self.client = P115Client(self.cookie)
        self.cache = P115PeerCache(self.client)

    def _collect_files(self, path: str) -> Iterable[str]:
        """展开文件或目录为待上传文件列表。"""
        if os.path.isfile(path):
            yield path
            return
        if not os.path.isdir(path):
            return
        for root, dirs, names in os.walk(path):
            dirs.sort()
            names.sort()
            for name in names:
                suffix = os.path.splitext(name)[1].lower()
                if suffix in SKIP_SUFFIXES:
                    continue
                if not self.include_sidecar and suffix not in VIDEO_SUFFIXES:
                    continue
                yield os.path.join(root, name)

    def _upload_file(self, local_path: str, cid: int, name: str) -> Tuple[bool, str]:
        """上传单个文件，返回是否成功及错误信息。"""
        last_error = ""
        for attempt in range(1, RETRY + 1):
            try:
                resp = self.client.upload_file(local_path, pid=cid, filename=name)
                if isinstance(resp, dict) and (
                    resp.get("state") or resp.get("code") in (0, "0")
                ):
                    return True, ""
                last_error = str(resp)[:300]
            except Exception as err:  # noqa: BLE001
                last_error = f"{type(err).__name__}: {err}"
            time.sleep(3 * attempt)
        return False, last_error

    def upload_paths(self, paths: Iterable[str]) -> Dict[str, Any]:
        """上传给定本地路径对应的文件并返回结果摘要。"""
        self._ensure_client()
        assert self.cache is not None

        files: List[str] = []
        ignored = 0
        for path in paths:
            if not path:
                continue
            for item in self._collect_files(path):
                if self._remote_path(item) is None:
                    ignored += 1
                    continue
                files.append(item)

        report: Dict[str, Any] = {
            "scanned": len(files),
            "ignored": ignored,
            "uploaded": 0,
            "uploaded_gib": 0.0,
            "skipped": 0,
            "duplicates": [],
            "failed": [],
        }
        if not files:
            return report

        groups: Dict[str, List[Tuple[str, str]]] = {}
        for item in files:
            remote = self._remote_path(item)
            assert remote is not None
            groups.setdefault(posixpath.dirname(remote), []).append(
                (item, posixpath.basename(remote))
            )

        uploaded_bytes = 0
        duplicates: List[Dict[str, str]] = report["duplicates"]
        failures: List[Dict[str, str]] = report["failed"]

        for remote_dir, items in sorted(groups.items()):
            parts = [part for part in remote_dir.split("/") if part]
            try:
                cid = self.cache.ensure(parts)
            except Exception as err:  # noqa: BLE001
                logger.error(f"115自动上传：创建目录失败 {remote_dir}：{err}")
                failures.append({"path": remote_dir, "error": str(err)})
                report["failed"] = failures
                continue

            remote_files = dict(self.cache.files_of(cid))
            remote_videos = {
                name: size
                for name, size in remote_files.items()
                if os.path.splitext(name)[1].lower() in VIDEO_SUFFIXES
            }
            local_videos = [
                (name, os.path.getsize(local))
                for local, name in items
                if os.path.splitext(name)[1].lower() in VIDEO_SUFFIXES
                and os.path.exists(local)
            ]
            matched = [
                name
                for name, size in local_videos
                if remote_videos.get(name) is not None
                and abs(int(remote_videos[name]) - size) <= 1024
            ]

            if self.dedup and local_videos:
                if len(matched) == len(local_videos):
                    duplicates.append(
                        {"dir": remote_dir, "reason": "115 已存在全部视频文件"}
                    )
                    report["skipped"] += len(items)
                    continue
                if len(local_videos) == 1 and remote_videos:
                    duplicates.append(
                        {"dir": remote_dir, "reason": "115 已存在同名影片（版本不同）"}
                    )
                    report["skipped"] += len(items)
                    continue

            for local_path, name in items:
                try:
                    size = os.path.getsize(local_path)
                except OSError:
                    failures.append({"path": local_path, "error": "读取文件失败"})
                    continue
                existing = remote_files.get(name)
                if existing is not None and abs(int(existing) - size) <= 1024:
                    report["skipped"] += 1
                    continue
                ok, error = self._upload_file(local_path, cid, name)
                if ok:
                    report["uploaded"] += 1
                    uploaded_bytes += size
                    logger.info(f"115自动上传：已上传 {remote_dir}/{name}")
                else:
                    failures.append({"path": f"{remote_dir}/{name}", "error": error})
                    logger.error(f"115自动上传：上传失败 {remote_dir}/{name}：{error}")
            self.cache.invalidate(cid)

        report["failed"] = failures
        report["duplicates"] = duplicates
        report["uploaded_gib"] = round(uploaded_bytes / 1024**3, 2)
        return report

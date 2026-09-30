"""
MoviePilot 软链接监控插件 (SymlinkMonitor)

监控下载目录中的文件变化：
  1. 新增文件 -> 在媒体库目录建立软链接（符号链接，可跨文件系统）；
  2. 源文件被删除 -> 延迟若干秒确认后，联动清理：
       · 对应的软链接
       · 同名的刮削文件（nfo / 图片 / 字幕）与刮削目录
       · MoviePilot 转移记录
       · 下载器中的任务（种子，不删除数据）
       · 空目录

配置项：
  enabled            启用插件
  notify             发送通知
  onlyonce           立即运行一次（全量扫描一次）
  monitor_dirs       监控目录，每行一条，格式「源目录」或「源目录:软链接目标目录」
  exclude_dirs       不删除目录，每行一条；这些目录下的文件不建链、也永不删除
  exclude_keywords   排除关键词，每行一条，命中则忽略
  scan_interval      扫描间隔（秒）
  delayed_deletion   启用延迟删除
  delay_seconds      延迟删除时间（秒）
  delete_scrap       联动清理刮削文件
  delete_history     联动删除转移记录
  delete_torrents    联动删除下载种子
  clean_empty_dir    联动清理空目录

兼容说明：本插件使用 MoviePilot 公开的宿主接口（app.plugins / app.log /
app.core.event / app.schemas.types / app.db.transferhistory_oper）。
日志入口在 V3 上优先取 app.sdk.logging，缺失时回退 app.log，因此同一份
实现可同时运行在 V2.x 与 V3 宿主上。
"""

import os
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.db.transferhistory_oper import TransferHistoryOper
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType

try:  # V3 稳定 SDK
    from app.sdk.logging import logger
except Exception:  # V2 及更早
    from app.log import logger

try:
    from app.core.config import settings
except Exception:  # pragma: no cover - 极端兜底
    settings = None


# 下载器尚未完成的临时文件后缀
TMP_SUFFIXES = {".!qb", ".part", ".mp", ".tmp", ".temp", ".downloading"}

# 刮削文件后缀（元数据 / 图片 / 字幕）
SCRAP_EXTENSIONS = {
    ".nfo", ".xml",
    ".jpg", ".jpeg", ".png", ".webp", ".tbn", ".gif", ".bmp",
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".pgs", ".smi", ".rt", ".sbv",
}

# 媒体服务器生成的关联刮削目录后缀
SCRAP_DIR_SUFFIXES = {".trickplay", ".actors", ".thumbs"}

_state_lock = threading.Lock()
_queue_lock = threading.Lock()


class SymlinkMonitor(_PluginBase):
    # 插件名称
    plugin_name = "软链接监控"
    # 插件描述
    plugin_desc = "监控下载目录并建立软链接；源文件删除后延迟联动清理软链接、刮削文件、转移记录与下载种子。"
    # 插件图标
    plugin_icon = "Linkace_C.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "sanqianxingluo"
    # 作者主页
    author_url = "https://github.com/sanqianxingluo"
    # 插件配置项ID前缀
    plugin_config_prefix = "symlinkmonitor_"
    # 加载顺序
    plugin_order = 5
    # 可使用的用户级别
    auth_level = 1

    # ---- 配置项 ----
    _enabled = False
    _notify = True
    _onlyonce = False
    _monitor_dirs = ""
    _exclude_dirs = ""
    _exclude_keywords = ""
    _scan_interval = 10
    _delayed_deletion = True
    _delay_seconds = 30
    _delete_scrap = True
    _delete_history = True
    _delete_torrents = True
    _clean_empty_dir = True

    # ---- 运行时状态 ----
    # {源目录: 软链接目标目录}
    _dirconf: Dict[str, Path] = {}
    # {源文件绝对路径: (size, mtime)}
    _snapshot: Dict[str, Tuple[int, float]] = {}
    # 待删除队列 [(源文件路径, 入队时间戳)]
    _deletion_queue: List[Tuple[str, datetime]] = []
    _stop_event: threading.Event = threading.Event()
    _thread: Optional[threading.Thread] = None
    _history = None
    # 本次运行统计
    _stat = {"link": 0, "delete": 0, "fail": 0}

    # ------------------------------------------------------------------ 配置

    def init_plugin(self, config: dict = None):
        """读取配置并启动监控线程；必须允许重复调用。"""
        self.stop_service()
        self._stop_event = threading.Event()
        self._dirconf = {}
        with _queue_lock:
            self._deletion_queue = []

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_dirs = config.get("exclude_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._delayed_deletion = bool(config.get("delayed_deletion", True))
            self._delete_scrap = bool(config.get("delete_scrap", True))
            self._delete_history = bool(config.get("delete_history", True))
            self._delete_torrents = bool(config.get("delete_torrents", True))
            self._clean_empty_dir = bool(config.get("clean_empty_dir", True))
            try:
                self._scan_interval = max(3, int(config.get("scan_interval") or 10))
            except (TypeError, ValueError):
                self._scan_interval = 10
            try:
                self._delay_seconds = max(5, min(86400, int(config.get("delay_seconds") or 30)))
            except (TypeError, ValueError):
                self._delay_seconds = 30

        self._history = TransferHistoryOper()

        # 解析「源目录:目标目录」
        for line in self._monitor_dirs.splitlines():
            line = line.strip()
            if not line:
                continue
            src, target = self._split_dir_conf(line)
            if not target:
                logger.warn(f"软链接监控：{src} 未配置软链接目标目录，跳过")
                continue
            if self._is_same_or_child(target, src):
                logger.warn(f"软链接监控：目标目录 {target} 是监控目录 {src} 的子目录，跳过")
                continue
            self._dirconf[src] = target

        if not (self._enabled or self._onlyonce):
            return
        if not self._dirconf:
            logger.warn("软链接监控：未配置有效的监控目录")
            return

        # 建立基线快照
        self._snapshot = self._build_snapshot()

        # 立即运行一次：补建缺失软链接
        if self._onlyonce:
            self._onlyonce = False
            self._save_config()
            self.sync_all()

        if self._enabled:
            self._thread = threading.Thread(target=self._loop, name="SymlinkMonitor", daemon=True)
            self._thread.start()
            logger.info(
                f"软链接监控已启动，监控 {len(self._dirconf)} 个目录，"
                f"扫描间隔 {self._scan_interval}s，延迟删除 {'开' if self._delayed_deletion else '关'}"
                f"（{self._delay_seconds}s）"
            )

    def _save_config(self):
        """保存当前配置。"""
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "monitor_dirs": self._monitor_dirs,
            "exclude_dirs": self._exclude_dirs,
            "exclude_keywords": self._exclude_keywords,
            "scan_interval": self._scan_interval,
            "delayed_deletion": self._delayed_deletion,
            "delay_seconds": self._delay_seconds,
            "delete_scrap": self._delete_scrap,
            "delete_history": self._delete_history,
            "delete_torrents": self._delete_torrents,
            "clean_empty_dir": self._clean_empty_dir,
        })

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    # -------------------------------------------------------------- 主循环

    def _loop(self):
        """监控主循环：定时扫描 + 处理延迟删除队列。"""
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception as e:
                logger.error(f"软链接监控扫描异常：{e} - {traceback.format_exc()}")
            try:
                self._flush_deletion_queue()
            except Exception as e:
                logger.error(f"软链接监控延迟删除异常：{e} - {traceback.format_exc()}")
            self._stop_event.wait(self._scan_interval)

    def _scan_once(self):
        """扫描一次监控目录，处理新增与删除。"""
        current = self._build_snapshot()
        with _state_lock:
            old = self._snapshot
        added = [p for p in current if p not in old]
        removed = [p for p in old if p not in current]
        # 文件大小/修改时间变化的文件按新增处理（下载完成、改名等）
        changed = [p for p in current if p in old and current[p] != old[p]]

        for path in added + changed:
            self._create_link(Path(path))

        for path in removed:
            self._enqueue_deletion(path)

        with _state_lock:
            self._snapshot = current

    def _build_snapshot(self) -> Dict[str, Tuple[int, float]]:
        """建立监控目录的文件快照。"""
        snap: Dict[str, Tuple[int, float]] = {}
        for src in self._dirconf:
            base = Path(src)
            if not base.is_dir():
                logger.warn(f"软链接监控：监控目录不存在 {src}")
                continue
            for root, _dirs, files in os.walk(base):
                for name in files:
                    fp = Path(root) / name
                    key = str(fp)
                    if self._skip_file(fp, key):
                        continue
                    try:
                        st = fp.stat()
                        snap[key] = (st.st_size, st.st_mtime)
                    except OSError:
                        continue
        return snap

    def _skip_file(self, path: Path, key: str = None) -> bool:
        """判断文件是否应跳过（临时文件 / 隐藏 / 回收站 / 排除关键词 / 不删除目录）。"""
        key = key or str(path)
        if path.suffix.lower() in TMP_SUFFIXES:
            return True
        for seg in ("/@Recycle/", "/#recycle/", "/@eaDir", "/.Trash"):
            if seg in key:
                return True
        if any(part.startswith(".") for part in path.parts):
            return True
        if self._is_excluded(path):
            return True
        if self._exclude_keywords:
            for kw in self._exclude_keywords.splitlines():
                kw = kw.strip()
                if kw and re.search(kw, key):
                    return True
        return False

    # -------------------------------------------------------------- 建软链接

    def _create_link(self, src: Path) -> bool:
        """为源文件在目标目录建立软链接。"""
        mon_path = self._owner_of(src)
        if not mon_path:
            return False
        target = self._dirconf.get(mon_path)
        if not target:
            return False
        try:
            rel = src.relative_to(Path(mon_path))
        except ValueError:
            return False
        dest = target / rel
        if dest.exists() or dest.is_symlink():
            return True
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(src), str(dest))
            self._stat["link"] += 1
            logger.info(f"软链接监控：建立软链接 {dest} -> {src}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="🔗 已建立软链接",
                    text=f"源文件：{src}\n软链接：{dest}",
                )
            return True
        except OSError as e:
            self._stat["fail"] += 1
            logger.error(f"软链接监控：建立软链接失败 {dest} -> {src}：{e}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="🔗 建立软链接失败",
                    text=f"源文件：{src}\n原因：{e}",
                )
            return False

    def _link_path_of(self, src: str) -> Optional[Path]:
        """由源文件路径推导出对应的软链接路径。"""
        mon_path = self._owner_of(Path(src))
        if not mon_path:
            return None
        target = self._dirconf.get(mon_path)
        if not target:
            return None
        try:
            rel = Path(src).relative_to(Path(mon_path))
        except ValueError:
            return None
        return target / rel

    def sync_all(self):
        """全量扫描一次：补建缺失的软链接。"""
        logger.info("软链接监控：开始全量扫描")
        snap = self._build_snapshot()
        with _state_lock:
            self._snapshot = snap
        count = 0
        for key in snap:
            if self._create_link(Path(key)):
                count += 1
        logger.info(f"软链接监控：全量扫描完成，处理 {count} 个文件")
        return count

    # -------------------------------------------------------------- 延迟删除

    def _enqueue_deletion(self, src: str):
        """把删除事件放入延迟队列。"""
        if self._is_excluded(Path(src)):
            logger.info(f"软链接监控：{src} 在不删除目录中，忽略删除")
            return
        link = self._link_path_of(src)
        if link and self._is_excluded(link):
            logger.info(f"软链接监控：软链接 {link} 在不删除目录中，忽略删除")
            return
        if not self._delayed_deletion:
            self._execute_deletion(src)
            return
        with _queue_lock:
            if any(item[0] == src for item in self._deletion_queue):
                return
            self._deletion_queue.append((src, datetime.now()))
        logger.info(f"软链接监控：源文件已删除，{self._delay_seconds}s 后清理 -> {src}")

    def _flush_deletion_queue(self):
        """处理延迟队列中到期的任务。"""
        now = datetime.now()
        due: List[str] = []
        with _queue_lock:
            keep = []
            for src, ts in self._deletion_queue:
                if (now - ts).total_seconds() >= self._delay_seconds:
                    due.append(src)
                else:
                    keep.append((src, ts))
            self._deletion_queue = keep
        for src in due:
            try:
                self._execute_deletion(src)
            except Exception as e:
                logger.error(f"软链接监控：清理 {src} 失败：{e} - {traceback.format_exc()}")

    def _execute_deletion(self, src: str):
        """执行一次完整的联动清理。"""
        src_path = Path(src)
        # 源文件又回来了（重新下载/改名）则跳过硬链接删除，只清理旧刮削文件
        if src_path.exists():
            logger.info(f"软链接监控：源文件 {src} 已重新出现，跳过清理")
            if self._delete_scrap:
                self._delete_scrap_files(src_path)
            return

        removed: List[str] = []

        # 1. 删除软链接
        link = self._link_path_of(src)
        if link and (link.is_symlink() or link.exists()):
            try:
                if link.is_dir() and not link.is_symlink():
                    shutil.rmtree(link)
                else:
                    link.unlink()
                removed.append(str(link))
                logger.info(f"软链接监控：已删除软链接 {link}")
            except OSError as e:
                logger.error(f"软链接监控：删除软链接 {link} 失败：{e}")

        # 2. 清理刮削文件
        if self._delete_scrap:
            self._delete_scrap_files(src_path)
            if link:
                self._delete_scrap_files(link)

        # 3. 删除转移记录 + 4. 联动删除种子
        hash_str = self._delete_transfer_history(src)
        if self._delete_torrents:
            self._delete_torrent(hash_str, src, link)

        # 5. 清理空目录（含只剩刮削文件的目录，如残留的 poster.jpg）
        if self._clean_empty_dir:
            for p in filter(None, (link, src_path)):
                if self._delete_scrap:
                    self._purge_only_scrap_dirs(p.parent)
                self._clean_empty_dirs(p.parent)

        self._stat["delete"] += 1
        if self._notify:
            lines = [f"🗂️ 源文件：{src}"]
            lines.append(f"🔗 软链接：{removed[0] if removed else '（未找到）'}")
            if self._delete_scrap:
                lines.append("🖼️ 已清理刮削文件")
            if self._delete_history:
                lines.append("📝 已删除转移记录" if hash_str is not None else "📝 无转移记录")
            if self._delete_torrents:
                lines.append("🌱 已联动删除种子" if hash_str else "🌱 未找到关联种子")
            if self._clean_empty_dir:
                lines.append("📁 已清理空目录")
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="🧹 软链接联动清理",
                text=f"⏰ 延迟删除完成\n\n" + "\n".join(lines),
            )

    def _delete_transfer_history(self, src: str) -> Optional[str]:
        """删除源路径对应的转移记录，返回其下载器 hash。"""
        if not self._delete_history or self._history is None:
            return None
        try:
            his = self._history.get_by_src(src)
            if not his:
                return None
            hash_str = getattr(his, "download_hash", None)
            self._history.delete(his.id)
            logger.info(f"软链接监控：已删除转移记录 #{his.id}（{src}）")
            return hash_str
        except Exception as e:
            logger.error(f"软链接监控：删除转移记录失败 {src}：{e}")
            return None

    def _delete_torrent(self, hash_str: Optional[str], src: str, link: Optional[Path]):
        """联动删除下载器中的任务（保留数据）。"""
        if hash_str:
            logger.info(f"软链接监控：联动删除种子 {hash_str}")
            eventmanager.send_event(EventType.DownloadFileDeleted, {"src": src, "hash": hash_str})
            return
        # 没有转移记录时，退化为按保存路径匹配下载任务
        try:
            from app.helper.downloader import DownloaderHelper
            services = DownloaderHelper().get_services()
            if not services:
                return
            for _name, info in services.items():
                inst = info.instance
                if inst.is_inactive():
                    continue
                torrents, _flag = inst.get_torrents()
                for t in torrents or []:
                    save = getattr(t, "save_path", None) or getattr(t, "download_dir", None)
                    name = getattr(t, "name", None)
                    if not save or not name:
                        continue
                    if str(Path(save) / name) == str(src) or (link and str(Path(save) / name) == str(link)):
                        tid = getattr(t, "hash", None) or getattr(t, "hashString", None)
                        if tid:
                            inst.delete_torrents(delete_file=False, ids=[tid])
                            logger.info(f"软链接监控：按路径匹配删除种子 {name}（{tid}）")
                        return
        except Exception as e:
            logger.error(f"软链接监控：联动删除种子失败 {src}：{e}")

    def _delete_scrap_files(self, path: Path):
        """清理与 path 同名的刮削文件 / 刮削目录。"""
        if not path.parent.is_dir():
            return
        prefix = path.stem
        try:
            for item in path.parent.iterdir():
                if item == path:
                    continue
                if not item.name.startswith(prefix):
                    continue
                try:
                    if item.is_dir():
                        if item.suffix.lower() in SCRAP_DIR_SUFFIXES:
                            shutil.rmtree(item, ignore_errors=True)
                            logger.info(f"软链接监控：已删除刮削目录 {item}")
                    elif item.suffix.lower() in SCRAP_EXTENSIONS:
                        item.unlink()
                        logger.info(f"软链接监控：已删除刮削文件 {item}")
                except OSError as e:
                    logger.error(f"软链接监控：删除刮削文件 {item} 失败：{e}")
        except OSError as e:
            logger.error(f"软链接监控：遍历刮削文件失败 {path.parent}：{e}")

    def _is_only_scrap(self, path: Path) -> bool:
        """目录内是否只剩刮削文件/刮削目录（说明媒体本体已不在）。"""
        try:
            for item in path.iterdir():
                if item.is_dir():
                    if item.suffix.lower() not in SCRAP_DIR_SUFFIXES:
                        return False
                elif item.suffix.lower() not in SCRAP_EXTENSIONS:
                    return False
        except OSError:
            return False
        return True

    def _purge_only_scrap_dirs(self, path: Optional[Path]):
        """自下而上清理「只剩刮削文件」的目录，直到遇到监控目录根或不删除目录。"""
        while path is not None:
            if self._is_excluded(path) or str(path) in self._dirconf or not path.is_dir():
                return
            if not self._is_only_scrap(path):
                return
            try:
                for item in list(path.iterdir()):
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink()
                    logger.info(f"软链接监控：已清理残留刮削 {item}")
            except OSError as e:
                logger.error(f"软链接监控：清理残留刮削失败 {path}：{e}")
                return
            path = path.parent

    def _clean_empty_dirs(self, path: Optional[Path]):
        """自下而上清理空目录，遇到监控目录根或不删除目录停止。"""
        while path is not None:
            if self._is_excluded(path):
                return
            if str(path) in self._dirconf or not path.is_dir():
                return
            try:
                if any(path.iterdir()):
                    return
                path.rmdir()
                logger.info(f"软链接监控：已清理空目录 {path}")
            except OSError:
                return
            path = path.parent

    # -------------------------------------------------------------- 工具方法

    @staticmethod
    def _split_dir_conf(line: str) -> Tuple[str, Optional[Path]]:
        """解析「源目录:目标目录」，兼容 Windows 盘符。"""
        if os.name == "nt" and line.count(":") > 1:
            parts = [line.split(":")[0] + ":" + line.split(":")[1],
                     ":".join(line.split(":")[2:])]
        else:
            parts = line.split(":")
        src = parts[0].strip()
        target = Path(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
        return src, target

    def _owner_of(self, path: Path) -> Optional[str]:
        """返回 path 所属的监控目录。"""
        key = str(path)
        for src in self._dirconf:
            if key == src or key.startswith(src.rstrip("/") + "/"):
                return src
        return None

    @staticmethod
    def _norm(p) -> str:
        return os.path.normcase(os.path.normpath(str(Path(p).expanduser())))

    @classmethod
    def _is_same_or_child(cls, path, base) -> bool:
        """path 是否等于 base 或位于 base 之下。"""
        if base is None:
            return False
        try:
            return os.path.commonpath([cls._norm(path), cls._norm(base)]) == cls._norm(base)
        except ValueError:
            return False

    def _is_excluded(self, path) -> bool:
        """是否命中「不删除目录」。"""
        if not self._exclude_dirs:
            return False
        for line in self._exclude_dirs.splitlines():
            line = line.strip()
            if line and self._is_same_or_child(path, line):
                return True
        return False

    def status_text(self) -> str:
        """运行状态描述。"""
        return (
            f"监控目录 {len(self._dirconf)} 个｜快照 {len(self._snapshot)} 个文件｜"
            f"待清理 {len(self._deletion_queue)} 项｜本次已建链 {self._stat['link']}、"
            f"已清理 {self._stat['delete']}、失败 {self._stat['fail']}"
        )

    # -------------------------------------------------------------- 宿主接口

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令与事件。"""
        return [{
            "cmd": "/symlink_monitor",
            "event": EventType.PluginAction,
            "desc": "软链接监控全量扫描",
            "category": "管理",
            "data": {"action": "symlink_monitor"},
        }]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        """响应远程命令。"""
        if not event or not event.event_data:
            return
        if event.event_data.get("action") != "symlink_monitor":
            return
        count = self.sync_all()
        self.post_message(
            channel=event.event_data.get("channel"),
            userid=event.event_data.get("user"),
            title="🔗 软链接监控完成",
            text=self.status_text() + f"\n本次处理 {count} 个文件",
        )

    def get_api(self) -> List[Dict[str, Any]]:
        """注册后端 API。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即运行一次",
                "description": "全量扫描监控目录并补建软链接",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "运行状态",
                "description": "返回当前监控状态",
            },
        ]

    def api_run(self):
        """立即运行一次。"""
        count = self.sync_all()
        return {"success": True, "message": self.status_text(), "data": {"count": count}}

    def api_status(self):
        """返回运行状态。"""
        return {"success": True, "data": {
            "enabled": self._enabled,
            "monitor_dirs": list(self._dirconf.keys()),
            "exclude_dirs": [x.strip() for x in self._exclude_dirs.splitlines() if x.strip()],
            "status": self.status_text(),
        }}

    def get_service(self) -> List[Dict[str, Any]]:
        """注册周期服务（在插件重载后仍能继续清理到期的延迟任务）。"""
        if not self._enabled:
            return []
        return [{
            "id": "SymlinkMonitor.Flush",
            "name": "软链接监控延迟清理",
            "trigger": "interval",
            "func": self._flush_deletion_queue,
            "kwargs": {"seconds": max(3, min(60, self._scan_interval))},
        }]

    def get_page(self) -> List[dict]:
        """详情页。"""
        return [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "title": "🔗 软链接监控",
                "text": ("监控目录中的新增文件会自动在目标目录建立软链接；"
                         "源文件删除后会延迟确认，再联动清理软链接、刮削文件、转移记录与下载种子。\n\n"
                         + self.status_text()),
            },
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页面。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._switch("enabled", "启用插件", 12, 3),
                            self._switch("notify", "发送通知", 12, 3),
                            self._switch("onlyonce", "立即运行一次", 12, 3),
                            self._switch("delayed_deletion", "延迟删除", 12, 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._switch("delete_scrap", "清理刮削文件", 12, 3),
                            self._switch("delete_history", "删除转移记录", 12, 3),
                            self._switch("delete_torrents", "联动删除种子", 12, 3),
                            self._switch("clean_empty_dir", "清理空目录", 12, 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._text("scan_interval", "扫描间隔（秒）", "默认 10 秒，最小 3", 12, 6),
                            self._text("delay_seconds", "延迟删除时间（秒）",
                                       "源文件删除后等待多久再清理，最小 5 秒", 12, 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "monitor_dirs",
                                        "label": "监控目录（源目录:软链接目标目录）",
                                        "rows": 5,
                                        "placeholder": "每一行一条，例如：\n"
                                                       "/downloads/电影:/media/电影\n"
                                                       "/downloads/剧集:/media/剧集",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "exclude_dirs",
                                        "label": "不删除目录",
                                        "rows": 3,
                                        "placeholder": "每一行一个目录；这些目录下的文件不建立软链接，也永远不会被本插件删除\n"
                                                       "（含其子目录）",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "exclude_keywords",
                                        "label": "排除关键词（正则）",
                                        "rows": 2,
                                        "placeholder": "每一行一个正则，命中完整路径则忽略该文件",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "软链接可跨文件系统，便于下载盘与媒体库分盘存放。"
                                                "联动删除种子只移除下载任务、不删除文件数据。",
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "monitor_dirs": "",
            "exclude_dirs": "",
            "exclude_keywords": "",
            "scan_interval": 10,
            "delayed_deletion": True,
            "delay_seconds": 30,
            "delete_scrap": True,
            "delete_history": True,
            "delete_torrents": True,
            "clean_empty_dir": True,
        }

    @staticmethod
    def _switch(model: str, label: str, cols: int = 12, md: int = 4) -> dict:
        """生成一个开关表单项。"""
        return {
            "component": "VCol",
            "props": {"cols": cols, "md": md},
            "content": [{
                "component": "VSwitch",
                "props": {"model": model, "label": label},
            }],
        }

    @staticmethod
    def _text(model: str, label: str, placeholder: str = "", cols: int = 12, md: int = 6) -> dict:
        """生成一个文本框表单项。"""
        return {
            "component": "VCol",
            "props": {"cols": cols, "md": md},
            "content": [{
                "component": "VTextField",
                "props": {"model": model, "label": label, "placeholder": placeholder},
            }],
        }

    def stop_service(self):
        """停止监控线程并清空队列，可重复调用。"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

        # 停用即执行剩余任务，避免延迟窗口内丢清理
        pending: List[str] = []
        with _queue_lock:
            if self._deletion_queue:
                pending = [src for src, _ts in self._deletion_queue if not Path(src).exists()]
                self._deletion_queue = []
        for src in pending:
            try:
                self._execute_deletion(src)
            except Exception as e:
                logger.error(f"软链接监控：停止时清理 {src} 失败：{e}")

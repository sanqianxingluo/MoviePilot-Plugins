"""
MoviePilot 软链接清理插件 (SymlinkMonitor)

**本插件不建立任何软链接。** 它只做一件事：盯着下载目录，发现文件被删除后
延迟确认，再把软链接目录（媒体库）里指向它的那个软链接删掉，并联动清理
刮削文件、转移记录、下载任务与空目录。

  1. 监控下载目录中的文件变化；
  2. 源文件被删除 -> 延迟若干秒确认为真删除后再清理：
       · 软链接目录中指向该文件的软链接
       · 同名的刮削文件（nfo / 图片 / 字幕）与刮削目录
       · MoviePilot 转移记录
       · 下载器中的任务（种子，不删除数据）
       · 空目录
  3. 「立即运行一次」= 全量清理一次：扫掉所有指向下载目录、但源文件已不存在
     的孤儿软链接。

安全约束：
  · 只删除「软链接」本身，绝不删除软链接目录里的真实文件；
  · 默认**不删除下载目录里的任何文件**（刮削清理仅作用于软链接目录侧）；
  · 「保护目录」（不删除目录）里的内容照常监控，但插件绝不删除它们；
  · 只删除指向监控目录的软链接，不碰无关链接。

双向行为：
  · 删下载目录 → 同步清理软链接目录里指向它的软链接（主功能）
  · 删软链接目录 → **不会**影响下载目录

配置项：
  enabled            启用插件
  notify             发送通知
  onlyonce           立即运行一次（全量清理孤儿链接）
  monitor_dirs       监控目录，每行一条：「下载目录:软链接目录」
  exclude_dirs       保护目录（不删除目录），每行一条；内容仍监控，但绝不删除
  exclude_keywords   排除关键词，每行一条，命中则忽略
  scan_interval      扫描间隔（秒）
  delayed_deletion   启用延迟删除
  delay_seconds      延迟删除时间（秒）
  delete_scrap       清理软链接目录里的刮削链接/文件
  clean_source_scrap 是否也清理下载目录里的刮削文件（默认关，保护源数据）
  delete_history     联动删除转移记录
  delete_torrents    联动删除下载种子
  clean_empty_dir    联动清理空目录

兼容说明：本插件使用 MoviePilot 公开的宿主接口（app.plugins / app.log /
app.core.event / app.schemas.types / app.db.transferhistory_oper）。
日志入口在 V3 上优先取 app.sdk.logging，缺失时回退 app.log，因此同一份
实现可同时运行在 V2.x 与 V3 宿主上。
"""

import json
import os
import re
import shutil
import threading
import traceback
from datetime import datetime
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

# 遍历软链接目录时跳过的系统目录
SKIP_DIR_NAMES = {"@eaDir", "#recycle", "@Recycle", ".Trash", ".stfolder"}

_state_lock = threading.Lock()
_queue_lock = threading.Lock()


def _now_unique(entry: dict) -> str:
    """生成一条记录的唯一键（时间 + 源路径 + 条目数）。"""
    return "%s|%s|%s" % (entry.get("time", ""), entry.get("src", ""),
                         entry.get("links", ""))


def _shorten(path: str, limit: int = None) -> str:
    """截断过长的路径，保留尾部（尾部信息量更大）。"""
    limit = limit or DELETION_LOG_PATH_MAX
    text = str(path)
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):]

# 删除记录：最多保留条数（超出丢弃最旧的）
DELETION_LOG_LIMIT = 200
# 详情页展示条数
DELETION_LOG_SHOW = 20
# 记录里的长路径截断长度
DELETION_LOG_PATH_MAX = 110


class SymlinkMonitor(_PluginBase):
    # 插件名称
    plugin_name = "软链接监控"
    # 插件描述
    plugin_desc = "监控下载目录，文件删除后延迟清理软链接目录中指向它的软链接，并联动清理刮削链接、转移记录与下载种子；保护目录里的内容永不删除。"
    # 插件图标
    plugin_icon = "Linkace_C.png"
    # 插件版本
    plugin_version = "2.2.4"
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
    _clean_source_scrap = False
    _delete_history = True
    _delete_torrents = True
    _clean_empty_dir = True

    # ---- 运行时状态 ----
    # {下载目录: 软链接目录}
    _dirconf: Dict[str, Path] = {}
    # {源文件绝对路径: (size, mtime)}
    _snapshot: Dict[str, Tuple[int, float]] = {}
    # 待删除队列 [(源文件路径, 入队时间戳)]
    _deletion_queue: List[Tuple[str, datetime]] = []
    _stop_event: threading.Event = threading.Event()
    _thread: Optional[threading.Thread] = None
    _history = None
    # 本次运行统计
    _stat = {"delete": 0, "link": 0, "sweep": 0, "fail": 0}

    # ---- 删除记录（详情页可视化，落库持久化）----
    _deletion_log: List[dict] = []
    _deletion_log_ids: set = set()
    _deletion_log_lock = threading.Lock()
    _TMPLOG_KEY = "deletion_log"

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
            self._clean_source_scrap = bool(config.get("clean_source_scrap", False))
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
        # 读取历史删除记录（详情页可视化用）
        self._load_deletion_log()

        # 解析「下载目录:软链接目录」
        for line in self._monitor_dirs.splitlines():
            line = line.strip()
            if not line:
                continue
            src, link_dir = self._split_dir_conf(line)
            if not link_dir:
                logger.warn(f"软链接监控：{src} 未配置软链接目录，跳过")
                continue
            if self._is_same_or_child(link_dir, src):
                logger.warn(f"软链接监控：软链接目录 {link_dir} 位于下载目录 {src} 内，跳过")
                continue
            self._dirconf[src] = link_dir

        # 防呆：「不删除目录」若覆盖了监控目录（或其父目录），该区域的文件将永远
        # 不进快照，删除事件彻底失灵 —— 必须在启动前拦住。
        self._dirconf = self._drop_self_excluded(self._dirconf)

        if not (self._enabled or self._onlyonce):
            return
        if not self._dirconf:
            logger.warn("软链接监控：未配置有效的监控目录")
            return

        # 建立基线快照（只用于发现删除，不建链）
        self._snapshot = self._build_snapshot()

        # 立即运行一次：全量清理孤儿软链接
        if self._onlyonce:
            self._onlyonce = False
            self._save_config()
            self.sync_all()

        if self._enabled:
            self._thread = threading.Thread(target=self._loop, name="SymlinkMonitor", daemon=True)
            self._thread.start()
            logger.info(
                f"软链接监控已启动，监控 {len(self._dirconf)} 个下载目录，"
                f"扫描间隔 {self._scan_interval}s，延迟删除 "
                f"{'开' if self._delayed_deletion else '关'}（{self._delay_seconds}s）"
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
            "clean_source_scrap": self._clean_source_scrap,
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
        """扫描一次下载目录：只关心「文件消失了」。"""
        current = self._build_snapshot()
        with _state_lock:
            old = self._snapshot
        removed = [p for p in old if p not in current]
        for path in removed:
            self._enqueue_deletion(path)
        with _state_lock:
            self._snapshot = current

    def _build_snapshot(self) -> Dict[str, Tuple[int, float]]:
        """建立下载目录的文件快照。"""
        snap: Dict[str, Tuple[int, float]] = {}
        for src in self._dirconf:
            base = Path(src)
            if not base.is_dir():
                logger.warn(f"软链接监控：监控目录不存在 {src}")
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
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
        """判断文件是否应跳过（临时 / 隐藏 / 回收站 / 排除关键词）。

        注意：「不删除目录」是**保护目录**——里面的文件照常监控（照常建快照、
        照常触发联动清理），只是插件绝不会删除它们。因此这里不做排除。
        """
        key = key or str(path)
        if path.suffix.lower() in TMP_SUFFIXES:
            return True
        for seg in ("/@Recycle/", "/#recycle/", "/@eaDir", "/.Trash"):
            if seg in key:
                return True
        if any(part.startswith(".") for part in path.parts):
            return True
        if self._exclude_keywords:
            for kw in self._exclude_keywords.splitlines():
                kw = kw.strip()
                if kw and re.search(kw, key):
                    return True
        return False

    # -------------------------------------------------------------- 软链接定位

    def _owner_of(self, path) -> Optional[str]:
        """返回 path 所属的下载目录。"""
        key = str(path)
        for src in self._dirconf:
            if key == src or key.startswith(src.rstrip("/") + "/"):
                return src
        return None

    def _link_dir_of(self, src) -> Optional[Path]:
        """返回源文件所属下载目录对应的软链接目录。"""
        mon = self._owner_of(src)
        return self._dirconf.get(mon) if mon else None

    def _link_path_of(self, src: str) -> Optional[Path]:
        """由下载目录中的源文件路径，直接推导软链接目录中的同名路径。"""
        mon = self._owner_of(Path(src))
        link_dir = self._dirconf.get(mon) if mon else None
        if not link_dir:
            return None
        try:
            rel = Path(src).relative_to(Path(mon))
        except ValueError:
            return None
        return link_dir / rel

    @staticmethod
    def _iter_entries(base: Path):
        """遍历软链接目录下的所有条目（不跟随软链接）。"""
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
            for name in list(dirs) + files:
                yield Path(root) / name

    @staticmethod
    def _target_of(link: Path) -> Optional[Path]:
        """读取软链接指向的目标绝对路径；相对目标按链接所在目录解析。"""
        try:
            raw = os.readlink(link)
        except OSError:
            return None
        target = Path(raw)
        if not target.is_absolute():
            target = link.parent / target
        return Path(os.path.normpath(str(target)))

    def _find_links(self, src: str) -> List[Path]:
        """找出软链接目录中指向源文件 src 的所有软链接。"""
        found: List[Path] = []
        seen = set()

        def add(p: Path):
            key = str(p)
            if key not in seen:
                seen.add(key)
                found.append(p)

        # 1. 直接推导（下载目录 -> 软链接目录，同名相对路径）
        direct = self._link_path_of(src)
        if direct and direct.is_symlink():
            add(direct)

        # 2. 扫描软链接目录，按链接目标匹配（应对改名、分类目录等情形）
        mon = self._owner_of(src)
        link_dir = self._dirconf.get(mon) if mon else None
        if link_dir and link_dir.is_dir():
            src_norm = self._norm(src)
            for entry in self._iter_entries(link_dir):
                if str(entry) in seen or not entry.is_symlink():
                    continue
                target = self._target_of(entry)
                if target is not None and self._norm(target) == src_norm:
                    add(entry)
        return found

    def _orphan_links(self) -> List[Path]:
        """全量扫描：找出指向监控目录、但源文件已不存在的断链（孤儿）。"""
        orphans: List[Path] = []
        for mon, link_dir in self._dirconf.items():
            if not link_dir.is_dir():
                logger.warn(f"软链接监控：软链接目录不存在 {link_dir}")
                continue
            for entry in self._iter_entries(link_dir):
                if not entry.is_symlink():
                    continue
                if entry.exists():  # 链接有效
                    continue
                target = self._target_of(entry)
                if target is None:
                    continue
                # 只处理指向监控目录的断链
                if not self._is_same_or_child(target, mon):
                    continue
                # 保护目录里的链接不动
                if self._is_protected(entry):
                    continue
                orphans.append(entry)
        return orphans

    def sync_all(self) -> int:
        """全量清理一次：删除所有孤儿软链接，并重建快照。"""
        logger.info("软链接监控：开始全量清理孤儿链接")
        count = 0
        swept: List[str] = []
        for link in self._orphan_links():
            try:
                link.unlink()
                count += 1
                swept.append(str(link))
                self._stat["sweep"] += 1
                logger.info(f"软链接监控：已清理孤儿软链接 {link}")
                if self._delete_scrap:
                    self._delete_scrap_files(link)
                if self._clean_empty_dir:
                    self._clean_empty_dirs(link.parent)
            except OSError as e:
                self._stat["fail"] += 1
                logger.error(f"软链接监控：清理孤儿软链接 {link} 失败：{e}")
        with _state_lock:
            self._snapshot = self._build_snapshot()
        logger.info(f"软链接监控：全量清理完成，共清理 {count} 个孤儿软链接")
        if count:
            self._append_deletion_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "src": "（全量清理孤儿链接）",
                "links": count,
                "link_paths": [_shorten(x) for x in swept[:5]],
                "scrap": 0,
                "history": None,
                "torrent": None,
                "empty": self._clean_empty_dir,
                "reason": "孤儿清理",
            })
        return count

    # -------------------------------------------------------------- 删除记录
    #
    # 单一事实源 = 插件数据目录里的 deletion_log.jsonl（追加写、跨重启保留）。
    # 内存里只缓存一份只读快照，任何对外输出（详情页 / API / 计数）都走同一个
    # _tail_delete_log()，避免「列表读文件、计数读内存」两边不一致。

    def _log_path(self) -> Path:
        """删除记录文件路径。"""
        return Path(self.get_data_path()) / "deletion_log.jsonl"

    def _load_deletion_log(self):
        """载入记录（把文件读进内存缓存），并顺带裁到上限。"""
        self._deletion_log = self._read_log()
        if len(self._deletion_log) > DELETION_LOG_LIMIT:
            self._deletion_log = self._deletion_log[-DELETION_LOG_LIMIT:]
            self._rewrite_log()
        self._deletion_log_ids = {x.get("unique") for x in self._deletion_log if x.get("unique")}
        # 兼容旧版本：曾把记录存在插件数据库里，若有则迁移到文件后清掉。
        try:
            legacy = self.get_data(self._TMPLOG_KEY)
            if isinstance(legacy, list) and legacy:
                for old in legacy:
                    if isinstance(old, dict):
                        self._deletion_log.append(old)
                self._rewrite_log()
            self.save_data(self._TMPLOG_KEY, [])
        except Exception:
            pass

    def _read_log(self, limit: Optional[int] = None) -> List[dict]:
        """读记录文件（容错的 JSONL 解析），返回最新 limit 条（倒序）。"""
        try:
            path = self._log_path()
            if not path.is_file():
                return []
            rows = []
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
            rows.sort(key=lambda r: str(r.get("time", "")), reverse=True)
            return rows[:limit] if limit else rows
        except Exception as e:
            logger.warn(f"软链接监控：读取删除记录失败：{e}")
            return []

    def _rewrite_log(self):
        """整表重写记录文件（只在裁剪/清理时需要）。"""
        try:
            path = self._log_path()
            with path.open("w", encoding="utf-8") as f:
                for entry in self._deletion_log:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"软链接监控：重写删除记录失败：{e}")

    def _tail_delete_log(self, limit: int = 20) -> List[dict]:
        """对外统一入口：返回最新 limit 条记录（倒序）。"""
        rows = self._read_log()
        if len(rows) > DELETION_LOG_LIMIT:
            rows = rows[:DELETION_LOG_LIMIT]
        return rows[:limit]

    def _append_deletion_log(self, entry: dict):
        """追加一条删除记录（追加写文件），并同步内存缓存。"""
        entry.setdefault("unique", _now_unique(entry))
        try:
            path = self._log_path()
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"软链接监控：写入删除记录失败：{e}")
            return
        self._deletion_log.append(entry)
        self._deletion_log_ids.add(entry["unique"])
        # 超出上限：裁剪文件
        if len(self._deletion_log) > DELETION_LOG_LIMIT:
            self._deletion_log = self._deletion_log[-DELETION_LOG_LIMIT:]
            self._deletion_log_ids = {x.get("unique") for x in self._deletion_log
                                      if x.get("unique")}
            self._rewrite_log()

    def _clear_delete_log(self):
        """清空删除记录。"""
        self._deletion_log = []
        self._deletion_log_ids = set()
        try:
            path = self._log_path()
            if path.is_file():
                path.unlink()
        except Exception as e:
            logger.warn(f"软链接监控：删除记录文件清理失败：{e}")
        try:
            self.save_data(self._TMPLOG_KEY, [])
        except Exception:
            pass

    def _log_count(self) -> int:
        """记录总数（与列表同源）。"""
        return len(self._read_log(limit=DELETION_LOG_LIMIT))

    # -------------------------------------------------------------- 延迟删除

    def _enqueue_deletion(self, src: str):
        """把删除事件放入延迟队列。"""
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
        """执行一次完整的联动清理（绝不删除软链接目录中的真实文件）。"""
        src_path = Path(src)
        # 源文件又回来了（重新下载 / 改名）则跳过
        if src_path.exists():
            logger.info(f"软链接监控：源文件 {src} 已重新出现，跳过清理")
            return

        removed: List[str] = []
        links = self._find_links(src)

        # 1. 删除指向该源文件的软链接（链接位于保护目录则跳过）
        for link in links:
            if self._is_protected(link):
                logger.info(f"软链接监控：{link} 在保护目录中，跳过")
                continue
            if not link.is_symlink():
                # 安全兜底：软链接目录中的真实文件一律不动
                logger.warn(f"软链接监控：{link} 不是软链接，跳过删除")
                continue
            try:
                link.unlink()
                removed.append(str(link))
                logger.info(f"软链接监控：已删除软链接 {link} -> {src}")
            except OSError as e:
                self._stat["fail"] += 1
                logger.error(f"软链接监控：删除软链接 {link} 失败：{e}")

        # 2. 清理刮削文件：只清「软链接目录侧」的链接名对应刮削；
        #    下载目录（源）侧若受保护或未开启「清理下载目录刮削」，一律不动。
        scrap_count = 0
        if self._delete_scrap:
            for link in links:
                scrap_count += self._delete_scrap_files(link)
            if self._clean_source_scrap and not self._is_protected(src_path):
                scrap_count += self._delete_scrap_files(src_path)

        # 3. 删除转移记录 + 4. 联动删除种子
        hash_str = self._delete_transfer_history(src)
        if self._delete_torrents:
            self._delete_torrent(hash_str, src, links[0] if links else None)

        # 5. 清理空目录（含只剩刮削文件的目录）；保护目录下的目录一律不动。
        #    注意：源侧的「只剩刮削」清理也会删文件，因此同样受 clean_source_scrap
        #    约束 —— 否则下载目录只读会被这条路径绕过。
        empty_count = 0
        if self._clean_empty_dir:
            for p in list(links) + [src_path]:
                if self._is_protected(p.parent):
                    continue
                is_source = (p == src_path)
                if self._delete_scrap and (self._clean_source_scrap or not is_source):
                    # 这条路径删的是文件，计入刮削数（源侧未开开关时不执行）
                    scrap_count += self._purge_only_scrap_dirs(p.parent)
                empty_count += self._clean_empty_dirs(p.parent)

        self._stat["delete"] += 1
        self._stat["link"] += len(removed)
        # 记录本次删除（详情页可视化）
        self._append_deletion_log({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "src": _shorten(src),
            "links": len(removed),
            "link_paths": [_shorten(x) for x in removed[:5]],
            "scrap": scrap_count,
            "empty_dirs": empty_count,
            "history": (hash_str is not None) if self._delete_history else None,
            "torrent": bool(hash_str) if self._delete_torrents else None,
            "empty": self._clean_empty_dir,
            "reason": "源文件删除",
        })
        if self._notify:
            lines = [f"🗂️ 源文件：{src}"]
            if removed:
                lines.append("🔗 已删除软链接：")
                lines.extend(f"　　{x}" for x in removed)
            else:
                lines.append("🔗 未找到对应软链接")
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
                text="⏰ 延迟删除完成\n\n" + "\n".join(lines),
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
            wanted = {str(src)}
            if link:
                wanted.add(str(link))
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
                    if str(Path(save) / name) in wanted:
                        tid = getattr(t, "hash", None) or getattr(t, "hashString", None)
                        if tid:
                            inst.delete_torrents(delete_file=False, ids=[tid])
                            logger.info(f"软链接监控：按路径匹配删除种子 {name}（{tid}）")
                        return
        except Exception as e:
            logger.error(f"软链接监控：联动删除种子失败 {src}：{e}")

    def _delete_scrap_files(self, path: Path) -> int:
        """清理与 path 同名的刮削文件 / 刮削目录，返回删除条目数。

        匹配规则：条目名 == 媒体名，或为「媒体名 + -._ 分隔的附加部分」
        （如 movie-poster.jpg / movie.zh.srt / movie.trickplay），
        因此「电影AB」不会被「电影A」误伤。
        """
        parent = path.parent
        if not parent.is_dir():
            return 0
        prefix = path.name.rsplit(".", 1)[0] if "." in path.name else path.name
        if not prefix:
            return 0
        pattern = re.compile(r"^%s([-._].*)?$" % re.escape(prefix))
        deleted = 0
        try:
            for item in parent.iterdir():
                if item == path:
                    continue
                stem = item.name.rsplit(".", 1)[0] if "." in item.name else item.name
                if not pattern.match(stem):
                    continue
                if self._is_excluded(item):
                    continue
                suffix = item.suffix.lower()
                try:
                    if item.is_dir() and not item.is_symlink():
                        if suffix in SCRAP_DIR_SUFFIXES:
                            shutil.rmtree(item, ignore_errors=True)
                            deleted += 1
                            logger.info(f"软链接监控：已删除刮削目录 {item}")
                    elif suffix in SCRAP_EXTENSIONS:
                        item.unlink()
                        deleted += 1
                        logger.info(f"软链接监控：已删除刮削文件 {item}")
                except OSError as e:
                    logger.error(f"软链接监控：删除刮削文件 {item} 失败：{e}")
        except OSError as e:
            logger.error(f"软链接监控：遍历刮削文件失败 {parent}：{e}")
        return deleted

    def _is_only_scrap(self, path: Path) -> bool:
        """目录内是否只剩刮削文件 / 刮削目录（说明媒体本体已不在）。"""
        try:
            for item in path.iterdir():
                if item.is_dir() and not item.is_symlink():
                    if item.suffix.lower() not in SCRAP_DIR_SUFFIXES:
                        return False
                elif item.suffix.lower() not in SCRAP_EXTENSIONS:
                    return False
        except OSError:
            return False
        return True

    def _purge_only_scrap_dirs(self, path: Optional[Path]) -> int:
        """自下而上清理「只剩刮削文件」的目录，直到遇到根或保护目录。返回删除条目数。"""
        deleted = 0
        while path is not None:
            if self._is_protected(path) or self._is_stop_dir(path) or not path.is_dir():
                return deleted
            if not self._is_only_scrap(path):
                return deleted
            try:
                for item in list(path.iterdir()):
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink()
                    deleted += 1
                    logger.info(f"软链接监控：已清理残留刮削 {item}")
            except OSError as e:
                logger.error(f"软链接监控：清理残留刮削失败 {path}：{e}")
                return deleted
            path = path.parent
        return deleted

    def _clean_empty_dirs(self, path: Optional[Path]) -> int:
        """自下而上清理空目录，遇到收尾边界或保护目录停止。返回删除目录数。"""
        removed = 0
        while path is not None:
            if self._is_protected(path) or self._is_stop_dir(path) or not path.is_dir():
                return removed
            try:
                if any(path.iterdir()):
                    return removed
                path.rmdir()
                removed += 1
                logger.info(f"软链接监控：已清理空目录 {path}")
            except OSError:
                return removed
            path = path.parent
        return removed

    # -------------------------------------------------------------- 工具方法

    @staticmethod
    def _split_dir_conf(line: str) -> Tuple[str, Optional[Path]]:
        """解析「下载目录:软链接目录」，兼容 Windows 盘符。"""
        if os.name == "nt" and line.count(":") > 1:
            parts = [line.split(":")[0] + ":" + line.split(":")[1],
                     ":".join(line.split(":")[2:])]
        else:
            parts = line.split(":")
        src = parts[0].strip()
        target = Path(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
        return src, target

    def _is_stop_dir(self, path) -> bool:
        """是否到达收尾边界：下载目录根 或 软链接目录根。"""
        key = str(path)
        if key in self._dirconf:
            return True
        return any(key == str(v) for v in self._dirconf.values())

    def _drop_self_excluded(self, dirconf: Dict[str, Path]) -> Dict[str, Path]:
        """保留占位：保护目录不再影响监控，故无需剔除任何监控项。

        历史背景：早期实现把「不删除目录」当作「不监控目录」，导致把监控目录
        填进去时删除事件静默失灵。现已改为「保护目录」语义——照常监控、只是不删，
        因此这里只做存在性告警。
        """
        for src, link_dir in dirconf.items():
            if self._is_protected(src):
                logger.info(
                    f"软链接监控：下载目录 {src} 位于保护目录中 —— 仍会照常监控，"
                    f"但本插件不会删除其中的任何文件。"
                )
        return dirconf

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
        """路径是否位于某个「保护目录 / 不删除目录」之下。"""
        if not self._exclude_dirs:
            return False
        for line in self._exclude_dirs.splitlines():
            line = line.strip()
            if line and self._is_same_or_child(path, line):
                return True
        return False

    def _is_protected(self, path) -> bool:
        """路径是否受保护（插件绝不删除它）。等价于 _is_excluded，保留语义化名称。"""
        return self._is_excluded(path)

    def status_text(self) -> str:
        """运行状态描述。"""
        return (
            f"监控目录 {len(self._dirconf)} 个｜快照 {len(self._snapshot)} 个文件｜"
            f"待清理 {len(self._deletion_queue)} 项｜已删链接 {self._stat['link']}、"
            f"已处理 {self._stat['delete']} 次、孤儿清理 {self._stat['sweep']}、"
            f"失败 {self._stat['fail']}"
        )

    # -------------------------------------------------------------- 宿主接口

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令与事件。"""
        return [{
            "cmd": "/symlink_monitor",
            "event": EventType.PluginAction,
            "desc": "软链接监控全量清理孤儿链接",
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
            title="🧹 软链接监控完成",
            text=self.status_text() + f"\n本次清理 {count} 个孤儿软链接",
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
                "description": "全量清理一次：删除所有指向下载目录的孤儿软链接",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "运行状态",
                "description": "返回当前监控状态",
            },
            {
                "path": "/deletions",
                "endpoint": self.api_deletions,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "最近删除记录",
                "description": "返回最近 N 条软链接清理记录（默认 20 条）",
            },
            {
                "path": "/clear_log",
                "endpoint": self.api_clear_log,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "清空删除记录",
                "description": "清空删除记录列表（不影响其它数据）",
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
            "link_dirs": [str(v) for v in self._dirconf.values()],
            "exclude_dirs": [x.strip() for x in self._exclude_dirs.splitlines() if x.strip()],
            "status": self.status_text(),
            "deletion_log_count": self._log_count(),
        }}

    def api_deletions(self, limit: int = DELETION_LOG_SHOW):
        """返回最近的删除记录。"""
        try:
            limit = max(1, min(DELETION_LOG_LIMIT, int(limit)))
        except (TypeError, ValueError):
            limit = DELETION_LOG_SHOW
        return {"success": True, "data": self._tail_delete_log(limit)}

    def api_clear_log(self):
        """清空删除记录。"""
        self._clear_delete_log()
        return {"success": True, "message": "删除记录已清空"}

    def get_service(self) -> List[Dict[str, Any]]:
        """注册周期服务（插件重载后仍能继续清理到期的延迟任务）。"""
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
        """详情页：状态卡片 + 最近 20 条删除记录（表格可视化）。"""
        rows = self._tail_delete_log(DELETION_LOG_SHOW)
        return [
            {
                "component": "div",
                "props": {"class": "pa-2"},
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-3",
                            "title": "🧹 软链接监控",
                            "text": ("本插件不建立软链接，只做清理。\n"
                                     "① 删除下载目录里的文件 → 清理软链接目录中指向它的软链接；\n"
                                     "② 删除软链接目录里的链接 → 不会影响下载目录。\n"
                                     "「保护目录」里的内容仍会被监控，但插件绝不删除它们。"),
                        },
                    },
                    {
                        "component": "VRow",
                        "props": {"class": "mb-2"},
                        "content": [
                            self._stat_card("已删软链接", self._stat["link"], "mdi-link-variant-off", "primary"),
                            self._stat_card("已处理批次", self._stat["delete"], "mdi-check-circle-outline", "success"),
                            self._stat_card("孤儿清理", self._stat["sweep"], "mdi-broom", "info"),
                            self._stat_card("失败", self._stat["fail"], "mdi-alert-circle-outline",
                                            "error" if self._stat["fail"] else "secondary"),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "secondary",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mb-3",
                            "text": self.status_text(),
                        },
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-subtitle-1 font-weight-bold mb-2"},
                        "text": f"🗒️ 最近删除记录（最新 {DELETION_LOG_SHOW} 条 / 共 {self._log_count()} 条）",
                    },
                    self._build_log_table(rows),
                ],
            }
        ]

    def _build_log_table(self, rows: List[dict]) -> dict:
        """把删除记录渲染成表格。"""
        items = []
        for r in rows:
            links = r.get("links") or 0
            scrap = r.get("scrap") or 0
            dirs = r.get("empty_dirs") or 0
            flags = []
            if links:
                flags.append(f"🔗软链{links}")
            if scrap:
                flags.append(f"🖼️刮削{scrap}")
            if dirs:
                flags.append(f"📁目录{dirs}")
            if r.get("history"):
                flags.append("📝记录")
            if r.get("torrent"):
                flags.append("🌱种子")
            items.append({
                "time": r.get("time", ""),
                "reason": r.get("reason", "源文件删除"),
                "links": links,
                "scrap": scrap,
                "dirs": dirs,
                "src": r.get("src", ""),
                "detail": " ".join(flags) if flags else "-",
            })
        if not items:
            items = [{"time": "-", "reason": "暂无记录", "links": 0, "scrap": 0,
                      "dirs": 0, "src": "插件尚未执行过清理，或记录已被清空", "detail": "-"}]
        return {
            "component": "VDataTableVirtual",
            "props": {
                "class": "text-sm",
                "headers": [
                    {"title": "时间", "key": "time", "sortable": True, "width": "150px"},
                    {"title": "类型", "key": "reason", "sortable": True, "width": "100px"},
                    {"title": "软链", "key": "links", "sortable": True, "width": "64px"},
                    {"title": "刮削", "key": "scrap", "sortable": True, "width": "64px"},
                    {"title": "目录", "key": "dirs", "sortable": True, "width": "64px"},
                    {"title": "源文件", "key": "src", "sortable": False},
                    {"title": "联动", "key": "detail", "sortable": False, "width": "220px"},
                ],
                "items": items,
                "height": "30rem",
                "density": "compact",
                "fixed-header": True,
                "hover": True,
                "no-data-text": "暂无删除记录",
            },
        }

    @staticmethod
    def _stat_card(title: str, value, icon: str, color: str) -> dict:
        """统计小卡片。"""
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [{
                "component": "VCard",
                "props": {"variant": "tonal", "color": color, "class": "pa-2"},
                "content": [{
                    "component": "div",
                    "props": {"class": "d-flex align-center"},
                    "content": [
                        {"component": "VIcon", "props": {"color": color, "class": "mr-2"}, "text": icon},
                        {
                            "component": "div",
                            "content": [
                                {"component": "div", "props": {"class": "text-caption"}, "text": title},
                                {"component": "div", "props": {"class": "text-h6"}, "text": str(value)},
                            ],
                        },
                    ],
                }],
            }],
        }

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
                            self._switch("delete_scrap", "清理软链接目录刮削", 12, 3),
                            self._switch("clean_source_scrap", "清理下载目录刮削", 12, 3),
                            self._switch("delete_history", "删除转移记录", 12, 3),
                            self._switch("delete_torrents", "联动删除种子", 12, 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
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
                                        "label": "监控目录（下载目录:软链接目录）",
                                        "rows": 5,
                                        "placeholder": "每一行一条，例如：\n"
                                                       "/Movies3rd/Data3:/Movies3rd/Link3\n\n"
                                                       "下载目录中的文件被删除后，"
                                                       "会删除软链接目录中指向它的软链接。",
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
                                        "label": "保护目录（不删除目录）",
                                        "rows": 3,
                                        "placeholder": "每一行一个目录；这些目录里的内容仍会被监控，"
                                                       "但本插件绝不会删除它们\n"
                                                       "例如：/Movies3rd/Link3/珍藏",
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
                                        "title": "两个方向的行为",
                                        "text": "① 删除下载目录里的文件 → 清理软链接目录里指向它的软链接；"
                                                "② 删除软链接目录里的链接 → 不会影响下载目录。\n"
                                                "「保护目录」里的内容仍会被监控，但插件绝不删除它们。",
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
                                        "text": "本插件只删除软链接，绝不会删除软链接目录中的真实文件；"
                                                "也不处理指向其他位置的无关链接。"
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
            "clean_source_scrap": False,
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

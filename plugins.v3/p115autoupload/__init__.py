"""115 自动上传插件：监听整理完成事件，把新入库文件上传到 115 网盘。"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from app.schemas import NotificationType
from app.schemas.types import EventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .uploader import P115Uploader, VIDEO_SUFFIXES

DEFAULT_MAPPINGS = (
    "/Movies1st/Link1/ => /媒体库/电影/\n"
    "/Movies3rd/Link3/ => /媒体库/电影/\n"
    "/TVseries2nd/Link2/ => /媒体库/电视剧/"
)

# 未配置 Cookie 时尝试复用的本地登录态文件
COOKIE_FALLBACK = "/config/temp/115_cookie.json"

# 本地登录态文件也不存在时，复用同账号 115 存储插件里保存的登录态
COOKIE_SOURCE_PLUGIN = "P115Disk"


class P115AutoUpload(_PluginBase):
    """115 自动上传插件主类。"""

    plugin_name = "115自动上传"
    plugin_desc = (
        "监听 MoviePilot 整理完成事件，把新入库的影视文件自动上传到 115 网盘；"
        "上传前校验 115 是否已存在，重复时不上传并发送提示。"
    )
    plugin_icon = (
        "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/"
        "src/assets/images/misc/u115.png"
    )
    plugin_version = "1.1.2"
    plugin_label = "云盘,上传,115"
    plugin_author = "sanqianxingluo"
    plugin_config_prefix = "p115autoupload_"
    plugin_order = 100
    auth_level = 1

    _enabled = False
    _notify = True
    _dedup = True
    _include_sidecar = True
    _max_workers = 1
    _max_fail_count = 3
    _mappings = DEFAULT_MAPPINGS
    _uploader: Optional[P115Uploader] = None
    _executor: Optional[ThreadPoolExecutor] = None

    def __init__(self) -> None:
        """初始化插件运行时状态。"""
        super().__init__()
        self._lock = threading.Lock()
        self._pending: set[str] = set()

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并初始化上传器与后台线程池。"""
        self.stop_service()
        self._enabled = False
        self._uploader = None
        config = config or {}
        self._notify = bool(config.get("notify", True))
        self._dedup = bool(config.get("dedup", True))
        self._include_sidecar = bool(config.get("include_sidecar", True))
        self._max_workers = max(1, min(4, int(config.get("max_workers") or 1)))
        self._max_fail_count = max(
            0, min(20, int(config.get("max_fail_count", 3) or 0))
        )
        self._mappings = str(config.get("mappings") or DEFAULT_MAPPINGS).strip()
        if not config.get("enabled"):
            return

        cookie = str(config.get("cookie") or "").strip()
        if not cookie:
            cookie = self._load_fallback_cookie()
            if cookie:
                logger.info("115自动上传：Cookie 为空，已使用本地登录态文件")
        if not cookie:
            cookie = self._load_disk_cookie()
            if cookie:
                logger.info(
                    f"115自动上传：Cookie 为空，已复用 {COOKIE_SOURCE_PLUGIN} 的登录态"
                )
        if not cookie:
            logger.error(
                "115自动上传：未获取到 115 Cookie（配置为空、本地登录态文件不存在、"
                f"无法复用 {COOKIE_SOURCE_PLUGIN}），插件不会执行上传"
            )
            self._notify_text(
                "115 自动上传 - 缺少 Cookie",
                "未获取到 115 Cookie：插件配置为空、本地登录态文件不存在，"
                f"也无法复用 {COOKIE_SOURCE_PLUGIN} 的登录态。\n"
                "插件已停止上传，请在插件配置中填写 115 Cookie 或完成 115 登录后重载插件。",
            )
            return
        if not self._mappings:
            logger.warning("115自动上传：未配置路径映射，插件不会执行上传")
            return

        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="p115upload"
        )
        self._uploader = P115Uploader(
            cookie=cookie,
            mappings=self._mappings,
            dedup=self._dedup,
            include_sidecar=self._include_sidecar,
            max_fail_count=self._max_fail_count,
            fail_counts=self.get_data("fail_counts") or {},
        )
        self._enabled = True
        logger.info("115自动上传：插件已启用")

    @staticmethod
    def _load_fallback_cookie() -> str:
        """从本地登录态文件读取 Cookie，读取失败时返回空字符串。"""
        try:
            if not os.path.exists(COOKIE_FALLBACK):
                return ""
            with open(COOKIE_FALLBACK, encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                return "; ".join(f"{key}={value}" for key, value in data.items())
        except Exception as err:  # noqa: BLE001
            logger.warning(f"115自动上传：读取本地登录态失败：{err}")
        return ""

    @staticmethod
    def _load_disk_cookie() -> str:
        """复用 115 存储插件配置中的登录态，读取失败时返回空字符串。"""
        try:
            from app.sdk.plugin.manager import PluginManager

            conf = PluginManager().get_plugin_config(COOKIE_SOURCE_PLUGIN) or {}
            return str(conf.get("cookie") or "").strip()
        except Exception as err:  # noqa: BLE001
            logger.warning(
                f"115自动上传：读取 {COOKIE_SOURCE_PLUGIN} 登录态失败：{err}"
            )
        return ""

    def get_state(self) -> bool:
        """返回插件是否已启用并完成初始化。"""
        return bool(self._enabled and self._uploader is not None)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册详情页使用的 API。"""
        return [
            {
                "path": "/reset_failures",
                "endpoint": self.api_reset_failures,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "清空失败次数记录",
                "description": "清空后，此前因失败次数达到上限而停止上传的文件会重新尝试",
            }
        ]

    def api_reset_failures(self) -> Dict[str, Any]:
        """清空失败次数记录，让被放弃的文件重新参与上传。"""
        if self._uploader is not None:
            self._uploader.fail_counts.clear()
        self.save_data("fail_counts", {})
        logger.info("115自动上传：已清空失败次数记录")
        return {"success": True, "message": "失败次数记录已清空"}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    self._section(
                        "基础设置",
                        "开关类选项，保存后立即生效。",
                        [
                            {
                                "component": "VRow",
                                "props": {"class": "mt-1"},
                                "content": [
                                    self._switch("enabled", "启用插件", 12, 6, 3),
                                    self._switch("notify", "发送通知", 12, 6, 3),
                                    self._switch("dedup", "上传前查重", 12, 6, 3),
                                    self._switch(
                                        "include_sidecar", "上传附属文件", 12, 6, 3
                                    ),
                                ],
                            }
                        ],
                    ),
                    self._section(
                        "上传策略",
                        "控制并发数量与失败处理方式。",
                        [
                            {
                                "component": "VRow",
                                "content": [
                                    self._text(
                                        "max_workers",
                                        "上传并发数",
                                        "取值 1-4，建议保持 1，避免触发 115 风控",
                                        12,
                                        6,
                                        {"type": "number", "min": 1, "max": 4},
                                    ),
                                    self._text(
                                        "max_fail_count",
                                        "失败次数上限",
                                        "同一文件累计失败达到该次数后不再上传并通知；填 0 表示一直重试",
                                        12,
                                        6,
                                        {"type": "number", "min": 0, "max": 20},
                                    ),
                                ],
                            }
                        ],
                    ),
                    self._section(
                        "115 连接",
                        f"Cookie 留空时依次读取 {COOKIE_FALLBACK} 与 "
                        f"{COOKIE_SOURCE_PLUGIN} 的登录态。",
                        [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "cookie",
                                    "label": "115 Cookie",
                                    "type": "password",
                                    "variant": "outlined",
                                    "density": "comfortable",
                                    "class": "mb-4",
                                    "placeholder": "UID=...; CID=...; SEID=...; KID=...",
                                    "persistent-hint": True,
                                    "hint": f"留空时读取 {COOKIE_FALLBACK}，"
                                    f"再复用 {COOKIE_SOURCE_PLUGIN} 的登录态",
                                },
                            }
                        ],
                    ),
                    self._section(
                        "路径映射",
                        "每行一条「本地前缀 => 115 前缀」，前缀越具体越优先。",
                        [
                            {
                                "component": "VTextarea",
                                "props": {
                                    "model": "mappings",
                                    "label": "路径映射",
                                    "rows": 6,
                                    "variant": "outlined",
                                    "density": "comfortable",
                                    "class": "mb-2",
                                    "placeholder": "/本地库前缀/ => /115目录/",
                                    "persistent-hint": True,
                                    "hint": "支持 => 或 -> 连接；本地链接目录与真实目录都会按映射镜像到 115",
                                },
                            }
                        ],
                    ),
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mt-2",
                            "text": (
                                "整理完成后自动上传对应文件。115 已存在同版本时不上传并通知；"
                                "只缺部分集数时仅补传缺失文件；同一文件失败次数达到上限后停止上传，"
                                "可在插件详情页清空失败记录后重新尝试。"
                            ),
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "dedup": True,
            "include_sidecar": True,
            "cookie": "",
            "mappings": DEFAULT_MAPPINGS,
            "max_workers": 1,
            "max_fail_count": 3,
        }

    @staticmethod
    def _section(title: str, subtitle: str, content: List[dict]) -> Dict[str, Any]:
        """构造带标题与说明的分区卡片，避免各表单项挤在一起。"""
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-6"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-subtitle-1 pb-0"},
                    "text": title,
                },
                {
                    "component": "VCardText",
                    "props": {"class": "pt-3"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "class": "text-caption text-medium-emphasis mb-4"
                            },
                            "text": subtitle,
                        },
                        *content,
                    ],
                },
            ],
        }

    @staticmethod
    def _switch(
        model: str, label: str, cols: int = 12, sm: int = 6, md: int = 3
    ) -> Dict[str, Any]:
        """构造一个开关表单项。"""
        return {
            "component": "VCol",
            "props": {"cols": cols, "sm": sm, "md": md, "class": "py-2"},
            "content": [
                {
                    "component": "VSwitch",
                    "props": {
                        "model": model,
                        "label": label,
                        "density": "comfortable",
                        "hide-details": True,
                        "color": "primary",
                    },
                }
            ],
        }

    @staticmethod
    def _text(
        model: str,
        label: str,
        hint: str,
        cols: int = 12,
        md: int = 6,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构造一个带说明的文本输入表单项。"""
        props: Dict[str, Any] = {
            "model": model,
            "label": label,
            "variant": "outlined",
            "density": "comfortable",
            "class": "mb-4",
            "persistent-hint": True,
            "hint": hint,
        }
        props.update(extra or {})
        return {
            "component": "VCol",
            "props": {"cols": cols, "md": md, "class": "py-1"},
            "content": [{"component": "VTextField", "props": props}],
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页，展示最近一次上传结果。"""
        if not self.get_state():
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "插件未启用或未完成初始化，请检查配置。",
                    },
                }
            ]
        report = self.get_data("last_report") or {}
        if not report:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无上传记录，等待下一次整理完成事件。",
                    },
                }
            ]
        duplicates = report.get("duplicates") or []
        failures = report.get("failed") or []
        abandoned = report.get("abandoned") or []
        content: List[dict] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if not failures and not abandoned else "warning",
                    "variant": "tonal",
                    "class": "mb-4",
                    "text": (
                        f"最近运行：{report.get('finished_at', '-')}｜"
                        f"上传 {report.get('uploaded', 0)} 个 / "
                        f"{report.get('uploaded_gib', 0)} GiB｜"
                        f"跳过 {report.get('skipped', 0)} 个｜"
                        f"失败 {len(failures)} 个｜"
                        f"停止重试 {len(abandoned)} 个"
                    ),
                },
            }
        ]
        if duplicates:
            lines = [f"{item['dir']}（{item['reason']}）" for item in duplicates[:10]]
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "class": "mb-4",
                        "title": "115 已存在，未上传",
                        "text": "\n".join(lines),
                    },
                }
            )
        if failures:
            lines = [
                "{path}：{error}{suffix}".format(
                    path=item.get("path"),
                    error=item.get("error"),
                    suffix=(
                        f"（已失败 {item['count']} 次，停止重试）"
                        if item.get("stopped")
                        else (
                            f"（已失败 {item['count']} 次）"
                            if item.get("count")
                            else ""
                        )
                    ),
                )
                for item in failures[:5]
            ]
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "variant": "tonal",
                        "class": "mb-4",
                        "title": "上传失败",
                        "text": "\n".join(lines),
                    },
                }
            )
        if abandoned:
            lines = [
                f"{item['path']}（失败 {item['count']} 次）" for item in abandoned[:10]
            ]
            content.append(
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mb-4"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "props": {"class": "text-subtitle-1 pb-2"},
                            "text": f"已停止上传（{len(abandoned)} 个）",
                        },
                        {
                            "component": "VCardText",
                            "props": {"class": "pt-0"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-body-2 mb-3"},
                                    "text": "\n".join(lines),
                                },
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "warning",
                                        "variant": "tonal",
                                        "size": "small",
                                        "prepend-icon": "mdi-restart",
                                    },
                                    "text": "清空失败记录",
                                    "events": {
                                        "click": {
                                            "api": f"plugin/{self.__class__.__name__}/reset_failures",
                                            "method": "get",
                                        }
                                    },
                                },
                            ],
                        },
                    ],
                }
            )
        return content

    def stop_service(self) -> None:
        """停止后台线程池并释放上传器。"""
        executor = self._executor
        self._executor = None
        self._uploader = None
        self._enabled = False
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """整理完成后把目标文件提交到后台上传队列。"""
        if not self.get_state() or self._executor is None:
            return
        event_data = getattr(event, "event_data", None) or {}
        transferinfo = event_data.get("transferinfo")
        if not transferinfo or not getattr(transferinfo, "success", False):
            return
        paths = self._target_paths(transferinfo)
        if not paths:
            return
        if self._include_sidecar:
            expanded: set[str] = set()
            for item in paths:
                if os.path.isdir(item):
                    expanded.add(item)
                elif os.path.splitext(item)[1].lower() in VIDEO_SUFFIXES:
                    expanded.add(os.path.dirname(item))
                else:
                    expanded.add(item)
            paths = sorted(expanded)
        mediainfo = event_data.get("mediainfo")
        title = (
            getattr(mediainfo, "title_year", None)
            or getattr(mediainfo, "title", None)
            or getattr(getattr(transferinfo, "fileitem", None), "name", None)
            or "未知媒体"
        )
        key = f"{title}|{','.join(sorted(paths))}"
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
        try:
            self._executor.submit(self._run_upload, title, paths, key)
        except Exception as err:  # noqa: BLE001
            with self._lock:
                self._pending.discard(key)
            logger.error(f"115自动上传：提交上传任务失败：{err}")

    @staticmethod
    def _target_paths(transferinfo: Any) -> List[str]:
        """从整理结果中提取需要上传的目标路径。"""
        paths = [
            str(item)
            for item in (getattr(transferinfo, "file_list_new", None) or [])
            if item
        ]
        if paths:
            return paths
        for name in ("target_item", "target_diritem"):
            target = getattr(transferinfo, name, None)
            path = getattr(target, "path", None)
            if path:
                return [str(path)]
        return []

    def _run_upload(self, title: str, paths: List[str], key: str) -> None:
        """执行一次上传并记录结果、发送通知。"""
        uploader = self._uploader
        try:
            if uploader is None:
                return
            started = time.time()
            report = uploader.upload_paths(paths)
            report["title"] = title
            report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            report["elapsed_sec"] = round(time.time() - started, 1)
            self.save_data("fail_counts", uploader.fail_counts)
            self.save_data("last_report", report)
            logger.info(
                "115自动上传：{} 完成，上传 {} 个（{} GiB），跳过 {} 个，重复目录 {} 个，失败 {} 个，停止重试 {} 个".format(
                    title,
                    report.get("uploaded", 0),
                    report.get("uploaded_gib", 0),
                    report.get("skipped", 0),
                    len(report.get("duplicates") or []),
                    len(report.get("failed") or []),
                    len(report.get("abandoned") or []),
                )
            )
            self._notify_result(title, report)
        except Exception as err:  # noqa: BLE001
            logger.error(f"115自动上传：处理 {title} 失败：{err}")
            self._notify_text("115 自动上传失败", f"{title}：{err}")
        finally:
            with self._lock:
                self._pending.discard(key)

    def _notify_result(self, title: str, report: Dict[str, Any]) -> None:
        """按上传结果组装并发送通知。"""
        if not self._notify:
            return
        lines: List[str] = []
        if report.get("uploaded"):
            lines.append(
                f"已上传 {report['uploaded']} 个文件，共 {report['uploaded_gib']} GiB"
            )
        duplicates = report.get("duplicates") or []
        if duplicates:
            lines.append("115 已存在，未上传：")
            lines.extend(
                f"- {item['dir']}（{item['reason']}）" for item in duplicates[:10]
            )
        failures = report.get("failed") or []
        if failures:
            lines.append(f"上传失败 {len(failures)} 个文件：")
            lines.extend(
                "- {path}：{error}{suffix}".format(
                    path=item.get("path"),
                    error=item.get("error"),
                    suffix=(
                        f"（已连续失败 {item['count']} 次，达到上限后不再上传）"
                        if item.get("stopped")
                        else (
                            f"（已失败 {item['count']} 次）"
                            if item.get("count")
                            else ""
                        )
                    ),
                )
                for item in failures[:5]
            )
        abandoned = report.get("abandoned") or []
        if abandoned:
            lines.append(
                f"以下 {len(abandoned)} 个文件失败次数已达上限，已停止上传："
            )
            lines.extend(
                f"- {item['path']}（失败 {item['count']} 次）"
                for item in abandoned[:10]
            )
        if report.get("ignored"):
            lines.append(f"{report['ignored']} 个文件未命中路径映射，已忽略")
        if not lines:
            return
        self._notify_text(f"115 自动上传 - {title}", "\n".join(lines))

    def _notify_text(self, title: str, text: str) -> None:
        """发送插件通知，失败时仅记录日志。"""
        if not self._notify:
            return
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=text,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(f"115自动上传：发送通知失败：{err}")

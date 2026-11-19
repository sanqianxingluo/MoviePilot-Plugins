"""p115autoupload v1.1.3：逐文件补传与延迟上传的行为验证。

覆盖：
  · 视频齐全但 sidecar 缺失时仍逐个补传（不再整目录跳过）
  · 文件全部存在时不上传
  · 同名大小不同在开启查重时只提示、不上传
  · 关闭查重时同名不同大小照常上传
  · 延迟上传使用宿主一次性任务，同影片重复整理替换旧任务
  · 延迟为 0 时走线程池
  · 停止服务时取消排队任务
  · 延迟秒数按 0-604800 裁剪
"""

from __future__ import annotations

import importlib
import os
import threading

plugin_mod = importlib.import_module("app.plugins.p115autoupload")
uploader_mod = importlib.import_module("app.plugins.p115autoupload.uploader")


class FakeCache:
    """模拟 115 目录缓存，返回固定远端文件列表。"""

    def __init__(self, files):
        """保存远端文件名到大小的映射。"""
        self.files = files
        self.ensured = []

    def ensure(self, parts):
        """记录目标目录并返回固定 cid。"""
        self.ensured.append("/".join(parts))
        return 1

    def files_of(self, cid):
        """返回远端文件映射。"""
        return self.files

    def invalidate(self, cid):
        """忽略缓存失效。"""
        return None


def build_show(tmp_path):
    """构造含视频、字幕与 NFO 的剧集目录。"""
    season = tmp_path / "测试剧 (2020)" / "Season 1"
    season.mkdir(parents=True)
    for name, size in (("E01.mkv", 4096), ("E01.zh.ass", 32), ("E01.nfo", 16)):
        (season / name).write_bytes(b"x" * size)
    return str(tmp_path / "测试剧 (2020)")


def make_uploader(tmp_path, files, dedup=True):
    """构造不联网的上传器，并记录实际上传的文件名。"""
    up = uploader_mod.P115Uploader(
        cookie="x", mappings=f"{tmp_path}/ => /媒体库/电视剧/", dedup=dedup
    )
    up.cache = FakeCache(files)
    up.client = object()
    up._ensure_client = lambda: None
    uploaded = []
    up._upload_file = lambda local, cid, name: (uploaded.append(name) or True, "")
    return up, uploaded


def make_plugin():
    """绕过宿主组合根构造插件实例，仅准备延迟调度需要的状态。"""
    obj = object.__new__(plugin_mod.P115AutoUpload)
    obj._lock = threading.Lock()
    obj._pending = set()
    obj._scheduled = {}
    obj._delay_seconds = 0
    obj._executor = None
    obj._uploader = None
    obj._enabled = False
    obj._notify = False
    return obj


def test_missing_sidecar_uploaded(tmp_path):
    """视频已存在时应补传缺失的字幕与 NFO。"""
    show = build_show(tmp_path)
    up, uploaded = make_uploader(tmp_path, {"E01.mkv": 4096})
    report = up.upload_paths([show])
    assert sorted(uploaded) == ["E01.nfo", "E01.zh.ass"]
    assert report["uploaded"] == 2
    assert report["skipped"] == 1
    assert report["duplicates"] == []


def test_existing_all_skipped(tmp_path):
    """全部文件已存在且大小一致时不上传。"""
    show = build_show(tmp_path)
    up, uploaded = make_uploader(
        tmp_path, {"E01.mkv": 4096, "E01.nfo": 16, "E01.zh.ass": 32}
    )
    report = up.upload_paths([show])
    assert uploaded == []
    assert report["uploaded"] == 0
    assert report["skipped"] == 3


def test_name_conflict_reported_not_uploaded(tmp_path):
    """开启查重时同名不同大小只记录提示。"""
    show = build_show(tmp_path)
    up, uploaded = make_uploader(tmp_path, {"E01.mkv": 8192})
    report = up.upload_paths([show])
    assert sorted(uploaded) == ["E01.nfo", "E01.zh.ass"]
    assert any("E01.mkv" in item["reason"] for item in report["duplicates"])


def test_name_conflict_uploaded_when_dedup_off(tmp_path):
    """关闭查重时同名不同大小照常上传。"""
    show = build_show(tmp_path)
    up, uploaded = make_uploader(tmp_path, {"E01.mkv": 8192}, dedup=False)
    report = up.upload_paths([show])
    assert sorted(uploaded) == ["E01.mkv", "E01.nfo", "E01.zh.ass"]
    assert report["duplicates"] == []


def test_delay_schedule_and_replace(monkeypatch):
    """延迟上传注册宿主一次性任务，重复整理替换同一 ID 的任务。"""
    calls = []
    monkeypatch.setattr(
        plugin_mod,
        "add_plugin_once_job",
        lambda **kwargs: (calls.append(kwargs) or True),
    )
    plugin = make_plugin()
    plugin._delay_seconds = 60
    plugin._schedule_upload("测试剧", ["/x/Season 1"], "key-1")
    plugin._schedule_upload("测试剧", ["/x/Season 1"], "key-1")
    assert len(calls) == 2
    assert calls[0]["delay_seconds"] == 60
    assert calls[0]["pid"] == "P115AutoUpload"
    assert calls[0]["func_kwargs"]["key"] == "key-1"
    assert calls[0]["job_id"] == calls[1]["job_id"] == plugin_mod.P115AutoUpload._job_id("key-1")
    assert plugin._scheduled


def test_delay_zero_uses_executor(monkeypatch):
    """延迟为 0 时直接提交线程池，不注册调度任务。"""
    calls = []
    monkeypatch.setattr(
        plugin_mod, "add_plugin_once_job", lambda **kwargs: (calls.append(kwargs) or True)
    )
    submitted = []

    class FakeExecutor:
        """记录提交任务的线程池替身。"""

        def submit(self, func, *args):
            """记录被提交的函数与参数。"""
            submitted.append((func, args))

    plugin = make_plugin()
    plugin._executor = FakeExecutor()
    plugin._schedule_upload("测试剧", ["/x/Season 1"], "key-2")
    assert calls == []
    assert submitted and submitted[0][1][2] == "key-2"


def test_stop_service_removes_scheduled(monkeypatch):
    """停止服务时取消排队中的延迟任务。"""
    removed = []
    monkeypatch.setattr(
        plugin_mod,
        "remove_plugin_once_job",
        lambda pid, job_id: removed.append(job_id),
    )
    plugin = make_plugin()
    plugin._scheduled = {"abc": {"title": "t"}}
    plugin.stop_service()
    assert removed == ["abc"]
    assert plugin._scheduled == {}


def test_delay_seconds_clamped():
    """延迟秒数按 0 与 7 天上限裁剪。"""
    plugin = make_plugin()
    plugin.init_plugin({"enabled": False, "delay_seconds": -5})
    assert plugin._delay_seconds == 0
    plugin.init_plugin({"enabled": False, "delay_seconds": 99999999})
    assert plugin._delay_seconds == plugin_mod.MAX_DELAY_SECONDS

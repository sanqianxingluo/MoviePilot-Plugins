"""v3.1.0 新增功能验证：干跑模式、单次删除熔断、媒体库刷新、累计统计。

要点：
  · 干跑：只写记录、绝不执行删除；记录 reason 标注「干跑（未执行）」并带 dry_run 标记
  · 熔断：单轮消失数超过上限时不清理、且**不更新快照基线**（下轮可复核）
  · 熔断告警去重：同一波异常 10 分钟内只告警一次
  · 媒体库刷新：无媒体服务器时优雅退出，不影响主流程
  · 详情页含「累计」与「本次」两组统计卡片

纯本地临时目录，不碰真实媒体库/下载器。
"""
import importlib
import json
import os
import shutil
from pathlib import Path

mod = importlib.import_module("app.plugins.symlinkmonitor")
SymlinkMonitor = mod.SymlinkMonitor

ROOT = Path("/tmp/_sm_newfeat")
D, L = ROOT / "Data3", ROOT / "Link3"


def check(name, cond, extra=""):
    """断言条件为真，失败时带上用例名。"""
    assert cond, name + (f" | {extra}" if extra else "")


def mk(**over):
    cfg = {
        "enabled": False, "notify": False, "onlyonce": False,
        "monitor_dirs": f"{D}:{L}", "exclude_dirs": "", "exclude_keywords": "",
        "scan_interval": 10, "delayed_deletion": False, "delay_seconds": 30,
        "delete_scrap": True, "clean_source_scrap": False,
        "delete_history": False, "delete_torrents": False, "clean_empty_dir": True,
        # 新功能默认值
        "dry_run": False, "max_delete_per_scan": 0, "refresh_library": False,
    }
    cfg.update(over)
    p = SymlinkMonitor()
    p.init_plugin(cfg)
    p._history = None
    p._stat = {"delete": 0, "link": 0, "sweep": 0, "fail": 0}
    p._breaker_last_warn = None
    return p


def scene(n=1):
    """建 n 个「源文件 + 对应软链接」。"""
    shutil.rmtree(ROOT, ignore_errors=True)
    D.mkdir(parents=True)
    L.mkdir(parents=True)
    for i in range(n):
        f = f"片{i}.mkv"
        (D / f).write_text("x")
        (L / f).symlink_to(D / f)


def test_dry_run():
    """干跑模式：只记录、不删除。"""
    scene(1)
    p = mk(dry_run=True)
    p._clear_delete_log()
    p._snapshot = p._build_snapshot()
    check("快照含 1 个文件", len(p._snapshot) == 1, f"{len(p._snapshot)}")

    (D / "片0.mkv").unlink()      # 模拟源被删
    p._scan_once()
    p._flush_deletion_queue()

    check("干跑：软链接仍在（未被删除）", (L / "片0.mkv").is_symlink(),
          "链接被删了，干跑没生效")
    logs = p._tail_delete_log()
    check("干跑：写入了记录", len(logs) >= 1, f"{len(logs)} 条")
    if logs:
        r = logs[0]
        check("干跑：记录标记 dry_run", r.get("dry_run") is True, str(r.get("dry_run")))
        check("干跑：记录类型为「干跑（未执行）」", r.get("reason") == "干跑（未执行）",
              str(r.get("reason")))
        check("干跑：记录里写了本会删几个链接", r.get("links") == 1, f"links={r.get('links')}")

    # 关掉干跑后应能真删
    scene(1)
    p2 = mk(dry_run=False)
    p2._snapshot = p2._build_snapshot()
    (D / "片0.mkv").unlink()
    p2._scan_once()
    p2._flush_deletion_queue()
    check("非干跑：软链接被真正删除", not os.path.lexists(L / "片0.mkv"),
          "链接还在，实际删除没生效")


def test_breaker():
    """熔断：单轮消失数超上限则跳过清理，且保住快照基线。"""
    scene(5)
    p = mk(max_delete_per_scan=2)
    p._clear_delete_log()
    p._snapshot = p._build_snapshot()
    check("快照含 5 个文件", len(p._snapshot) == 5, f"{len(p._snapshot)}")

    for i in range(5):           # 5 个全部消失 > 上限 2
        (D / f"片{i}.mkv").unlink()
    p._scan_once()
    p._flush_deletion_queue()

    check("熔断：所有软链接都还在", all((L / f"片{i}.mkv").is_symlink() for i in range(5)),
          "有链接被删了，熔断没起作用")
    check("熔断：快照基线未被覆盖（仍是 5）", len(p._snapshot) == 5,
          f"{len(p._snapshot)}（若为 0 说明基线被清，下轮无法复核）")
    check("熔断：计数已记录", p._stat.get("breaker", 0) >= 1, str(p._stat))
    check("熔断：未产生删除记录", len(p._tail_delete_log()) == 0,
          f"{len(p._tail_delete_log())} 条")

    # 告警去重：紧接着再触发一次，不应重复计数
    p._scan_once()
    check("熔断告警去重（10 分钟内只记一次）", p._stat.get("breaker", 0) == 1,
          f"breaker={p._stat.get('breaker')}")

    # 上限内应正常执行
    scene(2)
    p2 = mk(max_delete_per_scan=2)   # 2 个消失，不超过上限 2 → 应执行
    p2._snapshot = p2._build_snapshot()
    for i in range(2):
        (D / f"片{i}.mkv").unlink()
    p2._scan_once()
    p2._flush_deletion_queue()
    check("未超上限时正常清理", all(not os.path.lexists(L / f"片{i}.mkv") for i in range(2)),
          "上限内却没清理")


def test_refresh_library_graceful():
    """媒体库刷新：没有可用媒体服务器时优雅退出，不抛异常、不影响主流程。"""
    scene(1)
    p = mk(refresh_library=True)
    p._snapshot = p._build_snapshot()
    (D / "片0.mkv").unlink()
    p._scan_once()
    p._flush_deletion_queue()
    check("开启刷新后链接仍被正常删除", not os.path.lexists(L / "片0.mkv"))
    # 直接调用也不应抛异常（宿主的测试替身里没有配置媒体服务器）
    try:
        p._refresh_media_libraries()
        ok = True
        err = ""
    except Exception as e:
        ok = False
        err = f"{type(e).__name__}: {e}"
    check("无媒体服务器时刷新不抛异常", ok, err)


def test_lifetime_stats():
    """详情页：累计统计卡片 + 本次运行卡片 + 状态栏体现新配置。"""
    scene(1)
    p = mk(dry_run=True, max_delete_per_scan=9, refresh_library=True)
    p._clear_delete_log()
    p._snapshot = p._build_snapshot()
    (D / "片0.mkv").unlink()
    p._scan_once()
    p._flush_deletion_queue()

    lt = p._lifetime_stats()
    check("累计统计含 links", "links" in lt and lt["links"] >= 1, str(lt))
    check("累计统计含 dry 计数", lt.get("dry", 0) >= 1, str(lt))

    page = p.get_page()
    flat = json.dumps(page, ensure_ascii=False, default=str)
    check("页面含累计卡片", "累计删除软链接" in flat and "累计处理批次" in flat)
    check("页面含干跑计数卡片", "干跑记录" in flat)
    check("页面含本次运行卡片", "本次已删软链接" in flat)

    st = p.status_text()
    check("状态栏标注干跑中", "干跑中" in st, st)
    check("状态栏标注单次上限", "单次上限 9" in st, st)
    check("状态栏标注清理后刷新媒体库", "清理后刷新媒体库" in st, st)

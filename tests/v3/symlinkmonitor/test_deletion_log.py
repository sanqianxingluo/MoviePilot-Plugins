"""v2.2.0 验证：删除记录可视化（详情页 + API）。

要点：
  · 每次联动清理都写入一条记录（时间/源/链接数/刮削数/联动项）
  · 孤儿清理也记一条
  · 详情页 get_page 渲染出表格，且只展示最近 20 条
  · 记录落库持久化，重载插件后仍在
  · 记录有条数上限，超限丢最旧的

纯本地临时目录，不碰真实媒体库/下载器；插件数据写在 /config/plugins/SymlinkMonitor/。
"""
import importlib
import json
import shutil
from pathlib import Path

mod = importlib.import_module("app.plugins.symlinkmonitor")
SymlinkMonitor = mod.SymlinkMonitor

ROOT = Path("/tmp/_sm_log")
D, L = ROOT / "Data3", ROOT / "Link3"
results = []


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
    }
    cfg.update(over)
    p = SymlinkMonitor()
    p.init_plugin(cfg)
    p._history = None
    p._stat = {"delete": 0, "link": 0, "sweep": 0, "fail": 0}
    return p


def scene():
    shutil.rmtree(ROOT, ignore_errors=True)
    D.mkdir(parents=True)
    L.mkdir(parents=True)


# 清空上一轮记录，从干净状态开始


def test_deletion_log():
    """删除记录：落盘、上限、计数与列表同源、详情页与 API。"""
    scene()
    p = mk()
    p._clear_delete_log()
    check("清空后记录为 0", len(p._tail_delete_log()) == 0, f"{len(p._tail_delete_log())} 条")

    scene()
    for f in ("测试片.mkv", "测试片.nfo", "测试片-poster.jpg"):
        (D / f).write_text("x")
        (L / f).symlink_to(D / f)
    p = mk()
    p._snapshot = p._build_snapshot()
    (D / "测试片.mkv").unlink()          # 模拟源文件被删
    p._scan_once()
    p._flush_deletion_queue()
    logs = p._tail_delete_log()
    check("产生 1 条删除记录", len(logs) == 1, f"{len(logs)} 条")
    if logs:
        r = logs[0]
        check("记录含时间", bool(r.get("time")), r.get("time"))
        check("记录含源文件路径", "Test".lower() in r.get("src", "").lower()
              or "测试片" in r.get("src", ""), r.get("src"))
        check("记录链接数 = 1（软链接已删）", r.get("links") == 1, f"links={r.get('links')}")
        check("记录刮削数（软链侧 nfo/jpg + 只剩刮削目录内的残留）",
              r.get("scrap", 0) >= 1, f"scrap={r.get('scrap')}")
        check("记录含空目录清理数", "empty_dirs" in r, f"empty_dirs={r.get('empty_dirs')}")
        check("记录类型为「源文件删除」", r.get("reason") == "源文件删除", r.get("reason"))
        check("记录备注了具体链接路径", isinstance(r.get("link_paths"), list)
              and len(r["link_paths"]) >= 1, str(r.get("link_paths"))[:90])

    check("下载目录的 nfo 仍在", (D / "测试片.nfo").exists())
    check("下载目录的 jpg 仍在", (D / "测试片-poster.jpg").exists())

    scene()
    # 手工造一个孤儿：源文件不存在，但链接指向它
    orphan_target = D / "不存在的片子.mkv"
    (L / "孤儿.mkv").symlink_to(orphan_target)
    p = mk()
    n = p.sync_all()
    check("孤儿被清理 1 个", n == 1, f"n={n}")
    logs = p._tail_delete_log()
    check("孤儿清理写入记录", any(x.get("reason") == "孤儿清理" for x in logs),
          str([x.get("reason") for x in logs]))

    page = p.get_page()
    check("get_page 返回非空列表", isinstance(page, list) and len(page) > 0)
    flat = json.dumps(page, ensure_ascii=False, default=str)
    check("页面含统计卡片", "累计删除软链接" in flat and "累计处理批次" in flat)
    check("页面含本次运行卡片", "本次已删软链接" in flat and "本次处理批次" in flat)
    check("页面含表格组件", "VDataTableVirtual" in flat)
    check("表格含列头（时间/软链/刮削/目录）",
          all(k in flat for k in ("时间", "软链", "刮削", "目录", "源文件")))
    check("页面标注展示条数 = 20", "最近删除记录" in flat and "20" in flat)

    # 表格行数 = 记录数（不超过 20）
    def find_table(node):
        if isinstance(node, dict):
            if node.get("component") == "VDataTableVirtual":
                return node
            for v in node.values():
                r = find_table(v)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = find_table(v)
                if r:
                    return r
        return None


    tbl = find_table(page)
    check("找到表格组件", tbl is not None)
    if tbl:
        items = tbl["props"]["items"]
        check("表格行数与记录数一致", len(items) == len(p._tail_delete_log()),
              f"表格 {len(items)} 行 / 记录 {len(p._tail_delete_log())} 条")
        check("表格行含时间列", all("time" in it for it in items))
        check("空表也有占位行", len(items) >= 1)

    p._clear_delete_log()
    for i in range(30):
        p._append_deletion_log({
            "time": "2026-09-30 %02d:%02d:00" % (i // 60, i % 60),
            "src": f"/Movies3rd/Data3/片子{i}.mkv", "links": 1, "link_paths": [],
            "scrap": 1, "history": None, "torrent": None, "empty": True,
            "reason": "源文件删除",
        })
    shown = p._tail_delete_log(20)
    check("只取最近 20 条", len(shown) == 20, f"{len(shown)} 条")
    check("取到的是最新的（29 号在列）", any("片子29.mkv" in x.get("src", "") for x in shown))
    check("最旧的（0/1 号）未入选", not any("片子0.mkv" in x.get("src", "") for x in shown))
    tbl = find_table(p.get_page())
    if tbl:
        check("详情页表格也是 20 行", len(tbl["props"]["items"]) == 20,
              f"{len(tbl['props']['items'])} 行")

    for i in range(mod.DELETION_LOG_LIMIT + 30):
        p._append_deletion_log({
            "time": "2026-10-01 00:00:%02d" % (i % 60),
            "src": f"/tmp/超额{i}.mkv", "links": 1, "link_paths": [],
            "scrap": 0, "history": None, "torrent": None, "empty": False,
            "reason": "源文件删除",
        })
    check(f"内存记录不超过上限 {mod.DELETION_LOG_LIMIT}",
          len(p._deletion_log) <= mod.DELETION_LOG_LIMIT, f"{len(p._deletion_log)} 条")

    n_list = len(p._tail_delete_log(mod.DELETION_LOG_LIMIT))
    n_count = p._log_count()
    n_status = (p.api_status().get("data") or {}).get("deletion_log_count")
    check("_log_count 与列表长度一致", n_count == n_list, f"count={n_count} list={n_list}")
    check("api_status 计数与列表一致", n_status == n_list, f"status={n_status} list={n_list}")

    res = p.api_deletions()
    check("api_deletions 返回 success", res.get("success") is True)
    check("api_deletions 返回列表", isinstance(res.get("data"), list), f"{(len(res.get('data') or []))} 条")
    check("api_deletions 默认 20 条", len(res["data"]) == 20, f"{len(res['data'])} 条")
    check("api_status 含记录总数", "deletion_log_count" in (p.api_status().get("data") or {}))
    clr = p.api_clear_log()
    check("api_clear_log 生效", clr.get("success") and len(p._tail_delete_log()) == 0)

    p._append_deletion_log({
        "time": "2026-10-02 08:00:00", "src": "/Movies3rd/Data3/持久化测试.mkv",
        "links": 2, "link_paths": [], "scrap": 1, "history": True, "torrent": True,
        "empty": True, "reason": "源文件删除",
    })
    marker = p._tail_delete_log(1)[0].get("src")
    p2 = mk()   # 新实例 → init_plugin 会重新 _load_deletion_log
    logs2 = p2._tail_delete_log()
    check("重载后旧记录仍在", any("持久化测试" in x.get("src", "") for x in logs2),
          f"{len(logs2)} 条")
    check("重载后最新记录正确", logs2 and logs2[0].get("src") == marker, str(logs2[0].get("src")) if logs2 else "")

    try:
        path = Path(p2.get_data_path()) / "deletion_log.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write("这不是JSON\n")
            f.write("{坏的\n")
        rows = p2._tail_delete_log()
        check("坏行被跳过、不抛异常", isinstance(rows, list) and len(rows) >= 1, f"{len(rows)} 条")
    except Exception as e:
        check("坏行被跳过、不抛异常", False, str(e))

    # 清场
    try:
        p2._clear_delete_log()
    except Exception:
        pass
    shutil.rmtree(ROOT, ignore_errors=True)


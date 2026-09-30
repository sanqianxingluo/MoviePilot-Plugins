"""端到端验收测试（在 MoviePilot 容器内运行）。

覆盖：启用插件 → 建软链接 → 排除规则 → 增量建链 → 源删除后延迟联动清理
→ 不删除目录保护 → API 注册 → 停用，共 15 项断言。

用法（容器内）: python3 test_e2e_acceptance.py
"""
import json
import os
import shutil
import time
from pathlib import Path

from app.core.plugin import PluginManager

PID = "SymlinkMonitor"
# 测试目录落在 /config 下，跑完自动清理
ROOT = Path("/config/_smtest")
SRC, DST, KEEP = ROOT / "dl", ROOT / "lib", ROOT / "keep"

pm = PluginManager()
if ROOT.exists():
    shutil.rmtree(ROOT, ignore_errors=True)
SRC.mkdir(parents=True)
DST.mkdir(parents=True)
KEEP.mkdir(parents=True)

cfg = {
    "enabled": True, "notify": False, "onlyonce": False,
    "monitor_dirs": f"{SRC}:{DST}",
    "exclude_dirs": str(KEEP),
    "exclude_keywords": "sample",
    "scan_interval": 2, "delayed_deletion": True, "delay_seconds": 2,
    "delete_scrap": True, "delete_history": False,
    "delete_torrents": False, "clean_empty_dir": True,
}
pm.save_plugin_config(PID, cfg, force=True)
pm.reload_plugin(PID)
time.sleep(3)
print("启用后 state =", pm.get_plugin_state(PID))

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (f" | {extra}" if extra else ""))


# 1. 首次建链
(SRC / "电影A.mkv").write_text("x" * 10)
(SRC / "电影A.nfo").write_text("<x/>")
(SRC / "电影A-poster.jpg").write_text("img")
(SRC / "电影B.sample.mkv").write_text("x")
(KEEP / "电影C.mkv").write_text("x")
time.sleep(8)
check("监控目录建软链接", (DST / "电影A.mkv").is_symlink())
check("软链接指向正确",
      (DST / "电影A.mkv").exists() and (DST / "电影A.mkv").resolve() == (SRC / "电影A.mkv").resolve())
check("nfo 建软链接", (DST / "电影A.nfo").is_symlink())
check("排除关键词不建链", not (DST / "电影B.sample.mkv").exists())
check("不删除目录不建链", not (DST / "电影C.mkv").exists())

# 2. 新增文件（增量）
(SRC / "电影D.mkv").write_text("y" * 10)
time.sleep(8)
check("新增文件自动建链", (DST / "电影D.mkv").is_symlink())

# 3. 删除源文件 → 延迟确认 → 联动清理
(SRC / "电影A.mkv").unlink()
(SRC / "电影A.nfo").unlink()
(SRC / "电影A-poster.jpg").unlink()
time.sleep(1)
# 注意：源文件已删，软链接变成断链，必须用 is_symlink() 判断（exists() 会跟随链接返回 False）
check("延迟期内软链接仍在（延迟删除生效）", (DST / "电影A.nfo").is_symlink(),
      "delay=%ss scan=%ss" % (pm.get_plugin_config(PID).get("delay_seconds"),
                              pm.get_plugin_config(PID).get("scan_interval")))
time.sleep(12)
check("源文件删除后软链接被删", not (DST / "电影A.mkv").exists())
check("刮削文件软链接被删", not (DST / "电影A.nfo").exists())
check("残留刮削+空目录被清理", not (SRC / "电影A.nfo").exists())
check("保留未受影响的链接", (DST / "电影D.mkv").is_symlink())

# 4. 不删除目录保护
check("不删除目录原文件仍在", (KEEP / "电影C.mkv").exists())

# 5. API 注册
run_api = [a for a in pm.get_plugin_apis(PID) if a["path"].endswith("/run")]
check("「立即运行一次」API 已注册", bool(run_api),
      run_api[0]["path"] if run_api else "")
st_api = [a for a in pm.get_plugin_apis(PID) if a["path"].endswith("/status")]
check("状态 API 已注册", bool(st_api), st_api[0]["path"] if st_api else "")

# 6. 停用
cfg["enabled"] = False
pm.save_plugin_config(PID, cfg, force=True)
pm.reload_plugin(PID)
time.sleep(2)
check("插件可停用", pm.get_plugin_state(PID) is False)

shutil.rmtree(ROOT, ignore_errors=True)
print("已清理测试目录:", not ROOT.exists())

clean = {
    "enabled": False, "notify": True, "onlyonce": False,
    "monitor_dirs": "", "exclude_dirs": "", "exclude_keywords": "",
    "scan_interval": 10, "delayed_deletion": True, "delay_seconds": 30,
    "delete_scrap": True, "delete_history": True, "delete_torrents": True,
    "clean_empty_dir": True,
}
pm.save_plugin_config(PID, clean, force=True)
pm.reload_plugin(PID)
print("最终配置:", json.dumps(pm.get_plugin_config(PID), ensure_ascii=False))
print("最终状态 enabled =", pm.get_plugin_state(PID))

fails = [n for n, ok in results if not ok]
print(f"\n合计 {len(results)} 项，失败 {len(fails)}" + ("" if not fails else "：" + ", ".join(fails)))

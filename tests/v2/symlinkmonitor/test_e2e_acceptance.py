"""端到端验收测试（在 MoviePilot 容器内运行）。

验证「只监控删除、绝不建链」+「保护目录」语义：
  1. 启动后不改动已有软链接，也不为缺链的文件补链
  2. 下载目录新增文件 → **不建立任何软链接**（核心需求）
  3. 下载目录删除文件 → 延迟窗口内链还在 → 延迟后链被删除
  4. 方向一：删软链接目录里的链接 → 不波及下载目录（源只读）
  5. 空目录联动清理
  6. 软链接目录中的**真实文件**永不删除（安全底线）
  7. 「保护目录」里的链接不删，但其中的文件照常被监控
  8. 「立即运行一次」= 立刻清掉孤儿断链（不等延迟）
  9. API 注册 / 启用 / 停用

用法（容器内）: python3 test_e2e_acceptance.py
"""
import json
import shutil
import time
from pathlib import Path

from app.core.plugin import PluginManager

PID = "SymlinkMonitor"
ROOT = Path("/config/_smtest")
SRC, DST = ROOT / "dl", ROOT / "lib"
KEEP = DST / "珍藏"          # 保护目录：软链接目录下的子目录

pm = PluginManager()
if ROOT.exists():
    shutil.rmtree(ROOT, ignore_errors=True)
for d in (SRC, DST, KEEP):
    d.mkdir(parents=True)

# ---- 前置：模拟「别的工具已经建好的软链接」 ----
(SRC / "影片A.mkv").write_text("A" * 10)
(SRC / "影片A.nfo").write_text("<nfo/>")
(SRC / "影片A-poster.jpg").write_text("img")
(SRC / "影片B.mkv").write_text("B" * 10)
(SRC / "影片C.mkv").write_text("C" * 10)             # 故意没有链
(SRC / "影片K.mkv").write_text("K" * 10)
(DST / "影片A.mkv").symlink_to(SRC / "影片A.mkv")
(DST / "影片A.nfo").symlink_to(SRC / "影片A.nfo")
(DST / "影片A-poster.jpg").symlink_to(SRC / "影片A-poster.jpg")
(DST / "影片B.mkv").symlink_to(SRC / "影片B.mkv")
(DST / "真实文件.mkv").write_text("real")             # 真实文件，绝不能删
(KEEP / "影片K.mkv").symlink_to(SRC / "影片K.mkv")    # 位于保护目录中的链接

cfg = {
    "enabled": True, "notify": False, "onlyonce": False,
    "monitor_dirs": f"{SRC}:{DST}",
    "exclude_dirs": str(KEEP),
    "exclude_keywords": "",
    "scan_interval": 3, "delayed_deletion": True, "delay_seconds": 5,
    "delete_scrap": True, "clean_source_scrap": False,
    "delete_history": False, "delete_torrents": False, "clean_empty_dir": True,
}
pm.save_plugin_config(PID, cfg, force=True)
pm.reload_plugin(PID)
time.sleep(3)
obj = pm.running_plugins.get(PID)

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (f" | {extra}" if extra else ""))


check("插件已启用", pm.get_plugin_state(PID) is True)

# ---- 1. 启动后无副作用 ----
time.sleep(6)
check("已有的软链接被保留", (DST / "影片A.mkv").is_symlink())
check("真实文件未被删除", (DST / "真实文件.mkv").exists())
check("启动后未为缺链文件补链（不建软链接）", not (DST / "影片C.mkv").exists())

# ---- 2. 新增文件不建链 ----
(SRC / "影片D.mkv").write_text("D" * 10)
time.sleep(9)
check("新增文件不建立软链接（核心需求）", not (DST / "影片D.mkv").exists())
check("下载目录新增文件本体仍在", (SRC / "影片D.mkv").exists())

# ---- 3. 延迟删除 ----
(SRC / "影片A.mkv").unlink()
time.sleep(2)
check("延迟窗口内链接仍在（延迟删除生效）", (DST / "影片A.mkv").is_symlink(),
      "delay=5s scan=3s")
time.sleep(16)
check("源文件删除后软链接被删除",
      not (DST / "影片A.mkv").is_symlink() and not (DST / "影片A.mkv").exists())
check("链接侧的刮削链被清理", not (DST / "影片A.nfo").exists())
check("下载目录默认只读：源侧刮削未被删", (SRC / "影片A.nfo").exists()
      and (SRC / "影片A-poster.jpg").exists())
check("无关链接不受影响（影片B）", (DST / "影片B.mkv").is_symlink())

# ---- 4. 方向一：删软链接目录里的链接 → 不波及下载目录 ----
(SRC / "影片E.mkv").write_text("E" * 10)
(DST / "影片E.mkv").symlink_to(SRC / "影片E.mkv")
time.sleep(6)
(DST / "影片E.mkv").unlink()          # 手动删掉链接
time.sleep(12)
check("方向一：删链接后下载目录原文件仍在", (SRC / "影片E.mkv").exists())

# ---- 4b. 源目录只剩刮削时，源侧文件不应被删（v2.1.1 回归）----
(SRC / "只剩刮削").mkdir()
(SRC / "只剩刮削" / "裸片.mkv").write_text("N" * 10)
(SRC / "只剩刮削" / "裸片.nfo").write_text("<nfo/>")
(SRC / "只剩刮削" / "裸片-poster.jpg").write_text("img")
(DST / "只剩刮削").mkdir()
(DST / "只剩刮削" / "裸片.mkv").symlink_to(SRC / "只剩刮削" / "裸片.mkv")
(DST / "只剩刮削" / "裸片.nfo").symlink_to(SRC / "只剩刮削" / "裸片.nfo")
time.sleep(6)
(SRC / "只剩刮削" / "裸片.mkv").unlink()      # 只剩 nfo + jpg
time.sleep(18)
check("源目录只剩刮削时未被清理（下载目录只读）",
      (SRC / "只剩刮削" / "裸片.nfo").exists()
      and (SRC / "只剩刮削" / "裸片-poster.jpg").exists())
check("链接侧只剩刮削时被清理", not (DST / "只剩刮削" / "裸片.mkv").exists()
      and not (DST / "只剩刮削" / "裸片.nfo").exists())

# ---- 5. 真实文件与保护目录 ----
(SRC / "影片F.mkv").write_text("F" * 10)
(DST / "影片F.mkv").write_text("real-F")              # 同名真实文件
time.sleep(12)
check("软链接目录中的同名真实文件未被删除（安全底线）",
      (DST / "影片F.mkv").exists() and not (DST / "影片F.mkv").is_symlink())

(SRC / "影片K.mkv").unlink()          # 删除保护目录中链接的源文件
time.sleep(16)
check("保护目录里的链接未被清理（真保护）", (KEEP / "影片K.mkv").is_symlink())

# ---- 6. 空目录联动清理 ----
(SRC / "子目录").mkdir()
(SRC / "子目录" / "影片H.mkv").write_text("H" * 10)
(DST / "子目录").mkdir()
(DST / "子目录" / "影片H.mkv").symlink_to(SRC / "子目录" / "影片H.mkv")
time.sleep(6)
(SRC / "子目录" / "影片H.mkv").unlink()
time.sleep(18)
check("空目录被联动清理（下载侧）", not (SRC / "子目录").exists())
check("空目录被联动清理（链接侧）", not (DST / "子目录").exists())

# ---- 7. 立即运行一次：孤儿断链秒清 ----
(SRC / "影片G.mkv").write_text("G" * 10)
(DST / "影片G.mkv").symlink_to(SRC / "影片G.mkv")
time.sleep(6)
(SRC / "影片G.mkv").unlink()
n = obj.sync_all() if obj else -1
check("「立即运行一次」立刻清掉孤儿断链（不等延迟）",
      not (DST / "影片G.mkv").exists() and not (DST / "影片G.mkv").is_symlink(),
      f"sync_all 清理 {n} 个")

# ---- 8. API 注册 ----
apis = [a["path"] for a in pm.get_plugin_apis(PID)]
check("「立即运行一次」API 已注册", any(p.endswith("/run") for p in apis), str(apis))
check("状态 API 已注册", any(p.endswith("/status") for p in apis))
check("详情页可用", bool(obj and obj.get_page()))

# ---- 9. 停用 ----
cfg["enabled"] = False
pm.save_plugin_config(PID, cfg, force=True)
pm.reload_plugin(PID)
time.sleep(2)
check("插件可停用", pm.get_plugin_state(PID) is False)

# ---- 收尾：清理测试目录 + 恢复干净配置 ----
shutil.rmtree(ROOT, ignore_errors=True)
print("已清理测试目录:", not ROOT.exists())

clean = {
    "enabled": False, "notify": True, "onlyonce": False,
    "monitor_dirs": "", "exclude_dirs": "", "exclude_keywords": "",
    "scan_interval": 10, "delayed_deletion": True, "delay_seconds": 30,
    "delete_scrap": True, "clean_source_scrap": False,
    "delete_history": True, "delete_torrents": True, "clean_empty_dir": True,
}
pm.save_plugin_config(PID, clean, force=True)
pm.reload_plugin(PID)
print("最终配置:", json.dumps(pm.get_plugin_config(PID), ensure_ascii=False))
print("最终状态 enabled =", pm.get_plugin_state(PID))

fails = [n for n, ok in results if not ok]
print(f"\n合计 {len(results)} 项，失败 {len(fails)}" + ("" if not fails else "：" + ", ".join(fails)))
raise SystemExit(1 if fails else 0)

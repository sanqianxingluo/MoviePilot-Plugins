"""v2.0.1 回归测试：配置防呆 + 原有语义不回退。

纯本地临时目录，不碰真实媒体库，也不碰数据库/下载器。

仅使用插件自身的扫描/清理逻辑，不启动线程、不需要启用插件。
用法（容器内）: python3 test_regress_config_guard.py
"""
import importlib
import os
import shutil
from pathlib import Path

mod = importlib.import_module("app.plugins.symlinkmonitor")
SymlinkMonitor = mod.SymlinkMonitor

ROOT = Path("/config/_sm_regress")
D, L, KEEP = ROOT / "Data3", ROOT / "Link3", ROOT / "Link3" / "珍藏"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (f" | {extra}" if extra else ""))


def mk(exclude="", monitor=None):
    """构造实例并把配置灌进去（init_plugin 会覆盖实例属性，故只能从 config 走）。"""
    p = SymlinkMonitor()
    p.init_plugin({
        "enabled": False, "notify": False, "onlyonce": False,
        "monitor_dirs": monitor if monitor is not None else f"{D}:{L}",
        "exclude_dirs": exclude, "exclude_keywords": "",
        "scan_interval": 10,
        # 关闭延迟删除，让删除立即执行（延迟逻辑另外单独测）
        "delayed_deletion": False, "delay_seconds": 30,
        "delete_scrap": True, "delete_history": False,
        "delete_torrents": False, "clean_empty_dir": True,
    })
    p._history = None
    p._stat = {"delete": 0, "link": 0, "sweep": 0, "fail": 0}
    return p


shutil.rmtree(ROOT, ignore_errors=True)
D.mkdir(parents=True)
L.mkdir(parents=True)
KEEP.mkdir(parents=True)

print("=== 1. 防呆：历史配置里已把 Data3 填进「不删除目录」 ===")
p = mk(exclude=str(D))
check("被覆盖的监控项被剔除（dirconf 为空）", p._dirconf == {}, f"dirconf={p._dirconf}")

print("\n=== 2. 防呆：软链接目录被覆盖，同样剔除 ===")
p = mk(exclude=str(L))
check("软链接目录被覆盖时剔除该监控项", p._dirconf == {}, f"dirconf={p._dirconf}")

print("\n=== 3. 正确用法：只保护 Link3 下的子目录 ===")
p = mk(exclude=str(KEEP))
check("监控项保留", list(p._dirconf.keys()) == [str(D)], f"dirconf={p._dirconf}")
check("子目录保护仍生效", p._is_excluded(KEEP / "某片.mkv"))
check("下载目录本身不被排除", not p._is_excluded(D / "某片.mkv"))

print("\n=== 4. 语义未回退：删除 → 联动清链（不删除目录为空）===")
for f in ("片子Z.mkv", "片子Z.nfo"):
    (D / f).write_text("x")
    (L / f).symlink_to(D / f)
p = mk(exclude="")
p._snapshot = p._build_snapshot()
check("快照含 2 个文件", len(p._snapshot) == 2, f"{len(p._snapshot)}")
(D / "片子Z.mkv").unlink()
(D / "片子Z.nfo").unlink()
p._scan_once()
p._flush_deletion_queue()
check("检测到删除", p._stat["delete"] > 0, f"_stat={p._stat}")
check("软链接被清理", not (L / "片子Z.mkv").is_symlink())
check("链接侧刮削链被清理", not (L / "片子Z.nfo").exists())

print("\n=== 5. 语义未回退：不建链 ===")
shutil.rmtree(ROOT, ignore_errors=True)
D.mkdir(parents=True)
L.mkdir(parents=True)
p = mk(exclude="")
(D / "新片.mkv").write_text("x")
p._snapshot = p._build_snapshot()
p._scan_once()
check("新增文件后 Link3 无软链接", not (L / "新片.mkv").exists())
check("源文件本体在", (D / "新片.mkv").exists())

print("\n=== 6. 配置页告警存在 ===")
form, model = SymlinkMonitor().get_form()
flat = []


def walk(nodes):
    for n in nodes:
        if isinstance(n, dict):
            flat.append(n)
            walk(n.get("content") or [])


walk(form)
warns = [n for n in flat if n.get("component") == "VAlert"
         and n.get("props", {}).get("type") == "warning"]
check("存在 warning 级告警提示", len(warns) >= 1, f"{len(warns)} 条")
if warns:
    txt = warns[0]["props"].get("text", "")
    check("告警文案点明不要填监控目录", "不要填监控目录" in txt or "监控目录本身" in txt)

shutil.rmtree(ROOT, ignore_errors=True)
print("\n测试目录已清理:", not ROOT.exists())
fails = [n for n, ok in results if not ok]
print(f"合计 {len(results)} 项，失败 {len(fails)}" + ("" if not fails else "：" + ", ".join(fails)))

"""回归测试：保护目录语义 + 核心行为不回退。

对应 v2.1.0 的语义修正：把「不删除目录」从「不监控区」改成「保护区」——
里面的内容照常监控、照常触发联动清理，只是插件绝不删除它们。

纯本地临时目录，不碰真实媒体库/数据库/下载器。
仅使用插件自身的扫描/清理逻辑，不启动线程、不需要启用插件。
用法（容器内）: python3 test_regress_config_guard.py
"""
import importlib
import shutil
from pathlib import Path

mod = importlib.import_module("app.plugins.symlinkmonitor")
SymlinkMonitor = mod.SymlinkMonitor

ROOT = Path("/config/_sm_regress")
D, L = ROOT / "Data3", ROOT / "Link3"
KEEP = L / "珍藏"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (f" | {extra}" if extra else ""))


def mk(exclude="", **over):
    """实例属性只能经 init_plugin 的 config 传入（config 会覆盖属性）。"""
    cfg = {
        "enabled": False, "notify": False, "onlyonce": False,
        "monitor_dirs": f"{D}:{L}", "exclude_dirs": exclude, "exclude_keywords": "",
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
    KEEP.mkdir(parents=True)


scene()

print("=== 1. 保护目录不再等于「不监控」 ===")
(D / "片子Z.mkv").write_text("x")
(L / "片子Z.mkv").symlink_to(D / "片子Z.mkv")
p = mk(exclude=str(D))          # 故意把下载目录填进保护目录
p._snapshot = p._build_snapshot()
check("被保护的下载目录文件仍进快照（不再静默失灵）",
      len(p._snapshot) == 1, f"{len(p._snapshot)} 项")
(D / "片子Z.mkv").unlink()
p._scan_once()
p._flush_deletion_queue()
check("删除仍被检测到", p._stat["delete"] > 0, f"_stat={p._stat}")
check("软链接仍被清理", not (L / "片子Z.mkv").is_symlink())

print("\n=== 2. 保护 Link3 子目录：真保护 ===")
scene()
(D / "珍藏片.mkv").write_text("x")
(KEEP / "珍藏片.mkv").symlink_to(D / "珍藏片.mkv")
p = mk(exclude=str(KEEP))
check("监控项保留", list(p._dirconf.keys()) == [str(D)], f"dirconf={p._dirconf}")
check("子目录被识别为保护", p._is_protected(KEEP / "某片.mkv"))
check("下载目录不受保护", not p._is_protected(D / "某片.mkv"))
p._snapshot = p._build_snapshot()
(D / "珍藏片.mkv").unlink()
p._scan_once()
p._flush_deletion_queue()
check("删除被检测到", p._stat["delete"] > 0, f"_stat={p._stat}")
check("保护目录里的链接未被删", (KEEP / "珍藏片.mkv").is_symlink())

print("\n=== 3. 语义未回退：删除 → 联动清链 ===")
scene()
for f in ("片子A.mkv", "片子A.nfo"):
    (D / f).write_text("x")
    (L / f).symlink_to(D / f)
p = mk(exclude="")
p._snapshot = p._build_snapshot()
check("快照含 2 个文件", len(p._snapshot) == 2, f"{len(p._snapshot)}")
(D / "片子A.mkv").unlink()
(D / "片子A.nfo").unlink()
p._scan_once()
p._flush_deletion_queue()
check("检测到删除", p._stat["delete"] > 0, f"_stat={p._stat}")
check("软链接被清理", not (L / "片子A.mkv").is_symlink())
check("链接侧刮削链被清理", not (L / "片子A.nfo").exists())

print("\n=== 4. 语义未回退：不建链 ===")
scene()
p = mk(exclude="")
(D / "新片.mkv").write_text("x")
p._snapshot = p._build_snapshot()
p._scan_once()
check("新增文件后 Link3 无软链接", not (L / "新片.mkv").exists())
check("源文件本体在", (D / "新片.mkv").exists())

print("\n=== 5. 配置页结构完整 ===")
form, model = SymlinkMonitor().get_form()
flat = []


def walk(nodes):
    for n in nodes:
        if isinstance(n, dict):
            flat.append(n)
            walk(n.get("content") or [])


walk(form)
models = [n["props"]["model"] for n in flat if n.get("props", {}).get("model")]
missing = [k for k in model if k not in models]
check("默认模型字段都有对应表单项", missing == [], f"缺失={missing}")
check("无重复字段", len(models) == len(set(models)), f"{len(models)}/{len(set(models))}")
alerts = [n for n in flat if n.get("component") == "VAlert"]
check("配置页有说明提示", len(alerts) >= 1, f"{len(alerts)} 条")

shutil.rmtree(ROOT, ignore_errors=True)
print("\n测试目录已清理:", not ROOT.exists())
fails = [n for n, ok in results if not ok]
print(f"合计 {len(results)} 项，失败 {len(fails)}" + ("" if not fails else "：" + ", ".join(fails)))

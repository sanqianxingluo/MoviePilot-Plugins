"""v2.1.0 验证：保护目录语义 + 双向行为。

用户要的语义：
  · 「保护目录」= 插件绝不删这里的东西，但这里照常监控
  · 删下载目录 → 同步清软链接目录
  · 删软链接目录 → 不波及下载目录
  另外：默认不删下载目录里的任何文件（源数据只读）

纯本地临时目录，不碰真实媒体库/数据库/下载器。
"""
import importlib
import shutil
from pathlib import Path

mod = importlib.import_module("app.plugins.symlinkmonitor")
SymlinkMonitor = mod.SymlinkMonitor

ROOT = Path("/tmp/_sm_v210")
D, L = ROOT / "Data3", ROOT / "Link3"
KEEP = L / "珍藏"
results = []


def check(name, cond, extra=""):
    """断言条件为真，失败时带上用例名。"""
    assert cond, name + (f" | {extra}" if extra else "")


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
    for f in ("片子.mkv", "片子.nfo", "片子-poster.jpg"):
        (D / f).write_text("x")
        (L / f).symlink_to(D / f)


def test_protect_dirs_semantics():
    """保护目录语义与双向行为。"""
    scene()
    p = mk(exclude=str(KEEP))
    p._snapshot = p._build_snapshot()
    check("下载目录文件在快照里", any(k.endswith("片子.mkv") for k in p._snapshot),
          f"{len(p._snapshot)} 项")
    check("保护目录被正确识别", p._is_protected(KEEP / "任意.mkv"))
    check("下载目录不在保护范围", not p._is_protected(D / "片子.mkv"))

    scene()
    p = mk(exclude="")
    p._snapshot = p._build_snapshot()
    for f in ("片子.mkv", "片子.nfo", "片子-poster.jpg"):
        (L / f).unlink()
    p._scan_once()
    p._flush_deletion_queue()
    check("下载目录原片仍在", (D / "片子.mkv").exists())
    check("下载目录刮削仍在", (D / "片子.nfo").exists() and (D / "片子-poster.jpg").exists())
    check("没有触发任何删除动作", p._stat["delete"] == 0, f"_stat={p._stat}")

    scene()
    p = mk(exclude="")
    p._snapshot = p._build_snapshot()
    (D / "片子.mkv").unlink()
    p._scan_once()
    p._flush_deletion_queue()
    check("软链接被清理", not (L / "片子.mkv").is_symlink() and not (L / "片子.mkv").exists())
    check("链接侧刮削链被清理", not (L / "片子.nfo").exists())

    scene()
    p = mk(exclude="")
    p._snapshot = p._build_snapshot()
    (D / "片子.mkv").unlink()          # 只删正片，刮削故意留着
    p._scan_once()
    p._flush_deletion_queue()
    check("下载目录的刮削文件未被删（默认只读源）",
          (D / "片子.nfo").exists() and (D / "片子-poster.jpg").exists(),
          f"Data3 内容={sorted(x.name for x in D.iterdir())}")

    scene()
    p = mk(exclude="", clean_source_scrap=True)
    p._snapshot = p._build_snapshot()
    (D / "片子.mkv").unlink()
    p._scan_once()
    p._flush_deletion_queue()
    check("开启后下载目录刮削被清理",
          not (D / "片子.nfo").exists() and not (D / "片子-poster.jpg").exists())

    shutil.rmtree(ROOT, ignore_errors=True)
    D.mkdir(parents=True)
    L.mkdir(parents=True)
    KEEP.mkdir(parents=True)
    (D / "珍藏片.mkv").write_text("x")
    (KEEP / "珍藏片.mkv").symlink_to(D / "珍藏片.mkv")
    p = mk(exclude=str(KEEP))
    p._snapshot = p._build_snapshot()
    check("保护目录的文件仍进快照", any(k.endswith("珍藏片.mkv") for k in p._snapshot))
    (D / "珍藏片.mkv").unlink()
    p._scan_once()
    p._flush_deletion_queue()
    check("删除被检测到", p._stat["delete"] > 0, f"_stat={p._stat}")
    check("保护目录里的链接未被删", (KEEP / "珍藏片.mkv").is_symlink())

    scene()
    p = mk(exclude=str(D))
    p._snapshot = p._build_snapshot()
    check("快照非空（不再静默失灵）", len(p._snapshot) == 3, f"{len(p._snapshot)} 项")
    (D / "片子.mkv").unlink()
    p._scan_once()
    p._flush_deletion_queue()
    check("删除仍被检测到", p._stat["delete"] > 0, f"_stat={p._stat}")
    check("软链接被清理", not (L / "片子.mkv").is_symlink() and not (L / "片子.mkv").exists())
    check("受保护：下载目录自身内容未被插件删",
          (D / "片子.nfo").exists() and (D / "片子-poster.jpg").exists())

    page, model = SymlinkMonitor().get_form()
    flat = []


    def walk(nodes):
        for n in nodes:
            if isinstance(n, dict):
                flat.append(n)
                walk(n.get("content") or [])


    walk(page)
    models = [n["props"]["model"] for n in flat if n.get("props", {}).get("model")]
    check("新开关在表单里", "clean_source_scrap" in models, str(models))
    check("clean_empty_dir 仍在表单里", "clean_empty_dir" in models)
    missing = [k for k in model if k not in models]
    check("默认模型字段都有对应表单项", missing == [], f"缺失={missing}")
    check("clean_source_scrap 默认关闭", model.get("clean_source_scrap") is False)


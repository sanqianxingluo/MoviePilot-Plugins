"""插件单元测试（MoviePilot 容器内运行，不依赖启用的插件实例）。

只覆盖纯逻辑：路径解析、排除规则、刮削识别、快照过滤。
"""
import importlib
import shutil
import tempfile
from pathlib import Path

mod = importlib.import_module("app.plugins.symlinkmonitor")
SymlinkMonitor = mod.SymlinkMonitor

fails = []


def eq(name, got, want):
    ok = got == want
    print(("  PASS " if ok else "  FAIL ") + name + f" | got={got!r} want={want!r}")
    if not ok:
        fails.append(name)


def true(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="smt_"))
try:
    print("=== 目录配置解析 ===")
    eq("普通 源:目标", SymlinkMonitor._split_dir_conf("/a/b:/c/d")[0], "/a/b")
    eq("普通 目标", str(SymlinkMonitor._split_dir_conf("/a/b:/c/d")[1]), "/c/d")
    eq("无目标", SymlinkMonitor._split_dir_conf("/a/b")[1], None)
    eq("路径含空格", str(SymlinkMonitor._split_dir_conf("/a b:/c d")[1]), "/c d")

    print("\n=== 路径包含判断 ===")
    true("同路径", SymlinkMonitor._is_same_or_child("/a/b", "/a/b"))
    true("子路径", SymlinkMonitor._is_same_or_child("/a/b/c", "/a/b"))
    true("非子路径（前缀陷阱）", not SymlinkMonitor._is_same_or_child("/a/bc", "/a/b"))
    true("无关路径", not SymlinkMonitor._is_same_or_child("/x/y", "/a/b"))

    print("\n=== 插件实例：不删除目录 ===")
    p = SymlinkMonitor()
    p._exclude_dirs = "/media/keep\n/media/other\n"
    true("命中", p._is_excluded("/media/keep/a.mkv"))
    true("子目录命中", p._is_excluded("/media/keep/sub/a.mkv"))
    true("前缀陷阱不命中", not p._is_excluded("/media/keeper/a.mkv"))
    true("未配置则全不命中", not p._is_excluded("/media/x/a.mkv"))
    p._exclude_dirs = ""
    true("空配置不命中", not p._is_excluded("/media/keep/a.mkv"))

    print("\n=== 跳过规则 ===")
    p2 = SymlinkMonitor()
    p2._exclude_dirs = ""
    p2._exclude_keywords = r"sample|\.part$"
    true("临时文件跳过", p2._skip_file(tmp / "a.mkv.part"))
    true("隐藏文件跳过", p2._skip_file(tmp / ".hidden"))
    true("关键词命中跳过", p2._skip_file(tmp / "movie.sample.mkv"))
    true("正常文件不跳过", not p2._skip_file(tmp / "movie.mkv"))
    true("回收站跳过", p2._skip_file(Path("/x/@eaDir/y.mkv")))
    p2._exclude_dirs = "/media/keep"
    true("不删除目录跳过", p2._skip_file(Path("/media/keep/a.mkv")))

    print("\n=== 刮削识别 ===")
    p3 = SymlinkMonitor()
    d = tmp / "scrap"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.nfo").write_text("x")
    (d / "a.jpg").write_text("x")
    true("只剩刮削 -> True", p3._is_only_scrap(d))
    (d / "a.mkv").write_text("x")
    true("有媒体文件 -> False", not p3._is_only_scrap(d))
    (d / "a.mkv").unlink()
    (d / "sub").mkdir()
    (d / "sub" / "b.mkv").write_text("x")
    true("含非刮削子目录 -> False", not p3._is_only_scrap(d))

    print("\n=== 归属目录判断 ===")
    p4 = SymlinkMonitor()
    p4._dirconf = {"/downloads/movie": Path("/media/movie")}
    eq("命中归属", p4._owner_of(Path("/downloads/movie/a/b.mkv")), "/downloads/movie")
    eq("不命中归属", p4._owner_of(Path("/downloads/tv/a.mkv")), None)
    eq("软链接路径推导", str(p4._link_path_of("/downloads/movie/a/b.mkv")), "/media/movie/a/b.mkv")
    eq("不在监控内返回 None", p4._link_path_of("/other/x.mkv"), None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"共失败 {len(fails)} 项" + ("" if not fails else "：" + ", ".join(fails)))
raise SystemExit(1 if fails else 0)

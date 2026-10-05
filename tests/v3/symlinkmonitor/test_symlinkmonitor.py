"""插件纯逻辑单元测试（MoviePilot V3）。

覆盖：目录配置解析、路径包含、不删除目录、跳过规则、刮削识别/匹配、
软链接目标解析、归属判断。本插件**不建立软链接**，故无建链用例。

按官方测试规范：通过生产命名空间 app.plugins.symlinkmonitor 导入插件
（conftest 负责注入后端与插件目录）。
"""
import shutil
import tempfile
from pathlib import Path

from app.plugins.symlinkmonitor import SymlinkMonitor


def eq(name, got, want):
    """断言相等，失败信息带上用例名与实际值。"""
    assert got == want, f"{name} | got={got!r} want={want!r}"


def true(name, cond, extra=""):
    """断言条件为真，失败时带上用例名。"""
    assert cond, name + (f" | {extra}" if extra else "")


def test_plugin_pure_logic():
    """逐项校验插件的纯逻辑方法。"""
    # 临时目录固定 /tmp：插件会跳过任何含点目录的路径，而运行环境的
    # TMPDIR 可能落在 ~/.hermes/... 这类点目录里，导致正常文件被误判跳过。
    tmp = Path(tempfile.mkdtemp(prefix="smt_", dir="/tmp"))
    try:
        eq("普通 源", SymlinkMonitor._split_dir_conf("/a/b:/c/d")[0], "/a/b")
        eq("普通 目标", str(SymlinkMonitor._split_dir_conf("/a/b:/c/d")[1]), "/c/d")
        eq("无目标", SymlinkMonitor._split_dir_conf("/a/b")[1], None)
        eq("路径含空格", str(SymlinkMonitor._split_dir_conf("/a b:/c d")[1]), "/c d")

        true("同路径", SymlinkMonitor._is_same_or_child("/a/b", "/a/b"))
        true("子路径", SymlinkMonitor._is_same_or_child("/a/b/c", "/a/b"))
        true("非子路径（前缀陷阱）", not SymlinkMonitor._is_same_or_child("/a/bc", "/a/b"))
        true("无关路径", not SymlinkMonitor._is_same_or_child("/x/y", "/a/b"))

        p = SymlinkMonitor()
        p._exclude_dirs = "/media/keep\n/media/other\n"
        true("命中", p._is_excluded("/media/keep/a.mkv"))
        true("子目录命中", p._is_excluded("/media/keep/sub/a.mkv"))
        true("前缀陷阱不命中", not p._is_excluded("/media/keeper/a.mkv"))
        true("未配置则全不命中", not p._is_excluded("/media/x/a.mkv"))
        p._exclude_dirs = ""
        true("空配置不命中", not p._is_excluded("/media/keep/a.mkv"))

        p2 = SymlinkMonitor()
        p2._exclude_dirs = ""
        p2._exclude_keywords = r"sample|\.part$"
        true("临时文件跳过", p2._skip_file(tmp / "a.mkv.part"))
        true("隐藏文件跳过", p2._skip_file(tmp / ".hidden"))
        true("关键词命中跳过", p2._skip_file(tmp / "movie.sample.mkv"))
        true("正常文件不跳过", not p2._skip_file(tmp / "movie.mkv"))
        true("回收站跳过", p2._skip_file(Path("/x/@eaDir/y.mkv")))
        p2._exclude_dirs = "/media/keep"
        true("保护目录不再跳过（照常监控）", not p2._skip_file(Path("/media/keep/a.mkv")))
        true("但被标记为受保护", p2._is_protected(Path("/media/keep/a.mkv")))
        p2._exclude_keywords = r"^(?:(?!$).)*$"
        true("正则元字符不炸", isinstance(p2._skip_file(tmp / "movie.mkv"), bool))

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

        p5 = SymlinkMonitor()
        p5._exclude_dirs = ""
        sc = tmp / "scrap2"
        sc.mkdir(parents=True, exist_ok=True)
        for n in ("电影A.mkv", "电影A.nfo", "电影A-poster.jpg", "电影A.zh.srt",
                  "电影AB.mkv", "电影AB.nfo"):
            (sc / n).write_text("x")
        p5._delete_scrap_files(sc / "电影A.mkv")
        true("同名前缀 nfo 被清", not (sc / "电影A.nfo").exists())
        true("带 - 后缀图片被清", not (sc / "电影A-poster.jpg").exists())
        true("带 . 后缀字幕被清", not (sc / "电影A.zh.srt").exists())
        true("更长名字未被误伤（电影AB.mkv）", (sc / "电影AB.mkv").exists())
        true("更长名字未被误伤（电影AB.nfo）", (sc / "电影AB.nfo").exists())

        ld = tmp / "links"
        ld.mkdir(parents=True, exist_ok=True)
        tgt = tmp / "src" / "m.mkv"
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_text("x")
        (ld / "abs.mkv").symlink_to(tgt)
        (ld / "rel.mkv").symlink_to(Path("..") / "src" / "m.mkv")
        eq("绝对目标解析",
           str(SymlinkMonitor._target_of(ld / "abs.mkv")), str(tgt))
        eq("相对目标按链接目录解析",
           str(SymlinkMonitor._target_of(ld / "rel.mkv")), str(tgt))
        true("普通文件返回 None", SymlinkMonitor._target_of(ld) is None)

        p4 = SymlinkMonitor()
        p4._dirconf = {"/downloads/movie": Path("/media/movie")}
        eq("命中归属", p4._owner_of(Path("/downloads/movie/a/b.mkv")), "/downloads/movie")
        eq("不命中归属", p4._owner_of(Path("/downloads/tv/a.mkv")), None)
        eq("软链接路径推导", str(p4._link_path_of("/downloads/movie/a/b.mkv")),
           "/media/movie/a/b.mkv")
        eq("不在监控内返回 None", p4._link_path_of("/other/x.mkv"), None)
        eq("链接目录反查", str(p4._link_dir_of("/downloads/movie/a/b.mkv")), "/media/movie")
        eq("无归属时链接目录为 None", p4._link_dir_of("/other/x.mkv"), None)
        true("下载目录根是收尾边界", p4._is_stop_dir(Path("/downloads/movie")))
        true("软链接目录根是收尾边界", p4._is_stop_dir(Path("/media/movie")))
        true("普通子目录不是收尾边界", not p4._is_stop_dir(Path("/media/movie/子目录")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

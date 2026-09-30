# 测试说明

按插件代际分目录：`tests/v2/<插件ID>/`。测试需在 **MoviePilot 容器内**运行——
用例通过 `importlib.import_module("app.plugins.symlinkmonitor")` 加载插件，
并复用宿主提供的 `app.*` 模块（如 `app.core.plugin`、`app.db.*`）。

## 运行

```bash
# 宿主机（群晖）上，把仓库同步进容器后执行
docker exec jxxghp-moviepilot-v2 python3 tests/v2/symlinkmonitor/test_symlinkmonitor.py
```

多份用例可直接循环：

```bash
for f in tests/v2/symlinkmonitor/test_*.py; do
  docker exec jxxghp-moviepilot-v2 python3 "$f" || echo "FAIL: $f"
done
```

## 约定

- 全部用例只操作**本地临时目录**，不触碰真实媒体库、下载器或线上数据库。
- 插件数据（如 `deletion_log.jsonl`）写在插件数据目录
  `/config/plugins/SymlinkMonitor/`，测试结束请自行清理测试产生的记录。
- 用例失败以**非零退出码**结束，便于 CI 判定。
- 各代际（v1/v2/v3）同名插件包会冲突，**分会话运行**，见仓库 `pytest.ini`。

## 用例清单

| 文件 | 覆盖范围 |
| --- | --- |
| `test_symlinkmonitor.py` | 纯逻辑：目录配置解析、路径包含、保护目录、跳过规则、刮削识别与匹配、软链接目标解析、归属判断 |
| `test_protect_dirs.py` | 保护目录语义：照常监控但绝不删除；下载目录只读（`clean_source_scrap` 默认关） |
| `test_regress_config_guard.py` | 配置防呆：保护目录覆盖监控目录时忽略该项并告警 |
| `test_deletion_log.py` | 删除记录：每次清理记一条、落盘持久化、条数上限、计数与列表同源、详情页渲染 |
| `test_e2e_acceptance.py` | 端到端：删源文件 → 延迟窗口 → 联动清链/刮削/空目录，未受影响链接保留 |

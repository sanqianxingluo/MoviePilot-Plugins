# 软链接监控（SymlinkMonitor）

MoviePilot 插件：监控下载目录，自动在媒体库目录建立**软链接**；源文件被删除后，
延迟确认再**联动清理**软链接、刮削文件、转移记录与下载种子。

## 功能

| 功能 | 说明 |
| --- | --- |
| 软链接 | 监控目录新增文件 → 在目标目录建立符号链接（`os.symlink`，可跨文件系统） |
| 延迟删除 | 源文件消失后不立即动手，等 `delay_seconds`（默认 30 秒）再确认 |
| 联动删除种子 | 按转移记录的 `download_hash` 触发 `DownloadFileDeleted`，删除下载任务（**不删数据**） |
| 清理刮削文件 | 按文件名前缀清理同名的 `.nfo/.xml/图片/字幕`，以及 `.trickplay` 等刮削目录 |
| 删除转移记录 | 通过 `TransferHistoryOper.get_by_src()` 找到并删除对应整理记录 |
| 发送通知 | 建链 / 建链失败 / 联动清理各发一条通知（可关） |
| 启用插件 | 顶部开关 |
| 立即运行一次 | 勾选后 3 秒内全量扫描一次，补建缺失软链接 |
| 监控目录 | 每行一条，格式 `源目录:软链接目标目录` |
| 不删除目录 | 每行一条；这些目录**不建链、永不删除**（含子目录） |

## 配置项

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 关 | 启用插件 |
| `notify` | 开 | 发送通知 |
| `onlyonce` | 关 | 立即运行一次（全量补链） |
| `monitor_dirs` | 空 | 监控目录，`源:目标` 每行一条 |
| `exclude_dirs` | 空 | 不删除目录，每行一条 |
| `exclude_keywords` | 空 | 排除关键词（正则），每行一条 |
| `scan_interval` | 10 | 扫描间隔（秒），最小 3 |
| `delayed_deletion` | 开 | 启用延迟删除 |
| `delay_seconds` | 30 | 延迟时间（秒），范围 5 ~ 86400 |
| `delete_scrap` | 开 | 清理刮削文件 |
| `delete_history` | 开 | 删除转移记录 |
| `delete_torrents` | 开 | 联动删除种子 |
| `clean_empty_dir` | 开 | 清理空目录 |

## 安装

插件市场添加本仓库地址即可，或手动把 `plugins/symlinkmonitor/` 复制到
MoviePilot 的 `app/plugins/symlinkmonitor/` 后重启容器。


## 从插件市场安装

MoviePilot「设置 → 插件市场」中追加：

```
https://github.com/sanqianxingluo/moviepilot-plugins
```

或在 `config/app.env` 的 `PLUGIN_MARKET` 里追加该地址（逗号分隔）。

## 本地调试

`config/app.env` 加入 `PLUGIN_LOCAL_REPO_PATHS='/config/localrepo'`，
把本仓库放到 `<config>/localrepo/` 后重启容器即可。

## 注意

- **必须挂载**下载目录与媒体库目录到容器内，且路径用容器内路径填写。
- 软链接在宿主机上可通过 SMB/NFS 正常读取（与硬链接不同，**不要求同一文件系统**）。
- 若用 Emby/Jellyfin 扫描，媒体库路径需与软链接目标目录一致。
- 联动删除种子**只移除下载任务，不删除已下载的数据文件**；数据由本插件按源文件删除事件处理。

## 版本

- v1.0.0 首个版本：软链接、延迟删除、联动删种、刮削清理、转移记录清理、通知、立即运行、监控/不删除目录。

# MoviePilot Plugins（自建插件仓）

个人维护的 MoviePilot **V3** 插件仓库，可直接作为 `PLUGIN_MARKET` 源使用。

遵循 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) 的仓库规范
（`docs/Plugin_Development.md` + `docs/Repository_Guide.md`）：V3 新插件使用
`plugins.v3/` + `tests/v3/` + `package.v3.json`。

## 插件列表

| 插件 | 说明 | 版本 |
| --- | --- | --- |
| [软链接监控](plugins.v3/symlinkmonitor/README.md) | 只监控不建链：下载目录文件删除后，延迟清理软链接目录中指向它的链接，并联动清理刮削文件、转移记录与下载种子 | 3.0.0 |

## 使用方法

### 方式一：添加为插件市场

在 MoviePilot 的「设置 → 插件市场」中追加本仓库地址：

```
https://github.com/sanqianxingluo/MoviePilot-Plugins
```

### 方式二：本地插件仓库（推荐自用）

1. 把本仓库放到 MoviePilot 能读到的目录，例如
   `/volume1/docker/docker/moviepilot-v2/config/localrepo/`
2. 在 `config/app.env` 加入：

   ```
   PLUGIN_LOCAL_REPO_PATHS='/config/localrepo'
   ```

   多个路径用逗号分隔。**改完需重启 MoviePilot 容器**（启动时才会扫描本地仓库）。
3. 重启后在插件市场中即可看到本地插件并安装。

## 目录结构

```
.
├── package.v3.json              # V3 插件市场索引
├── package.json                 # 默认历史索引（本仓无 plugins/，留空）
├── LICENSE                      # GPL-3.0
├── icons/                       # 插件图标
├── plugins.v3/                  # V3 专用插件实现
│   └── symlinkmonitor/
├── tests/
│   └── v3/
│       └── symlinkmonitor/      # 单测（需在 MoviePilot 容器内运行，见 tests/README.md）
└── .github/                     # 发布工作流与校验脚本（照官方）
```

**一个插件只保留一套实现。** 不要同时放 `plugins/`、`plugins.v2/` 与 `plugins.v3/`
的同名副本：宿主按 `VERSION_FLAG` 只会加载其中一个，重复副本会造成「改一处、漏一处」。

## 版本目录解析规则（宿主实测）

宿主按 `VERSION_FLAG` 依次尝试：

1. `package.v3.json` 里有该插件 → 加载 `plugins.v3/<id>/`
2. 否则看默认索引 `package.json`，若该插件 `"v2": true` → 加载 `plugins/<id>/`
3. 都没有 → 插件不可用

本仓只提供 V3 实现，因此只有 `package.v3.json` 有条目。

## 发布

`.github/workflows/release.yml`（照官方）在 `package*.json` 变更时触发，
对索引里 `"release": true` 的条目自动打 Tag `插件ID_v版本号` 并发布
`插件目录小写_v版本号.zip`。推送前会先跑两道门禁：
`check_plugin_versions.py`（三处版本一致）与 `check_federation_css.py`（联邦样式）。

## 许可

GPL-3.0（与官方插件仓一致，见 [LICENSE](LICENSE)）。

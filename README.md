# MoviePilot Plugins（自建插件仓）

个人维护的 MoviePilot 插件仓库，可直接作为 `PLUGIN_MARKET` 源使用。

## 插件列表

| 插件 | 说明 | 版本 |
| --- | --- | --- |
| [软链接监控](plugins/symlinkmonitor/README.md) | 只监控不建链：下载目录文件删除后，延迟清理软链接目录中指向它的链接，并联动清理刮削文件、转移记录与下载种子 | 2.0.0 |

## 使用方法

### 方式一：添加为插件市场

在 MoviePilot 的「设置 → 插件市场」中追加本仓库地址：

```
https://github.com/sanqianxingluo/moviepilot-plugins
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
├── package.json                 # 插件索引（V2/V3 通用）
├── icons/                       # 插件图标
├── plugins/                     # V1/V2 兼容实现（package.json 里 v2: true 时生效）
│   └── symlinkmonitor/
└── plugins.v2/                  # V2 专用实现目录（可选）
```

MoviePilot 按 `VERSION_FLAG` 选择目录：V2 宿主读 `package.v2.json` + `plugins.v2/`，
回退读 `package.json` + `plugins/`。

## 许可

MIT

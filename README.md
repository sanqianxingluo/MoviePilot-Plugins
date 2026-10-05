# MoviePilot-Plugins

MoviePilot 第三方插件仓库：https://github.com/sanqianxingluo/MoviePilot-Plugins

## 插件列表

1. [软链接监控 v3.0.0](https://github.com/sanqianxingluo/MoviePilot-Plugins/tree/main/plugins.v3/symlinkmonitor)：只监控不建链。下载目录文件删除后延迟清理软链接目录中指向它的链接，并联动清理刮削文件、转移记录与下载种子；保护目录里的内容永不删除。

## 安装

在 MoviePilot 的「设置 → 插件市场」中追加本仓库地址：

```
https://github.com/sanqianxingluo/MoviePilot-Plugins
```

或在 `config/app.env` 的 `PLUGIN_MARKET` 里追加该地址（多个用逗号分隔）。

## 许可证

GPL-3.0（与 [官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins) 一致，见 [LICENSE](LICENSE)）。

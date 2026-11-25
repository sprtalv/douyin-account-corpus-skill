# 本机安装与迁移

技能文件与账号数据分离。技能中不打包账号内容、Cookie、音轨、模型权重或虚拟环境。支持Python3.11+，本机以Windows验证；其他系统需自行验证FFmpeg和GPU依赖。

## 安装

将发布ZIP解压，获得`douyin-account-corpus`目录；在源目录执行：
```text
python scripts/deploy.py
```
默认部署到CODEX_HOME/skills/douyin-account-corpus；未设CODEX_HOME则使用用户.codex/skills。目标已存在时先移至CODEX_HOME/skill-backups，失败尝试恢复。可以显式`--target`指定技能目标目录。源目录与目标必须分离，不要直接在安装目录运行安装自己。

安装后在目标技能目录执行：
```text
python scripts/setup_env.py
```
它创建技能自己的`.venv`并安装依赖，不改系统Python，不下载模型。需要GPU依赖时加`--gpu-libs`；开发测试加`--with-tests`。FFmpeg和ffprobe必须能从PATH找到，安装脚本不会修改系统PATH或安装GPU驱动。

执行技能`.venv`中的Python运行`scripts/run.py doctor`。首次模型获取使用`scripts/fetch_model.py --help`查看；已下载模型通过init的`--model-dir`复用。引擎仅使用完整本地模型，不因网络不可用改成质量更低的模型。模型获取和ASR分开，公共权重首次获取需要网络；ASR在本机运行。

会话是否立即发现新技能取决于宿主；可直接引用其SKILL.md路径使用，或在新会话调用`$douyin-account-corpus`。安装脚本成功只证明文件部署，不能证明浏览器可以访问目标账号。

## 发布

```text
python scripts/deploy.py --package-only --archive PATH.zip
```
只包含程序、技能说明、参考、依赖和测试，ZIP附文件SHA256清单。默认排除隐藏目录、.venv、缓存、dist和所有账号数据。不要将真实数据放在源码的scripts/tests目录。

## 已有资料

已有账号成果保留在原项目，无需为安装重跑。旧数据库需要显式隔离迁移；不要将其覆盖成新项目。只做纯文字分发时可直接读取已有转写 JSON，见 [纯文字导出](text-delivery.md)。

## 已知边界

本版提供独立命令和状态恢复；平台主页枚举仍依赖助手使用正常浏览器。run为单进程顺序消费队列，重用GPU模型；未实现浏览器、下载、ASR三个常驻并发服务。全文语义复核由助手阅读packet，Python校验不能替代语义理解。测试通过不代表新账号一定无访问限制或所有字词都准确。

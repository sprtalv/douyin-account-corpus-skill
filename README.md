# 抖音账号转写资料库 Skill

独立的 Codex Skill 与 Python 工具：将正常浏览器观察到的公开作品、已有字幕或合法本地媒体整理为完整转写资料，支持增量处理、可追溯说话人标注，以及按博主文件夹分发纯文字。

仓库只包含通用程序、说明与合成测试。账号资料、转写正文、疑点记录、模型、浏览器配置和凭据保存在仓库外。

## 安装

需要 Python 3.11+、FFmpeg 和 ffprobe。Windows 已验证；其他平台的音视频与 GPU 环境需自行验证。

```console
git clone https://github.com/sprtalv/douyin-account-corpus-skill.git
cd douyin-account-corpus-skill
python scripts/deploy.py
```

部署位置默认为 `$CODEX_HOME/skills/douyin-account-corpus`，未设置时为 `~/.codex/skills/douyin-account-corpus`。已存在的安装会先备份。进入部署后的技能目录运行：

```console
python scripts/setup_env.py
```

用创建的 `.venv` 中的 Python 执行 `scripts/run.py doctor`。GPU 库可通过 setup_env 的 `--gpu-libs` 选项安装；模型需显式下载或配置已有本地目录。详见 [安装说明](references/install.md)。

在 Codex 中调用 `$douyin-account-corpus`，提供账号链接、范围及仓库外的资料目录。入口规范见 [SKILL.md](SKILL.md)，CLI 用法见 [命令参考](references/commands.md)。浏览器采集由助手操作当前可用的正常浏览器完成；本工具不绕过验证码，不提供 Cookie 抓取器。

## 纯文字分发

```text
分发目录/
├── 博主甲/
│   └── 作品标题_作品ID.txt
└── 博主乙/
    └── 作品标题_作品ID.txt
```

一篇作品只输出一个已选正文版本。TXT 保留标题、博主、作品 ID、原链接与全文；不导出时间戳、疑点报告或二次识别对照。已有且与原文一致的说话人标签可以保留。仅按现有分段和句末标点分段，不润色、补标点或推断新角色。

```console
python scripts/export_text.py --manifest /path/to/internal/selection.json --output /path/to/delivery --audit /path/to/internal/export-audit.json
```

导出不需要 ASR 依赖。选择清单及审计记录留在交付目录之外；输入不覆盖，已存在的输出目录拒绝覆盖。版本选择规则、清单格式见 [纯文字导出](references/text-delivery.md)。

## 开发验证

```console
python -m pip install -r requirements.txt -r requirements-test.txt
python -m pytest -q
```

可在已具备依赖的独立环境运行测试。测试使用临时目录和合成数据，不需要访问真实账号。历史验证范围见 [验证记录](references/validation.md)。

机器转写可能存在错字和漏字；语义角色复核、二次识别、结构校验均不等于人工逐字听校。采集范围由实际证据决定，不承诺账号历史作品全部可获取。仓库不附带原作者内容或其使用许可。

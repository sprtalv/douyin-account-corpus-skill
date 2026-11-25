# 命令和输入输出

Windows中文输出建议先设置`$env:PYTHONIOENCODING="utf-8"`。

以下`PY`代表技能虚拟环境Python绝对路径，`RUN`代表技能`scripts/run.py`绝对路径；PowerShell使用`& $py $run ...`。资料目录是工作项目，不是技能安装目录。命令帮助：`PY RUN --help`。

## 新项目

```text
PY RUN doctor
PY RUN --root WORK init --name 账号显示名 --account https://www.douyin.com/user/真实ID --review-policy full --device cuda --model large-v3-turbo
PY RUN --root WORK import-index MANIFEST.json
```
CPU可选`--device cpu`；有已经下载好的模型目录用`--model-dir MODEL_DIR`。默认中文、beam_size=5、batch_size=1连续上下文解码、不启用VAD跳过静音，保留词级时间。默认模式中断后重跑该条未完成视频以保留完整上下文，已完成视频跳过；不是从最后一个词硬接续。批处理仅显式调整配置后使用，应先做质量对照。原机器大模型权重可复用，不需再次下载；不承诺任意模型质量相同。

`MANIFEST.json`真实观察格式（下例为字段说明，需替换占位符）：
```json
{"account":{"name":"实际账号名","url":"https://www.douyin.com/user/真实ID"},"observed_at":"实际日期时间+08:00","evidence":"正常页面观察方式与本地证据路径","videos":[{"id":"实际19位视频ID","title":"实际页面标题"}]}
```
清单只含当前账号的真实作品。每轮清单保存快照，重复导入不清空已处理状态。不自动把主页发布认定为原创，不把同名认定为同人。没有观察到的作品不删除历史记录。

## 字幕、音轨和批量运行

```text
PY RUN --root WORK import-subtitles VIDEO_ID FILE.srt
PY RUN --root WORK import-media VIDEO_ID FILE.mp4
PY RUN --root WORK observe OBSERVATIONS.json
PY RUN --root WORK run
PY RUN --root WORK run --id VIDEO_ID
PY RUN --root WORK run --retry-failed
```
字幕也支持VTT和带start/end/text的JSON；导入前检查是否完整，不能将片段字幕当作全片稿。相同字幕重复导入可跳过；不同文本不覆盖已存在的原始转写。

`OBSERVATIONS.json`：
```json
[{"id":"实际19位视频ID","page_url":"https://www.douyin.com/video/同一个ID","audio_url":"实际正常播放器公开HTTPS音轨URL","observed_at":"实际观察时间+08:00","published":"可选实际发布时间","declaration":"可选页面虚构演绎等声明"}]
```
不接收Cookie、密码、令牌。公开媒体地址含临时参数时仅作为本地下载输入，不在最终归档中打包。允许的现有媒体域为douyinvod.com及zjcdn.com的子域；其他正常域需核查后扩展，不能随意改成任意URL下载。

`run`结束后输出数量、失败及待复核队列，不输出全部原文。相同失败输入不会每次重试；更新观察或显式`--retry-failed`才再次尝试。一次run处理当时已有清单；新观察导入后再run。运行成功但有受限/失败条目不表示整账号完成。

## 复核与导出

```text
PY RUN --root WORK review-prepare
PY RUN --root WORK review-apply REVIEWS.json
PY RUN --root WORK verify --media
PY RUN --root WORK export
PY RUN --root WORK status
PY RUN --root WORK search 关键词 --speaker 主播
```
复核协议见review.md。最终导出在`exports/index.html`、`transcripts.md`、`uncertain.md`、`corpus.zip`。`review-policy none`表示用户选择不做角色复核，不表示听校完成。只有明确接受部分交付时执行`export --allow-partial`，目录会标部分交付。

`run`在转写和所需复核都满足时尝试最终导出；复核完成后可直接export，不需要重新转写。相同导出输入和文件哈希匹配时跳过重建。

## 状态和恢复

状态输出分别给出videos/media/transcripts/semantic_reviewed/audio_verified；不确定原文在导出uncertain.json。ASR识别疑点保留于原始segments/checkpoint，不等同于说话人不确定。

有`.part`或checkpoint时引擎按校验条件恢复。显式报媒体/配置变化时不要删除checkpoint强行继续；建立新revision项目。确认进程已结束后才删除`logs/workflow.lock`。环境移动导致媒体绝对路径失效时要显式迁移，不能静默把路径指向另一个音轨。

统计中的transcribed_seconds仅为取得媒体的时长；只有字幕而没有媒体时为0，不代表字幕无内容，字幕覆盖请查看原始时间段。

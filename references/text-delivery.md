# 按博主分发纯文字

适用于用户要求每位博主独立文件夹、每篇一份 TXT、去掉时间戳和内部核对材料的情形。源资料与分发快照分离；不移动、删除原稿，不为改变格式重新 ASR。

## 选择正文

有明确的正文校对结论时，使用已确认版本并记录证据；否则选择原始稿。二次识别或相似度更高不构成确认。语义角色复核只改变说话人标签，不构成文字纠错确认。

无语音、无稿、身份未核实及用户排除项只写内部清单，不创建空博主交付文件夹或伪正文。已有角色标注必须与所选正文逐字一致且哈希匹配，否则拒绝套用。

## 输入清单

UTF-8 JSON；相对路径以清单所在目录为基准。每条 source 是包含 `video` 与 `segments` 的完整转写 JSON，video 含 id/title/url，segments 含 text。可选 annotation 是含 video_id/source_sha256/turns 的标注 JSON，turns 含 text/speaker；source_sha256 为 segments 的 text 按顺序直接拼接后 UTF-8 SHA256。

```json
{
  "policy": "有明确校对结论采用确认版本，否则保留原稿",
  "entries": [
    {
      "author": "示例博主",
      "id": "7000000000000000001",
      "source": "../account/data/transcripts/7000000000000000001.json",
      "selection_reason": "原稿；没有已确认的文字修订",
      "issues": []
    }
  ],
  "excluded": []
}
```

可附 annotation、issues、revision_path、review_evidence 等内部字段，导出审计完整保留这些字段，TXT 不包含它们。不要将真实选择清单提交源码仓库。

## 执行和验收

使用 README 的 export_text 命令。只使用标准库；先校验所有输入，再写入新目录；写入失败时可能留下部分输出，应检查后改用新快照目录重试。审计必须位于分发目录之外。

核对账号/作品数量、重复作品、全部正文字符保留、说话人对应、文件哈希和仅 TXT 结构；抽读独白与对话样本。脚本不生成字幕时间戳；源正文中真实提及的时间数字保留。文件名清理 Windows 非法字符并保留作品 ID。审计记录是内部溯源资料，不是准确率报告。

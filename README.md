# pyVideoTrans Subtitle Workflow

这是服务器 `/data/pyvideotrans-workflow` 的可复现工作流版本，只包含脚本、配置、启动入口和说明，不包含视频、字幕输出、模型文件、缓存或 pyVideoTrans 程序本体。

## 当前固定效果

- 语音识别：faster-whisper `large-v3`
- 默认语言：日语 `ja`
- GPU：CUDA 自动启用
- 翻译模型：Ollama `qwen2.5:7b-instruct`
- 输出语言：繁体中文
- 字幕保护：翻译后强制按原字幕编号和时间轴回套，避免模型改坏 SRT 结构
- 启动自检：模型、语言、热词、Ollama 地址不符合时阻止启动

## 服务器启动方式

```bash
pvt-start
```

`pvt-start --help` 只显示帮助，不启动任务。

## 目录约定

- 输入目录：`/data/pyvideotrans-workflow/input`
- 输出目录：`/data/pyvideotrans-workflow/output`
- 工作目录：`/data/pyvideotrans-workflow/work`
- 错误目录：`/data/pyvideotrans-workflow/error`

这些运行目录不纳入 Git。

## 验收标志

启动日志应包含：

- `启动自检: STT 语言=ja`
- `启动自检: 翻译模型=qwen2.5:7b-instruct`
- `启动自检通过`
- `STT 命令: ... --model_name large-v3 --cuda --detect_language ja`
- `Ollama 模型确认: qwen2.5:7b-instruct @ http://127.0.0.1:11434/v1`

完成后的 SRT 应满足：编号数量 = 时间轴数量，无拒绝句/提示词泄漏。

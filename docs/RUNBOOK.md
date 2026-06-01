# pyVideoTrans 字幕流水线 - 部署说明

## 用途

将任意语言视频自动翻译成中文字幕，主要针对日语视频。

**当前只做：** 视频 -> 语音识别(STT) -> 字幕翻译 -> 输出 SRT 文件  
**当前不做：** TTS配音、视频压制、硬字幕烧录、音视频合成

---

## 目录结构

```
/data/pyvideotrans-workflow/
├── input/      # 放入要翻译的视频（放入这里）
├── output/     # 翻译结果（从这里取字幕）
├── work/       # 处理中的临时文件
├── done/       # 处理完成的原视频
├── error/      # 处理失败的视频
├── logs/       # 任务日志
├── status/     # 当前任务状态
├── config/     # 配置文件
├── scripts/    # 处理脚本
└── app/        # pyVideoTrans 程序
```

---

## Windows / Mac 访问 SMB 共享

服务器 IP: `192.168.3.9`

### Windows
1. 打开「此电脑」或「文件资源管理器」
2. 地址栏输入: `\\192.168.3.9`
3. 弹出登录框，用户名: `lqyai`，密码: (Samba密码)
4. 看到两个共享文件夹:
   - `pvt-in` = 放入视频
   - `pvt-out` = 取出字幕

或者直接打开:
- `\\192.168.3.9\pvt-in`
- `\\192.168.3.9\pvt-out`

### Mac
1. Finder -> 前往 -> 连接服务器
2. 输入: `smb://192.168.3.9`
3. 用户名: `lqyai`，密码: (Samba密码)
4. 挂载后看到 `pvt-in` 和 `pvt-out`

---

## 使用方法

### 1. 放入视频
将视频文件（mp4/mkv/mov/avi/webm 等）复制到 `pvt-in` 共享目录

### 2. 启动处理
SSH 登录服务器或打开服务器终端，运行:
```bash
pvt-start
```
屏幕会显示处理进度。按 `Ctrl+C` 可以停止（当前任务完成后停止）。

### 3. 查看状态
```bash
pvt-status
```
显示当前任务ID、步骤、是否完成、是否有错误。

### 4. 查看日志
```bash
pvt-logs
```
实时显示最新任务的日志，按 `Ctrl+C` 退出。

### 5. 环境检测
```bash
pvt-test
```
检查所有组件是否正常。

---

## 输出文件

任务完成后，在 `pvt-out` 或 `output/` 目录下，每个视频会生成一个同名文件夹：

```
output/
  FAD-1219/                  ← Windows: \\192.168.3.9\pvt-out\FAD-1219\
    FAD-1219.avi              ← 原视频（小白直接播放）
    FAD-1219.srt              ← 中文翻译字幕（播放器自动加载 ★）
    original.srt               ← 原始语言字幕
    translated.zh-cn.srt       ← 中文翻译字幕（完整名）
    original.txt               ← 原始字幕纯文本
    translated.zh-cn.txt       ← 翻译字幕纯文本
    manifest.json              ← 任务元信息
    job.log                   ← 任务详细日志
```

**小白使用方式**：打开 `\\192.168.3.9\pvt-out\FAD-1219\`，直接播放 `FAD-1219.avi`，播放器会自动加载同文件夹下的 `FAD-1219.srt` 中文字幕。

---

## 当前模型配置

- **ASR 语音识别:** faster-whisper large-v3（自动识别语言，不写死日语）
- **翻译模型:** hy-mt2-translate:latest（本地 Ollama，专为字幕翻译优化）
- **翻译方向:** 自动检测语言 → 中文(zh-cn)
- **历史备选:** qwen2.5:7b-instruct（曾用，hy-mt2 翻译质量更优）
- **并行任务:** 1（串行处理）

---

## 判断设置是否生效

```bash
pvt-test
```
看到所有 ✓ PASS 即正常。

---

## 常见问题

**Q: 视频放进去没反应？**
A: 检查 pvt-start 是否在运行，pvt-status 是否显示 idle

**Q: 字幕翻译质量不好？**
A: 当前使用 hy-mt2-translate:latest（专为字幕翻译优化）。历史使用过 qwen2.5:7b-instruct，如需切换可修改 config/job_config.yaml 中的 selected_model

**Q: 处理失败了？**
A: 去 /data/pyvideotrans-workflow/error/ 找视频，日志在对应 output/<job_id>/job.log

**Q: 怎么删除已完成的任务输出？**
A: 清理 /data/pyvideotrans-workflow/output/ 下的对应 job_id 目录

**Q: 想同时处理多个视频？**
A: 当前配置 max_parallel_jobs=1，如需并行请修改 config/job_config.yaml

---

## 失败视频位置

处理失败的文件会移动到: `/data/pyvideotrans-workflow/error/`

日志文件: `/data/pyvideotrans-workflow/logs/`

---

## 日志位置

所有任务日志在: `/data/pyvideotrans-workflow/logs/`
文件名格式: `job_<job_id>.log`

---

## 扩展方向（未来可做）

- **硬字幕:** 用 ffmpeg 将字幕烧录进视频
- **配音:** 调用 TTS 生成中文配音并替换原音
- **视频压制:** 将字幕和配音合并为最终视频

以上当前均未实现。

---

## 重要提醒

**密码、token、密钥不要写入日志。**  
配置中的 api_key 是占位符，实际 Ollama 本地调用不需要真实密钥。

---

## 命令汇总

| 命令 | 作用 |
|------|------|
| `pvt-start` | 启动批处理流水线 |
| `pvt-status` | 查看当前任务状态 |
| `pvt-logs` | 实时查看任务日志 |
| `pvt-test` | 环境完整性检测 |

---

## 给 Hermes 的启动提醒

> 本章节供 Hermes Agent 参考。完整指南见 [HERMES_START_GUIDE.md](./HERMES_START_GUIDE.md)

### 字幕流水线内部继续用 127.0.0.1（不需改动）

- 服务器本机跑 pvt-start / pvt-test 时，Ollama 地址写 `http://127.0.0.1:11434/v1`
- 这是本机回环地址，服务器自己访问自己的 Ollama，稳定且安全
- **不要改成 192.168.3.9**，字幕流水线不需要局域网调用

### Ollama 已对局域网开放（不需要再配置）

当前状态：
- systemd `OLLAMA_HOST=0.0.0.0:11434`
- `ss` 显示监听 `*:11434`
- ufw inactive（防火墙已关闭）

### 局域网测试 Ollama 的正确端点

- 服务器本机：`curl http://127.0.0.1:11434/api/tags` 或 `/v1/models`
- 局域网：`curl http://192.168.3.9:11434/api/tags` 或 `/v1/models`
- 直接访问 `http://192.168.3.9:11434/v1` 返回 404 是**正常的**（/v1 不是网页路径）
- 正确的测试路径是 `/v1/models` 或 `/api/tags`

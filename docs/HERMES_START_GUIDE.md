# Hermes 使用指南：pyVideoTrans 字幕流水线

> 本文档供 Hermes Agent 内部使用，面向直接调用 Hermes 的用户（小白）。
> Hermes 收到"开始处理字幕/启动任务/跑 pvt"相关请求时，执行本指南。

---

## 一、流水线职责范围

**只做：**
- 视频 → 语音识别(STT，Whisper) → 字幕翻译(Ollama) → 输出 SRT 文件

**不做（永远不要做）：**
- 配音、TTS 语音合成
- 视频压制、硬字幕烧录
- 字幕融合、WebUI 界面
- 任何不在上述"只做"列表里的功能

---

## 二、命令速查

| 命令 | 作用 |
|------|------|
| `pvt-test` | 环境完整性检测（先跑这个） |
| `pvt-start` | 启动字幕处理流水线 |
| `pvt-status` | 查看当前任务状态 |
| `pvt-logs` | 实时查看任务日志，按 Ctrl+C 退出 |

所有命令在服务器本机 `/data/pyvideotrans-workflow/` 目录下执行，或通过 `pvt-*` alias。

---

## 三、标准启动流程

### 第 1 步：环境检测
先执行 `pvt-test`，确认所有组件正常。所有 PASS 才继续。

### 第 2 步：检查 input 有没有视频
pvt-test 会检查 `/data/pyvideotrans-workflow/input/` 目录（也映射为 SMB 共享 `pvt-in`）。

**如果 input 为空（没有视频）：**
告诉用户去以下位置上传视频：
- **Windows:** `\\192.168.3.9\pvt-in`
- **Mac:** `smb://192.168.3.9/pvt-in` → 连接服务器，输入用户名 `lqyai` 和密码

然后等待用户确认上传完成，再继续。

### 第 3 步：检查流水线是否已在运行
执行 `pvt-status`：
- 如果显示 idle 或无可用任务 → 可以启动
- 如果已有任务在跑 → **不要重复启动**，告诉用户流水线已在运行中，用 `pvt-status` 或 `pvt-logs` 查看进度

### 第 4 步：启动
确认 input 有视频且流水线 idle 时，执行 `pvt-start`。

---

## 四、处理中

用 `pvt-status` 和 `pvt-logs` 查看进度。

---

## 五、完成

任务完成后，告诉用户去以下位置取字幕文件：
- **Windows:** `\\192.168.3.9\pvt-out`
- **Mac:** `smb://192.168.3.9/pvt-out`

**新输出结构**：不再使用 job_id 文件夹，每个视频有独立的同名文件夹：

```
output/
  FAD-1219/
    FAD-1219.avi           ← 原视频
    FAD-1219.srt           ← 中文字幕（播放器自动加载 ★）
    original.srt
    translated.zh-cn.srt
    manifest.json
    job.log
```

小白打开 `\\192.168.3.9\pvt-out\FAD-1219\`，直接播放 `FAD-1219.avi`，字幕自动加载。

---

## 六、关于 Ollama 地址的详细解释（重要）

### 6.1 当前服务器 Ollama 已对局域网开放

当前服务器 systemd 配置 `OLLAMA_HOST=0.0.0.0:11434`，`ss` 显示监听 `*:11434`，`ufw` 已关闭。
**Ollama 已经可以从局域网其他机器访问，不需要再做任何配置。**

### 6.2 字幕流水线内部继续用 127.0.0.1（不需改动）

字幕流水线的所有脚本（pvt-start / pvt-test / pvt-logs）在服务器本机执行，配置里的 Ollama 地址仍然是：

```
http://127.0.0.1:11434/v1
```

这是**正确且稳定的**，原因：
- 服务器自己访问自己的 Ollama，127.0.0.1 是本机回环地址，不走网卡，无延迟
- 127.0.0.1 不会暴露在局域网上，更安全
- 字幕流水线所有任务都在服务器本机执行，不存在跨机器调用
- **不需要、也不应该改成 192.168.3.9**

### 6.3 局域网测试 Ollama 是否正常的正确方式

**服务器本机执行：**
```bash
curl http://127.0.0.1:11434/api/tags
# 或
curl http://127.0.0.1:11434/v1/models
```

**局域网其他机器执行（推荐）：**
```bash
curl http://192.168.3.9:11434/api/tags
# 或
curl http://192.168.3.9:11434/v1/models
```

返回 JSON 格式的模型列表即为正常。

### 6.4 为什么访问 http://192.168.3.9:11434/v1 显示 404 是正常的

在浏览器或 curl 中直接访问：

```
http://192.168.3.9:11434/v1
```

返回 `404 page not found` **是正常现象，不代表 Ollama 坏了。**

原因：
- `/v1` 是 OpenAI 兼容的 **API 端点前缀**，不是网页路径
- 浏览器/curl 会把 `/v1` 当作页面去加载，而 Ollama 对根路径 `/v1` 没有返回内容
- 正确的测试端点是 `/v1/models` 或 `/api/tags`（见 6.3）

### 6.5 当前 Ollama 监听状态汇总

| 项目 | 值 |
|------|-----|
| systemd OLLAMA_HOST | `0.0.0.0:11434` |
| ss 监听地址 | `*:11434`（所有网卡） |
| ufw 防火墙 | inactive（已关闭） |
| 局域网测试地址 | `http://192.168.3.9:11434/v1/models` 或 `/api/tags` |
| 字幕流水线内部地址 | `http://127.0.0.1:11434/v1`（不变） |

---

## 七、安全注意事项

**永远不要把密码、token、密钥写入日志。**

- 日志中如出现密码/token/密钥，立即用占位符替换
- pvt-test / pvt-status / pvt-logs 的输出里可能包含文件路径，这是安全的，但不要输出任何凭据
- SMB 共享密码、Samba 密码等不要出现在任何命令输出里

---

## 八、遇到问题时的检查顺序

1. `pvt-test` — 组件是否都 PASS
2. `pvt-status` — 任务状态，是否 idle
3. `pvt-logs` — 最新错误信息
4. `ls /data/pyvideotrans-workflow/input/` — 视频是否已放入
5. `ls /data/pyvideotrans-workflow/error/` — 是否有失败视频
6. 检查对应 `output/<job_id>/job.log`

---

## 九、SMB 共享路径速查

| 用途 | Windows 路径 | Mac 路径 |
|------|------------|---------|
| 放入视频 | `\\192.168.3.9\pvt-in` | `smb://192.168.3.9/pvt-in` |
| 取出字幕 | `\\192.168.3.9\pvt-out` | `smb://192.168.3.9/pvt-out` |

用户名统一是 `lqyai`，密码是 Samba 密码。

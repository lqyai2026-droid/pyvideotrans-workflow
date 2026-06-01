#!/usr/bin/env python3
"""
pyVideoTrans 字幕批处理流水线 v2.1
功能：视频 -> STT(original.srt) -> Ollama翻译(translated.zh-cn.srt)

修复点（v2.1）：
1. 递归扫描 input 下所有子目录的视频
2. output 目录为 output/<视频主名>/
3. 成功：视频移动到 output/<视频主名>/ + 生成同名 .srt
4. 失败：视频移动到 error/<相对路径>/
5. 防复用字幕：STT 前记录时间戳，只接受本任务产生的 SRT
6. large-v3 离线失败时降级到 medium 并记录到 job.log 和 manifest.json
7. 零时长字幕跨秒正确进位
8. pvt-status 显示总数/已完成/失败/当前视频/输出目录
9. 成功输出目录只保留：原视频 + 同名.srt（manifest/log/original/translated/txt 移至归档）
10. 失败跳过按具体视频判断，不按父目录
11. 翻译提示词纯净，适合私人字幕翻译；清理更彻底
"""

import os
import sys
import json
import time
import fcntl
import signal
import logging
import subprocess
import re
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import urllib.request
import urllib.error

# ─── 路径配置 ────────────────────────────────────────────
BASE_DIR = Path("/data/pyvideotrans-workflow")
CONFIG_FILE = BASE_DIR / "config/job_config.yaml"
STATUS_FILE = BASE_DIR / "status/current.json"
LOGS_DIR = BASE_DIR / "logs"
ARCHIVE_DIR = BASE_DIR / "status/archive"
WORK_DIR = BASE_DIR / "work"
DONE_DIR = BASE_DIR / "done"
ERROR_DIR = BASE_DIR / "error"
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
PVT_APP = BASE_DIR / "app/pyvideotrans"

# ─── 全局锁 ──────────────────────────────────────────────
LOCK_FILE = BASE_DIR / ".pvt_batch.lock"

# ─── 日志配置 ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("pvt_batch")


# ════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════

def load_config() -> Dict[str, Any]:
    """加载 job_config.yaml"""
    import yaml
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_status(status: Dict[str, Any]):
    """持久化 current.json"""
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def update_status(job_id: str, step: str, progress_note: str = "",
                  error: str = None, extra: Dict[str, Any] = None):
    """更新状态文件"""
    status = {
        "status": "error" if error else "running",
        "job_id": job_id,
        "current_step": step,
        "progress_note": progress_note,
        "error": error,
        "updated_at": datetime.now().isoformat(),
    }
    if extra:
        status.update(extra)
    save_status(status)


def generate_job_id(source_file: Path) -> str:
    """生成唯一 job_id"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = f"{timestamp}_{source_file.stem}_{os.getpid()}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def wait_for_stable_file(filepath: Path, min_interval: float = 3.0, max_wait: float = 120.0) -> bool:
    """等待文件大小稳定（避免半上传）"""
    last_size = -1
    start = time.time()
    while time.time() - start < max_wait:
        if not filepath.exists():
            time.sleep(1)
            continue
        size = filepath.stat().st_size
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(min_interval)
    return False


def safe_filename(name: str) -> str:
    """替换不安全字符为下划线"""
    keepcharacters = (" ", ".", "_", "-")
    return "".join(c if c.isalnum() or c in keepcharacters else "_" for c in name)



def normalize_srt_timestamps(content: str) -> str:
    """修复模型偶发破坏 SRT 时间轴的错误。"""
    # 00:00:00:000 -> 00:00:00,000（最优先，贪心问题最多的格式）
    content = re.sub(
        r"(\d{2}:\d{2}:\d{2}):(\d{3})(?=[\s\)]|$)",
        r"\1,\2",
        content,
    )
    # 00:00:00.000 -> 00:00:00,000
    content = re.sub(
        r"(\d{2}:\d{2}:\d{2})\.(\d{3})",
        r"\1,\2",
        content,
    )
    return content


def validate_srt(content: str, allow_bad_chunk: bool = False, start_num: int = 1) -> Tuple[bool, str, List[Tuple[int, str]]]:
    """
    校验 SRT 合法性：编号递增、时间轴格式、块结构
    返回 (是否合法, 错误信息, 坏块列表[(块序号, 错误原因)])
    当 allow_bad_chunk=True 时不因坏块返回 False，用于定位具体坏块
    start_num: 期望的第一个块编号，默认1（用于验证分 chunk 翻译后的 SRT）
    """
    blocks = re.split(r"\n\n+", content.strip())
    if not blocks:
        return False, "SRT 内容为空", []
    expected_idx = start_num
    bad_chunks = []
    for block_idx, block in enumerate(blocks):
        lines = block.strip().split("\n")
        if len(lines) < 2:
            bad_chunks.append((block_idx + 1, f"块{block_idx+1}格式错误: {repr(block[:50])}"))
            if not allow_bad_chunk:
                return False, f"块格式错误: {repr(block[:50])}", bad_chunks
            expected_idx += 1
            continue
        idx_match = re.match(r"^\s*(\d+)\s*$", lines[0])
        if not idx_match:
            bad_chunks.append((block_idx + 1, f"块{block_idx+1}编号行格式错误: {repr(lines[0])}"))
            if not allow_bad_chunk:
                return False, f"编号行格式错误: {repr(lines[0])}", bad_chunks
            expected_idx += 1
            continue
        idx = int(idx_match.group(1))
        if idx != expected_idx:
            bad_chunks.append((block_idx + 1, f"块{block_idx+1}编号不连续: 期望{expected_idx}, 实际{idx}"))
            if not allow_bad_chunk:
                return False, f"编号不连续: 期望{expected_idx}, 实际{idx}", bad_chunks
        expected_idx += 1
        ts_match = re.match(
            r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*$",
            lines[1],
        )
        if not ts_match:
            bad_chunks.append((block_idx + 1, f"块{block_idx+1}时间轴格式错误: {repr(lines[1])}"))
            if not allow_bad_chunk:
                return False, f"时间轴格式错误: {repr(lines[1])}", bad_chunks
            continue
        start_t, end_t = ts_match.group(1), ts_match.group(2)
        if start_t >= end_t:
            bad_chunks.append((block_idx + 1, f"块{block_idx+1}开始时间>=结束时间: {start_t} -> {end_t}"))
            if not allow_bad_chunk:
                return False, f"开始时间>=结束时间: {start_t} -> {end_t}", bad_chunks
    if bad_chunks:
        return False, bad_chunks[0][1], bad_chunks
    return True, "", bad_chunks


def renumber_srt(srt_text: str, start_num: int = 1) -> str:
    """将 SRT 字幕块重新编号，从 start_num 开始，顺序递增"""
    blocks = re.split(r'\n\n+', srt_text.strip())
    result = []
    for i, block in enumerate(blocks, start_num):
        lines = block.strip().split('\n')
        if not lines:
            continue
        lines[0] = str(i)
        result.append('\n'.join(lines))
    return '\n\n'.join(result)


def restore_srt_structure(source_srt: str, translated_srt: str) -> str:
    """用原 SRT 的编号和时间轴覆盖模型输出，只保留模型翻译正文。

    兜底逻辑：
    - 块数相等：沿用原逻辑（编号/时间轴回套）。
    - 块数不等：从 translated_srt 提取正文字符串，按序分配给 source blocks；
      不足时用 source 正文兜底，多余内容并入最后一块。
    """
    source_blocks = re.split(r"\n\n+", source_srt.strip())
    translated_blocks = re.split(r"\n\n+", translated_srt.strip())

    timeline_re = re.compile(
        r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}$"
    )

    # 情况1：块数相等，沿用原有严格逻辑
    if len(source_blocks) == len(translated_blocks):
        result = []
        for source_block, translated_block in zip(source_blocks, translated_blocks):
            source_lines = [line.strip() for line in source_block.splitlines() if line.strip()]
            if len(source_lines) < 2 or not timeline_re.match(source_lines[1]):
                return translated_srt

            translated_lines = [line.strip() for line in translated_block.splitlines() if line.strip()]
            if translated_lines and re.match(r"^\d+$", translated_lines[0]):
                translated_lines = translated_lines[1:]
            if translated_lines and timeline_re.match(translated_lines[0]):
                translated_lines = translated_lines[1:]

            body_lines = translated_lines or source_lines[2:] or [""]
            result.append("\n".join([source_lines[0], source_lines[1], *body_lines]))

        return "\n\n".join(result)

    # 情况2：块数不等，进入兜底逻辑
    # 策略：从 translated_blocks 提取每个块的正文字符串（去掉编号行和时间轴行），
    #       再按空行分块或按行顺序分配给 source blocks。
    #
    # 第一步：从每个 translated block 提取正文（纯文本行）
    def extract_body_lines(block_str: str) -> List[str]:
        """去掉纯数字行、去掉时间轴行，返回正文行列表"""
        lines = [ln.strip() for ln in block_str.splitlines() if ln.strip()]
        cleaned = []
        for ln in lines:
            if re.match(r"^\d+$", ln):
                continue  # 去掉编号行
            if timeline_re.match(ln):
                continue  # 去掉时间轴行
            cleaned.append(ln)
        return cleaned

    # 优先尝试：按空行块分割 translated_srt，每块提取正文后与 source 按序对应
    # 只有当 translated_blocks 数量与 source_blocks 可按位置对应时才这么做
    all_translated_bodies: List[List[str]] = [extract_body_lines(b) for b in translated_blocks]

    # 检查是否可以按块对应（translated_blocks 数量与 source 接近，或有明确的分段规律）
    # 如果 translated_blocks 数量 == source_blocks 数量但前面被判定为不等，说明是其他原因（如分块逻辑差异）
    # 此时直接用 all_translated_bodies 按顺序分配
    result = []
    total_src = len(source_blocks)
    total_trans = len(translated_blocks)

    # 每个 source block 分配一个翻译正文
    # 分配规则：按 index 对应；不足时用 source 正文兜底；多余内容并入最后一块
    for idx, source_block in enumerate(source_blocks):
        source_lines = [ln.strip() for ln in source_block.splitlines() if ln.strip()]
        if len(source_lines) < 2 or not timeline_re.match(source_lines[1]):
            # source 块本身格式已坏，无法回套，用原翻译块
            result.append(source_block)
            continue

        if idx < total_trans:
            body = all_translated_bodies[idx]
        else:
            body = []  # 不足，兜底用 source 正文

        if body:
            result.append("\n".join([source_lines[0], source_lines[1], *body]))
        else:
            # 用 source 正文兜底（确保不生成空正文）
            fallback = source_lines[2:] if len(source_lines) > 2 else [""]
            result.append("\n".join([source_lines[0], source_lines[1], *fallback]))

    # 多余内容（translated 有但 source 不够）并入最后一块
    if total_trans > total_src:
        overflow = all_translated_bodies[total_src:]
        if overflow:
            flat_overflow = []
            for chunk in overflow:
                flat_overflow.extend(chunk)
            if flat_overflow and result:
                last = result[-1]
                # 追加到最后一行的正文后面（用空格分隔，避免破坏 SRT 块结构）
                parts = last.split("\n", 2)
                if len(parts) >= 3:
                    result[-1] = parts[0] + "\n" + parts[1] + "\n" + parts[2].rstrip() + " " + " ".join(flat_overflow)
                elif len(parts) == 2:
                    result[-1] = last + " " + " ".join(flat_overflow)
                else:
                    result[-1] = last + " " + " ".join(flat_overflow)

    return "\n\n".join(result)


def clean_llm_commentary(text: str) -> str:
    """
    清理 LLM 输出的多余解释、markdown、思考过程、提示词残留。
    适用于翻译任务（可能混入【强制要求】等提示词内容）。
    """
    # 去除 markdown code block 标记
    text = re.sub(r"```srt\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*\$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*```.*$", "", text, flags=re.MULTILINE)

    # 去除 markdown 标题/强调/列表
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)

    # 去除"翻译如下/结果/解释/提示"等引导语
    text = re.sub(r"^.*(?:翻译|translat|结果|如下)[:：]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^.*(?:解释|note|comment|注意|注|备注)[:：]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^(?:思考|分析|首先|其次|最后)[:：].*$", "", text, flags=re.MULTILINE)

    # 去除混入的提示词标记（【强制要求】、【输出格式】、【待翻译】等）
    text = re.sub(r"【[^】]*】", "", text)

    # 去除残留的 markdown 思考标签
    text = re.sub(r"<[^>]+>", "", text)

    # 去除多余空行
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def parse_manifest_log(job_log_path: Path) -> str:
    """读取 job 日志末尾"""
    if not job_log_path.exists():
        return ""
    try:
        with open(job_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-50:]) if len(lines) > 50 else "".join(lines)
    except Exception:
        return ""


def hms_add_ms(hms: str, ms: int) -> Tuple[str, int]:
    """
    给 HH:MM:SS, 加上 ms 毫秒，返回新的 (HH:MM:SS, 进位ms)
    ms 范围 0-999，进位时秒+1，跨分跨时正确进位
    """
    parts = hms.split(":")
    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    total_ms = s * 1000 + ms
    new_s = total_ms // 1000
    carry_ms = total_ms % 1000
    total_s = s + new_s
    new_s = total_s % 60
    carry_s = total_s // 60
    total_m = m + carry_s
    new_m = total_m % 60
    carry_h = total_m // 60
    new_h = h + carry_h
    return f"{new_h:02d}:{new_m:02d}:{new_s:02d}", carry_ms


def fix_zero_duration(m):
    """
    修复零时长字幕：开始时间==结束时间时，结束时间增加 1ms
    跨秒时正确进位（如 00:19:59,999 --> 00:19:59,999 → 00:20:00,000）
    """
    hms = m.group(1)
    ms = int(m.group(2))
    end_hms = m.group(3)
    end_ms = int(m.group(4))

    if hms == end_hms and ms == end_ms:
        # 结束时间 = 开始时间，增加 1ms
        new_end_hms, new_end_ms = hms_add_ms(end_hms.rstrip(','), 1)
        return f'{hms}{ms:03d} --> {new_end_hms},{new_end_ms:03d}'
    return m.group(0)


# ════════════════════════════════════════════════════════
# Ollama 翻译
# ════════════════════════════════════════════════════════

def check_ollama_model(model_name: str, api_base: str) -> Tuple[bool, str]:
    """检查 Ollama 模型是否存在"""
    try:
        req = urllib.request.Request(
            f"{api_base}/models",
            headers={"Authorization": "Bearer 1234"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for m in data.get("data", []):
                if m["id"] == model_name:
                    return True, "ok"
        return False, f"模型 {model_name} 不在 Ollama 中"
    except Exception as e:
        return False, f"Ollama 检查失败: {e}"





ASR_INITIAL_PROMPT_JA = ""
ASR_HOTWORDS_JA = (
    "生殖器官,子宮,卵巣,膣,外陰部,陰茎,精巣,睾丸,前立腺,乳房,乳首,"
    "陰部,性交,射精,勃起,妊娠,避妊,性病,性感染症,くそ,エロ,下品,卑猥,"
    "おまんこ,ちんこ,まんこ,ペニス,クリトリス,アナル,フェラ,中出し,潮吹き"
)


def ensure_pvt_asr_settings() -> None:
    """pyVideoTrans 可能在运行后清空热词；每次启动和 STT 前都写回关键设置。"""
    cfg_path = PVT_APP / "videotrans/cfg.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    changed = False
    desired = {
        "initial_prompt_ja": ASR_INITIAL_PROMPT_JA,
        "hotwords": ASR_HOTWORDS_JA,
        "no_speech_threshold": 0.20,
        "min_speech_duration_ms": 250,
        "min_silence_duration_ms": 80,
        "max_speech_duration_s": 8,
        "condition_on_previous_text": False,
        "cuda_com_type": "float16",
    }
    for key, value in desired.items():
        if data.get(key) != value:
            data[key] = value
            changed = True
    if changed:
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        logger.info("  已恢复 pyVideoTrans 日语 ASR 提示词和热词设置")


def unload_ollama_model(model_name: str, api_base: str) -> None:
    """在 STT 前停止 Ollama runner，给 faster-whisper large-v3 留出显存。"""
    try:
        result = subprocess.run(
            ["ollama", "stop", model_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info(f"  Ollama 已停止常驻模型: {model_name}")
            time.sleep(2)
        else:
            msg = (result.stderr or result.stdout or "").strip()
            logger.warning(f"  Ollama 停止常驻模型失败，继续尝试 STT: {msg[:200]}")
    except Exception as e:
        logger.warning(f"  Ollama 停止常驻模型异常，继续尝试 STT: {e}")


def validate_startup_settings(config: Dict[str, Any]) -> None:
    """启动时确认 STT 和翻译两套关键设置都已生效。"""
    problems = []
    stt_cfg = config.get("stt", {})
    trans_cfg = config.get("translation", {})

    if stt_cfg.get("detect_language") != "ja":
        problems.append("STT detect_language 必须是 ja，才能启用日语 Whisper 提示词")

    cfg_path = PVT_APP / "videotrans/cfg.json"
    try:
        pvt_settings = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        problems.append(f"无法读取 pyVideoTrans cfg.json: {e}")
        pvt_settings = {}

    initial_prompt_ja = pvt_settings.get("initial_prompt_ja") or ""
    hotwords = pvt_settings.get("hotwords") or ""
    required_terms = ["生殖器官", "子宮", "卵巣", "膣", "陰茎", "精巣", "前立腺"]
    if not all(term in hotwords for term in required_terms):
        problems.append("STT hotwords 缺少医学/敏感词热词")

    no_speech_threshold = pvt_settings.get("no_speech_threshold")
    try:
        if float(no_speech_threshold) > 0.4:
            problems.append("STT no_speech_threshold 偏高，可能跳过低声/短词")
    except (TypeError, ValueError):
        problems.append("STT no_speech_threshold 不是有效数字")

    model_name = trans_cfg.get("selected_model")
    if model_name != "qwen2.5:7b-instruct":
        problems.append("翻译模型必须是 qwen2.5:7b-instruct")

    api_base = None
    for base in trans_cfg.get("api_base_candidates", []):
        ok, msg = check_ollama_model(model_name, base)
        if ok:
            api_base = base
            break
        logger.info(f"  启动自检: {base} -> {msg}")
    if api_base is None:
        problems.append("Ollama 中找不到 qwen2.5:7b-instruct")

    bad_model_found = False
    if api_base:
        try:
            req = urllib.request.Request(
                f"{api_base}/models",
                headers={"Authorization": "Bearer 1234"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            ids = {m.get("id") for m in data.get("data", [])}
            bad_model_found = "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M" in ids
        except Exception as e:
            problems.append(f"无法读取 Ollama 模型列表: {e}")
    if bad_model_found:
        problems.append("Ollama 中仍存在原始坏模板 Hy-MT2，请删除 hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")

    logger.info("  启动自检: STT 语言=ja")
    logger.info(f"  启动自检: STT 提示词已关闭防幻听={initial_prompt_ja == ''}")
    logger.info(f"  启动自检: STT 热词包含陰茎={'陰茎' in hotwords}")
    logger.info(f"  启动自检: STT 静音阈值={no_speech_threshold}")
    logger.info(f"  启动自检: 翻译模型={model_name}")
    logger.info(f"  启动自检: Ollama 地址={api_base or '未通过'}")

    if problems:
        for problem in problems:
            logger.error(f"  启动自检失败: {problem}")
        raise RuntimeError("启动自检失败，已阻止工作流启动。请先修复 STT/翻译模型设置。")

    logger.info("  启动自检通过: STT 敏感词识别提示 + qwen2.5 翻译提示均已生效")


REFUSAL_PATTERNS = [
    r"无法(翻译|处理|提供|协助|满足)",
    r"不能(翻译|处理|提供|协助|满足)",
    r"抱歉[^\n]{0,30}(无法|不能)",
    r"对不起[^\n]{0,30}(无法|不能)",
    r"I\s+(can't|cannot|can not)\s+(translate|help|provide|assist)",
    r"sorry[^\n]{0,40}(cannot|can not|can't)",
]


def contains_refusal_text(content: str) -> bool:
    return any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in REFUSAL_PATTERNS)


def contains_kana_residue(srt_text: str) -> bool:
    """
    检测 SRT 正文中是否残留明显日文假名。
    策略：提取每块正文字符串（去掉编号行和时间轴行），
    对纯文本行用 [ぁ-んァ-ン]{2,} 检测。
    匹配到连续2个及以上假名字符即为阳性。
    """
    timeline_re = re.compile(
        r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}$"
    )
    blocks = re.split(r"\n\n+", srt_text.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        body_lines = []
        for ln in lines:
            if re.match(r"^\d+$", ln):
                continue  # 编号行
            if timeline_re.match(ln):
                continue  # 时间轴行
            body_lines.append(ln)
        for body in body_lines:
            if re.search(r"[ぁ-んァ-ン]{2,}", body):
                return True
    return False


def repair_kana_residue_chunk(
    chunk_srt: str,
    translated_chunk: str,
    api_base: str,
    model_name: str,
    target_lang: str = "简体中文",
    temperature: float = 0.1,
) -> Tuple[Optional[str], str]:
    """
    修补翻译结果中的日文假名残留。
    调用 Ollama API，让模型只修复翻译正文中的假名残留，
    严格保持编号、时间轴、块数。
    返回 (修补后SRT, 错误信息)
    """
    repair_prompt = f"""以下是一段字幕的原文和翻译结果。翻译结果中残留了日文假名，需要你修复。

要求：
1. 只输出修复后的 SRT 字幕，不输出任何解释、思考过程或额外说明
2. 严格保持原始字幕的编号、时间轴和块数（一个都不能多，一个都不能少）
3. 时间轴格式: 00:00:00,000 --> 00:00:00,000（原样保留）
4. 每条字幕块之间用一个空行分隔
5. 只修复翻译正文中的日文假名残留，不要改动编号和时间轴
6. 修复后的字幕正文必须全部是中文，不能包含任何日文假名（ひらがな、片假名）

原文（SRT，只读编号和时间轴，正文仅供参考）：
{chunk_srt}

翻译结果（需要修复假名残留，只改正文）：
{translated_chunk}
"""

    try:
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": repair_prompt,
                }
            ],
            "temperature": temperature,
            "stream": False,
        }

        req = urllib.request.Request(
            f"{api_base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer 1234",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            repaired = result["choices"][0]["message"]["content"]

        repaired = clean_llm_commentary(repaired)
        repaired = normalize_srt_timestamps(repaired)
        repaired = restore_srt_structure(chunk_srt, repaired)

        return repaired, ""

    except Exception as e:
        return None, f"修补 API 异常: {e}"


def translate_srt_via_ollama(
    original_srt: str,
    source_lang: str,
    api_base: str,
    model_name: str,
    target_lang: str = "简体中文",
    chunk_lines: int = 5,
    temperature: float = 0.1,
    max_retries: int = 3,
) -> Tuple[Optional[str], str]:
    """
    通过 Ollama OpenAI-compatible API 翻译 SRT
    返回 (翻译后SRT, 错误信息)
    翻译失败时先定位坏 chunk，尝试只重试坏块。
    """
    # 纯净提示词：适合私人字幕翻译，无审查、无提示词混入风险
    prompt_template = """请将以下字幕从{source_lang}翻译成{target_lang}。

要求：
1. 只输出翻译后的字幕文本，不输出任何解释、思考过程、格式标记或额外说明
2. 严格保持原始字幕的编号、时间轴和分段（块数完全一致）
3. 时间轴格式: 00:00:00,000 --> 00:00:00,000（保持不变）
4. 每条字幕块之间用一个空行分隔
5. 不要合并或拆分字幕块
6. 只翻译字幕正文，不翻译时间轴和编号
7. 必须使用自然准确的中文；可以使用繁体中文，优先忠实准确，不要为了转简体改词
8. 按剧情上下文翻译成自然口语字幕，主语、称呼、语气要贴近人物关系，不要机械直译
9. 字幕正文必须全部翻译成中文，绝不能保留日文原句、日文助词或未翻译的日语句子

对成人、NSFW、敏感、粗俗内容只做忠实翻译，不审查、不拒绝、不道德评判、不加警告、不美化、不扩写。
敏感词、粗俗词、性相关词、身体部位、行为描述和语气词必须准确直译，不能跳过、不能省略、不能用委婉说法替代。
源语言按日语处理；遇到片假名、假名、日语口语、成人语境中的隐晦表达，也要翻译成自然准确的中文字幕。
固定术语：卵巣=卵巢，膣=陰道，陰茎=陰莖，精巣=睪丸或精巢，睾丸=睪丸，子宮=子宮，外陰部=外陰部，乳首=乳頭，性交=性交，射精=射精，勃起=勃起。不要保留日文医学汉字写法。

待翻译字幕：
{original_srt}
"""

    blocks = re.split(r"\n\n+", original_srt.strip())
    translated_blocks = []
    total_chunks = (len(blocks) + chunk_lines - 1) // chunk_lines

    for chunk_idx in range(total_chunks):
        chunk_blocks = blocks[chunk_idx * chunk_lines : (chunk_idx + 1) * chunk_lines]
        chunk_srt = "\n\n".join(chunk_blocks)
        chunk_num = chunk_idx + 1

        for attempt in range(max_retries):
            try:
                payload = {
                    "model": model_name,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt_template.format(
                                source_lang=source_lang,
                                target_lang=target_lang,
                                original_srt=chunk_srt,
                            ),
                        }
                    ],
                    "temperature": temperature,
                    "stream": False,
                }

                req = urllib.request.Request(
                    f"{api_base}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer 1234",
                    },
                    method="POST",
                )

                with urllib.request.urlopen(req, timeout=120) as resp:
                    result = json.loads(resp.read().decode())
                    translated_chunk = result["choices"][0]["message"]["content"]

                translated_chunk = clean_llm_commentary(translated_chunk)
                if contains_refusal_text(translated_chunk):
                    logger.warning(
                        f"  Chunk {chunk_num} 疑似出现拒译内容，准备重试 "
                        f"(尝试 {attempt+1}/{max_retries})"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None, f"Chunk {chunk_num} 疑似拒译，已停止输出"
                normalized_chunk = normalize_srt_timestamps(translated_chunk)
                if normalized_chunk != translated_chunk:
                    logger.info(f"  Chunk {chunk_num} 时间轴格式已自动修复")
                translated_chunk = normalized_chunk
                start_num = chunk_idx * chunk_lines + 1
                translated_chunk = renumber_srt(translated_chunk, start_num=start_num)
                restored_chunk = restore_srt_structure(chunk_srt, translated_chunk)
                if restored_chunk != translated_chunk:
                    logger.info(f"  Chunk {chunk_num} 编号和时间轴已按原字幕回套")
                translated_chunk = restored_chunk

                # 验证此 chunk 翻译结果
                valid_t, valid_t_err, bad_chunks = validate_srt(
                    translated_chunk, allow_bad_chunk=True, start_num=start_num
                )
                if not valid_t and bad_chunks:
                    logger.warning(
                        f"  Chunk {chunk_num} 翻译结果有坏块: {bad_chunks[0][1]} "
                        f"(尝试 {attempt+1}/{max_retries})"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue  # 重试
                    else:
                        # 耗尽所有重试次数的最后防线：
                        # 当模型输出的块数量与原 chunk 相同时，说明只是编号/时间轴被模型写坏，
                        # 内容文本可能仍然正确。此时用原 chunk 的编号和时间轴强制回套，再验证。
                        source_count = len(re.split(r"\n\n+", chunk_srt.strip()))
                        translated_count = len(re.split(r"\n\n+", translated_chunk.strip()))
                        if source_count == translated_count:
                            forced_restored = restore_srt_structure(chunk_srt, translated_chunk)
                            valid_final, final_err, final_bad = validate_srt(
                                forced_restored, allow_bad_chunk=True, start_num=start_num
                            )
                            if valid_final:
                                # 强制回套修复后仍需通过日文假名残留检查
                                if contains_kana_residue(forced_restored):
                                    kana_warn = (
                                        f"Chunk {chunk_num} 回套修复后仍有日文假名残留，尝试修补 "
                                        f"(尝试 {attempt+1}/{max_retries})"
                                    )
                                    logger.warning(kana_warn)
                                    # 先尝试修补，不直接重试
                                    repaired, repair_err = repair_kana_residue_chunk(
                                        chunk_srt,
                                        forced_restored,
                                        api_base,
                                        model_name,
                                        target_lang,
                                        temperature,
                                    )
                                    if repaired is not None:
                                        repaired_valid, _, _ = validate_srt(
                                            repaired, allow_bad_chunk=True, start_num=start_num
                                        )
                                        if repaired_valid and not contains_kana_residue(repaired):
                                            logger.warning(
                                                f"  Chunk {chunk_num} 修补成功，假名残留已清除"
                                            )
                                            translated_blocks.append(repaired)
                                            logger.info(f"  Chunk {chunk_num}/{total_chunks} 翻译成功（修补修复）")
                                            break
                                        else:
                                            logger.warning(
                                                f"  Chunk {chunk_num} 修补后仍不通过验证，继续重试逻辑"
                                            )
                                    else:
                                        logger.warning(
                                            f"  Chunk {chunk_num} 修补失败: {repair_err}"
                                        )
                                    # 修补不成功，按原 retry 逻辑重试原 chunk
                                    if attempt < max_retries - 1:
                                        time.sleep(2 ** attempt)
                                        continue  # 重试
                                    else:
                                        return None, (
                                            f"Chunk {chunk_num} 翻译后仍有日文假名残留，已停止输出"
                                        )
                                logger.warning(
                                    f"  Chunk {chunk_num} 重试耗尽，但回套修复成功（块数={source_count}）"
                                )
                                translated_blocks.append(forced_restored)
                                logger.info(f"  Chunk {chunk_num}/{total_chunks} 翻译成功（回套修复）")
                                break
                        return None, (
                            f"Chunk {chunk_num} 翻译输出不是合法 SRT: "
                            f"{bad_chunks[0][1]}"
                        )

                # 日文假名残留质量门：先尝试修补，修补成功则继续；修补失败再重试
                if contains_kana_residue(translated_chunk):
                    kana_warning = (
                        f"Chunk {chunk_num} 翻译后仍有日文假名残留，尝试修补 "
                        f"(尝试 {attempt+1}/{max_retries})"
                    )
                    logger.warning(kana_warning)
                    repaired, repair_err = repair_kana_residue_chunk(
                        chunk_srt,
                        translated_chunk,
                        api_base,
                        model_name,
                        target_lang,
                        temperature,
                    )
                    if repaired is not None:
                        repaired_valid, _, _ = validate_srt(
                            repaired, allow_bad_chunk=True, start_num=start_num
                        )
                        if repaired_valid and not contains_kana_residue(repaired):
                            logger.warning(
                                f"  Chunk {chunk_num} 修补成功，假名残留已清除"
                            )
                            translated_blocks.append(repaired)
                            logger.info(f"  Chunk {chunk_num}/{total_chunks} 翻译成功（修补修复）")
                            break
                        else:
                            logger.warning(
                                f"  Chunk {chunk_num} 修补后仍不通过验证，继续重试逻辑"
                            )
                    else:
                        logger.warning(
                            f"  Chunk {chunk_num} 修补失败: {repair_err}"
                        )
                    # 修补不成功，按原 retry 逻辑重试原 chunk
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue  # 重试
                    else:
                        return None, (
                            f"Chunk {chunk_num} 翻译后仍有日文假名残留，已停止输出"
                        )

                translated_blocks.append(translated_chunk)
                logger.info(f"  Chunk {chunk_num}/{total_chunks} 翻译成功")
                break

            except Exception as e:
                logger.warning(
                    f"  Chunk {chunk_num}/{total_chunks} 翻译失败 "
                    f"(尝试 {attempt+1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue  # 重试
                else:
                    # 耗尽所有重试次数
                    return None, f"Chunk {chunk_num} 翻译异常: {e}"

    result_srt = "\n\n".join(translated_blocks)

    return result_srt, ""


# ════════════════════════════════════════════════════════
# pyVideoTrans STT
# ════════════════════════════════════════════════════════

def run_stt(video_path: Path, job_work_dir: Path, config: Dict[str, Any],
            stt_start_time: float) -> Tuple[Optional[Path], str, str]:
    """
    运行 pyVideoTrans STT
    返回 (original_srt路径, 检测到的语言, 错误信息)
    stt_start_time: STT 开始时间戳（用于过滤 SRT 文件）
    """
    stt_cfg = config["stt"]
    model_name = stt_cfg["model_name"]
    use_cuda = stt_cfg.get("use_cuda", "auto")
    detect_lang = stt_cfg.get("detect_language", "auto")
    fix_punc = stt_cfg.get("fix_punc", True)
    remove_noise = stt_cfg.get("remove_noise", False)

    cuda_flag = ["--cuda"] if use_cuda in ("auto", "true", "yes", "1") else []
    detect_flag = ["--detect_language", detect_lang] if detect_lang != "auto" else []
    punc_flag = ["--fix_punc"] if fix_punc else []
    noise_flag = ["--remove_noise"] if remove_noise else []

    output_name = f"original"
    output_path = job_work_dir / f"{output_name}.srt"

    uv_bin = shutil.which("uv")
    if not uv_bin:
        return None, "", "STT 依赖 uv，但系统 PATH 中找不到 uv"

    cmd = [
        uv_bin, "run", "--offline", "--no-sync", "cli.py",
        "--task", "stt",
        "--name", str(video_path),
        "--recogn_type", str(stt_cfg.get("recogn_type", 0)),
        "--model_name", model_name,
        *cuda_flag,
        *detect_flag,
        *punc_flag,
        *noise_flag,
    ]

    logger.info(f"  STT 命令: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(PVT_APP),
            capture_output=True,
            text=True,
            timeout=3600,  # 1小时超时
            env={
                **os.environ,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_ENDPOINT": "",
                "HF_HOME": str(PVT_APP / "models"),
                "MODELSCOPE_CACHE": str(PVT_APP / "models"),
            },
        )

        if result.returncode != 0:
            logger.error(f"  STT stderr: {result.stderr[:500]}")
            return None, "", f"STT 失败 (exit {result.returncode}): {result.stderr[:300]}"

        # 查找输出文件：只接受 mtime >= stt_start_time 的 SRT（防复用）
        possible_paths = [
            job_work_dir / f"{output_name}.srt",
            job_work_dir / "temp_diarize" / f"{output_name}.srt",
            job_work_dir / f"{video_path.stem}.srt",
            PVT_APP / "temp_diarize" / f"{output_name}.srt",
            # cli.py 实际输出路径
            PVT_APP / "output" / "recogn" / f"{video_path.stem}.srt",
            PVT_APP / "output" / "recogn" / f"{output_name}.srt",
        ]
        found_path = None
        for p in possible_paths:
            if p.exists() and p.stat().st_mtime >= stt_start_time:
                found_path = p
                break

        # 通配搜索 output 目录（时间戳过滤）
        if not found_path:
            for p in (PVT_APP / "output").glob("**/*.srt"):
                if p.stat().st_size > 1000 and p.stat().st_mtime >= stt_start_time:
                    found_path = p
                    break

        if found_path:
            dest = job_work_dir / f"{output_name}.srt"
            shutil.copy2(found_path, dest)
            lang = detect_lang
            lang_info_file = job_work_dir / "language_detected.txt"
            if lang_info_file.exists():
                with open(lang_info_file, "r") as f:
                    lang = f.read().strip()
            return dest, lang, ""
        return None, "", f"STT 完成但找不到输出文件。stdout: {result.stdout[:300]}"

    except subprocess.TimeoutExpired:
        return None, "", "STT 超时（超过1小时）"
    except Exception as e:
        return None, "", f"STT 异常: {e}"


# ════════════════════════════════════════════════════════
# 输入目录清理
# ════════════════════════════════════════════════════════

def cleanup_input_dir(original_video_path: Path):
    """
    清理输入目录中的隐藏垃圾文件和空目录。
    从视频原父目录向上清理到 INPUT_DIR 为止（不包含 INPUT_DIR 本身）。
    不清理 output/work/logs/status/config/app 等目录。
    只清理 input 树下的目录。
    """
    # 安全加固：确认视频父目录在 INPUT_DIR 之下
    parent = original_video_path.parent
    try:
        parent.relative_to(INPUT_DIR)
    except ValueError:
        logger.warning(f"拒绝清理：视频父目录不在 INPUT_DIR 之下: {parent}")
        return

    protected_names = {"output", "work", "logs", "status", "config", "app", "input"}

    current = parent
    input_dir = INPUT_DIR

    while current != input_dir and current.is_dir():
        if current.name in protected_names:
            break

        try:
            # 第一遍：删除所有 .DS_Store 和 ._* 文件
            for item in list(current.iterdir()):
                try:
                    if item.name in (".DS_Store", "._.DS_Store"):
                        item.unlink()
                        logger.info(f"  清理隐藏文件: {item}")
                    elif item.name.startswith("._"):
                        item.unlink()
                        logger.info(f"  清理隐藏文件: {item}")
                except Exception as e:
                    logger.warning(f"  删除失败 {item}: {e}")

            # 第二遍：如果目录为空则删除
            if not any(current.iterdir()):
                try:
                    current.rmdir()
                    logger.info(f"  清理空目录: {current}")
                except Exception as e:
                    logger.warning(f"  删除空目录失败 {current}: {e}")
        except Exception as e:
            logger.warning(f"  处理目录失败 {current}: {e}")

        current = current.parent


# ════════════════════════════════════════════════════════
# 归档工具
# ════════════════════════════════════════════════════════

def archive_job_artifacts(
    job_id: str,
    job_output_dir: Path,
    job_work_dir: Path,
    video_stem: str,
    video_name: str,
    original_srt_content: str,
    translated_srt: str,
    manifest: Dict[str, Any],
) -> bool:
    """
    将 job.log / manifest / original.srt / original.txt / translated.*.srt / translated.*.txt
    归档到 ARCHIVE_DIR / <job_id>/ 下。
    返回是否归档成功（失败不影响主流程）。
    """
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        arc = ARCHIVE_DIR / job_id
        arc.mkdir(parents=True, exist_ok=True)

        # original.srt
        (arc / "original.srt").write_text(original_srt_content, encoding="utf-8")

        # original.txt
        original_txt = re.sub(r"^\d+\s*$", "", original_srt_content, flags=re.MULTILINE)
        original_txt = re.sub(r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s*$", "", original_txt, flags=re.MULTILINE)
        original_txt = re.sub(r"\n{3,}", "\n\n", original_txt).strip()
        (arc / "original.txt").write_text(original_txt, encoding="utf-8")

        # translated.zh-cn.srt
        (arc / "translated.zh-cn.srt").write_text(translated_srt, encoding="utf-8")

        # translated.zh-cn.txt
        translated_txt = re.sub(r"^\d+\s*$", "", translated_srt, flags=re.MULTILINE)
        translated_txt = re.sub(r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s*$", "", translated_txt, flags=re.MULTILINE)
        translated_txt = re.sub(r"\n{3,}", "\n\n", translated_txt).strip()
        (arc / "translated.zh-cn.txt").write_text(translated_txt, encoding="utf-8")

        # manifest.json
        (arc / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # job.log（从 LOGS_DIR 复制）
        job_log_file = LOGS_DIR / f"job_{job_id}.log"
        if job_log_file.exists():
            shutil.copy2(job_log_file, arc / "job.log")

        logger.info(f"  工件已归档至: {arc}")
        return True
    except Exception as e:
        logger.warning(f"  归档失败（不影响主流程）: {e}")
        return False


# ════════════════════════════════════════════════════════
# 核心任务处理
# ════════════════════════════════════════════════════════

def process_video(video_path: Path, config: Dict[str, Any]) -> bool:
    """
    处理单个视频，返回是否成功

    视频主名 = video_path.stem（不含扩展名）
    输出目录 = OUTPUT_DIR / <视频主名>/
    成功时：视频 → OUTPUT_DIR/<视频主名>/<视频原文件名>
            同名字幕 → OUTPUT_DIR/<视频主名>/<视频主名>.srt
    失败时：视频 → ERROR_DIR / <相对输入路径>/
    """
    job_id = generate_job_id(video_path)
    video_stem = video_path.stem          # FAD-1219
    video_name = video_path.name           # FAD-1219.avi
    relative_path = video_path.relative_to(INPUT_DIR)  # FAD-1219/FAD-1219.avi
    error_subdir = ERROR_DIR / str(relative_path.parent)  # ERROR_DIR/FAD-1219/

    job_work_dir = WORK_DIR / job_id
    job_output_dir = OUTPUT_DIR / video_stem
    job_log_file = LOGS_DIR / f"job_{job_id}.log"

    job_status: Dict[str, Any] = {
        "status": "starting",
        "job_id": job_id,
        "source_file": str(video_path),
        "current_step": "init",
        "progress_note": "",
        "error": None,
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "output_dir": str(job_output_dir),
        "latest_log": "",
        "detected_language": None,
        "stt_model": None,
        "stt_use_cuda": False,
        "translation_model": None,
        "ollama_base_url": None,
        "manifest_notes": [],
        "model_fallback": False,
    }

    file_handler = logging.FileHandler(job_log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    logger.info(f"=== 开始处理任务 {job_id} ===")
    logger.info(f"  源文件: {video_path}")
    logger.info(f"  视频主名: {video_stem}")
    logger.info(f"  工作目录: {job_work_dir}")
    logger.info(f"  输出目录: {job_output_dir}")

    try:
        job_work_dir.mkdir(parents=True, exist_ok=True)
        job_output_dir.mkdir(parents=True, exist_ok=True)

        update_status(job_id, "waiting_file_stable", f"等待文件稳定: {video_path.name}", extra=job_status)
        logger.info("  等待文件大小稳定...")
        if not wait_for_stable_file(video_path):
            raise Exception("文件大小在等待期间未稳定，可能还在上传中")
        logger.info(f"  文件稳定，大小: {video_path.stat().st_size} bytes")

        # 复制视频到 work 目录（安全文件名）
        safe_name = safe_filename(video_name)
        work_video = job_work_dir / safe_name
        if safe_name != video_name or " " in video_name:
            shutil.copy2(video_path, work_video)
            logger.info(f"  视频复制为安全文件名: {work_video}")
        else:
            work_video = video_path

        # 检测 Ollama 模型
        update_status(job_id, "checking_ollama", "检查 Ollama 模型...", extra=job_status)
        trans_cfg = config["translation"]
        model_name = trans_cfg["selected_model"]
        api_base = None
        for base in trans_cfg["api_base_candidates"]:
            ok, msg = check_ollama_model(model_name, base)
            if ok:
                api_base = base
                break
            logger.info(f"  {base} -> {msg}")

        if api_base is None:
            err = f"{model_name} not found in Ollama."
            logger.error(f"  {err}")
            job_status["manifest_notes"].append(err)
            job_status["error"] = err
            job_status["status"] = "error"
            job_status["finished_at"] = datetime.now().isoformat()
            save_status(job_status)
            error_subdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(video_path, error_subdir / f"{job_id}_{video_name}")
            if job_log_file.exists():
                shutil.copy2(job_log_file, error_subdir / f"{job_id}.log")
            return False

        job_status["ollama_base_url"] = api_base
        job_status["translation_model"] = model_name
        logger.info(f"  Ollama 模型确认: {model_name} @ {api_base}")
        unload_ollama_model(model_name, api_base)

        # STT 阶段：记录开始时间戳（用于过滤 SRT）
        stt_start_time = time.time()

        update_status(job_id, "stt", "正在进行语音识别 (STT)...", extra=job_status)
        ensure_pvt_asr_settings()
        logger.info("  开始 STT...")
        original_srt_path, detected_lang, stt_err = run_stt(work_video, job_work_dir, config, stt_start_time)

        if stt_err:
            # large-v3 离线失败时降级到 medium
            if "snapshot_download" in stt_err or "remote repo cannot be accessed" in stt_err:
                fallback_model = config["stt"].get("fallback_model_name", "medium")
                # 确认 fallback 模型在本地可用，避免空降级
                fallback_local_dir = PVT_APP / "models" / f"models--Systran--faster-whisper-{fallback_model.replace('/', '--')}"
                if not fallback_local_dir.exists() or not any(fallback_local_dir.glob("*.bin")):
                    raise Exception(f"STT 降级失败: fallback 模型 {fallback_model} 本地不存在，无法使用")
                logger.warning(f"  large-v3 离线加载失败，尝试降级到 {fallback_model}...")
                logger.info(f"  [降级] large-v3 → {fallback_model} 写入日志")
                job_status["manifest_notes"].append(f"[降级] large-v3 → {fallback_model}")

                # 保存原始模型名，fallback 结束后恢复（不污染后续任务）
                original_model_name = config["stt"]["model_name"]
                try:
                    # 修改配置使用 fallback 模型重试
                    config["stt"]["model_name"] = fallback_model
                    job_status["stt_model"] = fallback_model
                    original_srt_path, detected_lang, stt_err = run_stt(work_video, job_work_dir, config, stt_start_time)

                    if not stt_err:
                        job_status["model_fallback"] = True
                        job_status["manifest_notes"].append(f"[降级成功] 使用模型: {fallback_model}")
                        logger.info(f"  降级模型 {fallback_model} STT 成功")
                    else:
                        raise Exception(f"STT 降级失败: {stt_err}")
                finally:
                    # 恢复原始模型名，防止污染后续任务
                    config["stt"]["model_name"] = original_model_name
            else:
                raise Exception(f"STT 失败: {stt_err}")

        job_status["detected_language"] = detected_lang
        job_status["stt_model"] = config["stt"]["model_name"]
        job_status["stt_use_cuda"] = config["stt"].get("use_cuda", "auto") in ("auto", "true", "yes", "1")
        logger.info(f"  STT 完成，检测语言: {detected_lang}")

        # 读取 original.srt
        with open(original_srt_path, "r", encoding="utf-8") as f:
            original_srt_content = f.read()

        # 零时长字幕修复（跨秒正确进位）
        original_srt_content = re.sub(
            r'(\d{2}:\d{2}:\d{2},)(\d{3}) --> (\d{2}:\d{2}:\d{2},)(\d{3})',
            fix_zero_duration,
            original_srt_content,
        )
        valid, valid_err, _ = validate_srt(original_srt_content)
        if not valid:
            raise Exception(f"STT 输出的 SRT 不合法: {valid_err}")

        # 翻译
        update_status(job_id, "translating", "正在翻译字幕...", extra=job_status)
        logger.info("  开始翻译...")
        source_lang = "日语"
        translated_srt, trans_err = translate_srt_via_ollama(
            original_srt_content,
            source_lang=source_lang,
            api_base=api_base,
            model_name=model_name,
            target_lang=trans_cfg["target_language_code"],
            chunk_lines=trans_cfg.get("chunk_lines", 5),
            temperature=trans_cfg.get("temperature", 0.1),
            max_retries=trans_cfg.get("max_retries", 3),
        )

        if trans_err:
            raise Exception(f"翻译失败: {trans_err}")

        # 清理和验证翻译结果
        translated_srt = translated_srt.strip()
        translated_srt = re.sub(r"^\s*```\s*", "", translated_srt)
        translated_srt = re.sub(r"\s*```\s*$", "", translated_srt)

        # 再次清理（防止首次遗漏的提示词残留）
        translated_srt = clean_llm_commentary(translated_srt)

        translated_srt = normalize_srt_timestamps(translated_srt)
        valid_t, valid_t_err, bad_chunks = validate_srt(translated_srt)
        if not valid_t:
            # 第一次清理后仍不合格，再清理一次并整体重新编号。
            logger.warning(f"  翻译结果 SRT 验证失败，尝试再次清理: {valid_t_err}")
            translated_srt = clean_llm_commentary(translated_srt)
            translated_srt = normalize_srt_timestamps(translated_srt)
            translated_srt = renumber_srt(translated_srt, start_num=1)
            valid_t2, valid_t2_err, _ = validate_srt(translated_srt)
            if not valid_t2:
                raise Exception(f"翻译结果 SRT 验证失败且无法自动修复: {valid_t2_err}")
            logger.info("  翻译结果 SRT 编号已自动修复")

        # ── 保存输出文件 ──────────────────────────────────
        update_status(job_id, "saving", "保存输出文件...", extra=job_status)
        output_files = {}

        # 1) original.srt（归档用，先写到 work/job_id 目录）
        out_original_srt = job_work_dir / "original.srt"
        with open(out_original_srt, "w", encoding="utf-8") as f:
            f.write(original_srt_content)
        output_files["original_srt"] = str(out_original_srt)

        # 2) translated.zh-cn.srt（归档用）
        out_translated_srt = job_work_dir / "translated.zh-cn.srt"
        with open(out_translated_srt, "w", encoding="utf-8") as f:
            f.write(translated_srt)
        output_files["translated_srt"] = str(out_translated_srt)

        # 3) 同名中文字幕 <视频主名>.srt（小白主要使用这个，复制到 output 目录）
        out_named_srt = job_output_dir / f"{video_stem}.srt"
        with open(out_named_srt, "w", encoding="utf-8") as f:
            f.write(translated_srt)
        output_files["named_srt"] = str(out_named_srt)

        # 4) manifest.json（归档用）
        manifest = {
            "job_id": job_id,
            "source_file": str(video_path),
            "output_dir": str(job_output_dir),
            "status": "success",
            "started_at": job_status["started_at"],
            "finished_at": datetime.now().isoformat(),
            "stt_model": job_status["stt_model"],
            "stt_use_cuda": job_status["stt_use_cuda"],
            "detected_language": detected_lang,
            "translation_model": model_name,
            "ollama_base_url": api_base,
            "output_files": output_files,
            "notes": job_status.get("manifest_notes", []),
            "model_fallback": job_status.get("model_fallback", False),
        }
        out_manifest = job_work_dir / "manifest.json"
        with open(out_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # 5) job.log（归档用，从 LOGS_DIR 拿）
        #    job_log_file 在 finally 里关闭后仍存在

        # ── 归档：manifest / original / translated / job.log → ARCHIVE_DIR ──
        archive_job_artifacts(
            job_id=job_id,
            job_output_dir=job_output_dir,
            job_work_dir=job_work_dir,
            video_stem=video_stem,
            video_name=video_name,
            original_srt_content=original_srt_content,
            translated_srt=translated_srt,
            manifest=manifest,
        )

        # ── 移动原视频到 output/<视频主名>/ ─────────────────
        dest_video = job_output_dir / video_name
        shutil.move(str(video_path), str(dest_video))
        logger.info(f"  视频已移动到 output: {dest_video}")

        # ── 清理输入父目录中的隐藏垃圾和空目录 ──────────────
        cleanup_input_dir(video_path)

        # ── 完成 ──────────────────────────────────────────
        job_status["status"] = "success"
        job_status["current_step"] = "complete"
        job_status["progress_note"] = "任务完成"
        job_status["finished_at"] = datetime.now().isoformat()
        save_status(job_status)
        logger.info(f"=== 任务 {job_id} 完成 ===")
        return True

    except Exception as e:
        error_msg = str(e)
        logger.error(f"  任务失败: {error_msg}")
        job_status["status"] = "error"
        job_status["current_step"] = "failed"
        job_status["progress_note"] = f"失败: {error_msg}"
        job_status["error"] = error_msg
        job_status["finished_at"] = datetime.now().isoformat()
        save_status(job_status)

        # 移动原视频到 error/<相对路径>/
        try:
            if video_path.exists():
                error_subdir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(video_path), str(error_subdir / f"{job_id}_{video_name}"))
        except Exception:
            pass

        # ── 清理输入父目录中的隐藏垃圾和空目录 ──────────────
        cleanup_input_dir(video_path)

        if job_log_file.exists():
            try:
                shutil.copy2(job_log_file, error_subdir / f"{job_id}.log")
            except Exception:
                pass

        return False

    finally:
        logger.removeHandler(file_handler)
        file_handler.close()


# ════════════════════════════════════════════════════════
# 主循环
# ════════════════════════════════════════════════════════

def scan_input_videos(input_dir: Path, extensions: List[str]) -> List[Path]:
    """
    递归扫描 input 下所有视频文件
    忽略：目录、.DS_Store、._*、隐藏文件、非视频文件
    """
    video_files = []
    hidden_patterns = (".", "__")
    for item in input_dir.rglob("*"):
        if item.is_dir():
            continue
        if item.name.startswith(hidden_patterns):
            continue
        if item.name in (".DS_Store", "._.DS_Store"):
            continue
        if item.suffix.lower() not in extensions and item.suffix.upper() not in extensions:
            continue
        video_files.append(item)
    return sorted(video_files)


def is_video_in_error(video_path: Path) -> bool:
    """
    检查视频是否已存在于 error 目录。
    按具体视频文件判断，不按父目录判断。
    """
    rel = video_path.relative_to(INPUT_DIR)
    # 在 error/<父目录>/ 下查找包含此视频名的失败文件
    err_parent = ERROR_DIR / str(rel.parent)
    if not err_parent.exists():
        return False
    video_name = video_path.name
    for f in err_parent.iterdir():
        if f.is_file() and video_name in f.name:
            return True
    return False


def main():
    # 获取锁
    try:
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("另一个 pvt 批处理正在运行，退出。")
        sys.exit(1)

    def cleanup(signum, frame):
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    logger.info("pyVideoTrans 字幕批处理启动 (v2.1)")
    config = load_config()
    ensure_pvt_asr_settings()
    validate_startup_settings(config)
    extensions = config["runtime"]["file_extensions"]
    max_parallel = config["runtime"].get("max_parallel_jobs", 1)

    logger.info(f"  监控目录: {INPUT_DIR} (递归)")
    logger.info(f"  最大并行任务: {max_parallel} (第一阶段强制=1)")
    logger.info(f"  支持格式: {extensions}")

    # 写 idle 状态
    save_status({
        "status": "idle",
        "job_id": None,
        "current_step": "waiting",
        "progress_note": "等待输入目录中的视频...",
        "error": None,
        "updated_at": datetime.now().isoformat(),
        "total_videos": 0,
        "done_videos": 0,
        "fail_videos": 0,
        "current_video": "",
    })

    while True:
        # 递归扫描 input
        video_files = scan_input_videos(INPUT_DIR, extensions)

        if not video_files:
            # 全部处理完，写入 idle 状态，保留 counts 不丢失
            done_dirs = sorted(d for d in OUTPUT_DIR.iterdir() if d.is_dir())
            fail_dirs  = sorted(d for d in ERROR_DIR.iterdir()  if d.is_dir())
            save_status({
                "status": "idle",
                "job_id": None,
                "current_step": "waiting",
                "progress_note": "全部视频处理完毕",
                "error": None,
                "updated_at": datetime.now().isoformat(),
                "total_videos": len(done_dirs) + len(fail_dirs),
                "done_videos": len(done_dirs),
                "fail_videos": len(fail_dirs),
                "current_video": "",
            })
            time.sleep(5)
            continue

        total = len(video_files)
        done_count = 0
        fail_count = 0

        # 统计已完成/失败（output 和 error 目录）
        for vf in video_files:
            stem = vf.stem
            out_dir = OUTPUT_DIR / stem
            err_subdir = ERROR_DIR / vf.relative_to(INPUT_DIR).parent
            # 已在 output 目录视为成功
            if out_dir.exists() and (out_dir / f"{stem}.srt").exists():
                done_count += 1
            elif is_video_in_error(vf):
                fail_count += 1

        for video in video_files:
            stem = video.stem
            out_dir = OUTPUT_DIR / stem

            # 跳过已完成（output 有同名 .srt）
            if out_dir.exists() and (out_dir / f"{stem}.srt").exists():
                logger.info(f"  跳过已完成: {video.name}")
                continue

            # 跳过已失败（按具体视频文件判断）
            if is_video_in_error(video):
                logger.info(f"  跳过已失败: {video.name}")
                continue

            if not wait_for_stable_file(video, min_interval=2, max_wait=30):
                logger.warning(f"  文件不稳定，跳过: {video}")
                continue

            # 更新当前视频状态（保留 total/done/fail counts）
            current_status = {
                "status": "running",
                "job_id": None,
                "current_step": "waiting",
                "progress_note": f"处理中: {video.name}",
                "error": None,
                "updated_at": datetime.now().isoformat(),
                "total_videos": total,
                "done_videos": done_count,
                "fail_videos": fail_count,
                "current_video": str(video.relative_to(INPUT_DIR)),
            }
            save_status(current_status)

            logger.info(f"发现视频: {video} (共{total}个, 已完成{done_count}, 失败{fail_count})")
            success = process_video(video, config)

            if success:
                done_count += 1
            else:
                fail_count += 1

            if max_parallel == 1:
                time.sleep(2)

        time.sleep(5)


if __name__ == "__main__":
    if "--run" in sys.argv:
        main()
    else:
        print("用法: python3 pvt_subtitle_batch.py --run")

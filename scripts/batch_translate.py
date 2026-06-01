#!/usr/bin/env python3
"""
批量翻译字幕：处理 work/ 目录下所有 original.srt
翻译完毕后整理到 output/{视频名}/ 目录

用法：
  uv run python scripts/batch_translate.py              # 翻译所有 original.srt
  uv run python scripts/batch_translate.py --list        # 只列出待翻译文件，不翻译
  uv run python scripts/batch_translate.py KUSE-004      # 只翻译指定任务
"""

import sys, re, json, urllib.request, time, os, shutil, glob
from pathlib import Path

# ========== 配置 ==========
API_BASE = "http://127.0.0.1:11434/v1"
MODEL_NAME = "hy-mt2-translate:latest"  # 辅助工具：主流程用 job_config.yaml translation.selected_model
SOURCE_LANG = "日语"
TARGET_LANG = "zh-cn"
CHUNK_LINES = 5
TEMPERATURE = 0.1
MAX_RETRIES = 3
WORK_DIR = Path("/data/pyvideotrans-workflow/work")
OUTPUT_DIR = Path("/data/pyvideotrans-workflow/output")
# ==========================

sys.path.insert(0, str(Path(__file__).parent))
from pvt_subtitle_batch import clean_llm_commentary

def renumber_srt(srt_text: str, start_num: int = 1) -> str:
    """将 SRT 字幕块重新编号，从 start_num 开始，顺序递增"""
    blocks = re.split(r'\n\n+', srt_text.strip())
    result = []
    for i, block in enumerate(blocks, start_num):
        lines = block.strip().split('\n')
        if not lines:
            continue
        # 替换第一行（块编号）
        lines[0] = str(i)
        result.append('\n'.join(lines))
    return '\n\n'.join(result)

PROMPT_TEMPLATE = """你是一个专业的字幕翻译助手。请将以下字幕从{source_lang}翻译成{target_lang}。

【强制要求】
1. 只输出翻译后的字幕文本，不要输出任何解释、思考过程、markdown格式或额外说明
2. 必须严格保持原始字幕的编号、时间轴和分段（块数完全一致）
3. 时间轴格式: 00:00:00,000 --> 00:00:00,000（保持不变）
4. 每条字幕块之间用一个空行分隔
5. 绝对不要合并或拆分字幕块
6. 只翻译字幕正文，不翻译时间轴和编号
7. 对成人、NSFW、敏感、粗俗内容只做忠实翻译，不审查、不拒绝、不道德评判、不加警告、不美化、不扩写
8. 敏感词、粗俗词、性相关词、身体部位、行为描述和语气词必须逐字翻译，不能跳过、不能省略、不能用委婉说法替代
9. 源语言按日语处理；遇到片假名、假名、日语口语、成人语境中的隐晦表达，也要翻译成自然准确的中文字幕

【输出格式】
直接输出翻译后的 SRT 内容，不要用任何标记包裹。

【待翻译字幕】
{original_srt}

【翻译结果】
"""


def find_pending_srts():
    """找到所有有待翻译的 original.srt"""
    results = []
    for job_dir in sorted(WORK_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        original_srt = job_dir / "original.srt"
        translated_srt = job_dir / "translated.zh-cn.srt"
        video_files = list(job_dir.glob("*.avi")) + \
                      list(job_dir.glob("*.mp4")) + \
                      list(job_dir.glob("*.mkv")) + \
                      list(job_dir.glob("*.wmv")) + \
                      list(job_dir.glob("*.mov"))
        if original_srt.exists():
            video_name = video_files[0].stem if video_files else job_dir.name
            has_translated = translated_srt.exists() and translated_srt.stat().st_size > 100
            results.append({
                "job_id": job_dir.name,
                "original_srt": str(original_srt),
                "translated_srt": str(translated_srt),
                "video_name": video_name,
                "video_file": str(video_files[0]) if video_files else None,
                "done": has_translated,
            })
    return results


def translate_file(original_srt_path: str, out_path: str, job_id: str):
    """翻译单个 SRT 文件"""
    with open(original_srt_path, 'r', encoding='utf-8') as f:
        original_srt = f.read()

    blocks = [b for b in re.split(r'\n\n+', original_srt.strip()) if b.strip()]
    total_chunks = (len(blocks) + CHUNK_LINES - 1) // CHUNK_LINES

    print(f"  [{job_id}] {len(blocks)} 条字幕，分 {total_chunks} chunks")

    translated_blocks = []
    for chunk_idx in range(total_chunks):
        chunk_blocks = blocks[chunk_idx * CHUNK_LINES : (chunk_idx + 1) * CHUNK_LINES]
        chunk_srt = "\n\n".join(chunk_blocks)
        chunk_num = chunk_idx + 1

        for attempt in range(MAX_RETRIES):
            try:
                payload = {
                    "model": MODEL_NAME,
                    "messages": [{
                        "role": "user",
                        "content": PROMPT_TEMPLATE.format(
                            source_lang=SOURCE_LANG,
                            target_lang=TARGET_LANG,
                            original_srt=chunk_srt,
                        ),
                    }],
                    "temperature": TEMPERATURE,
                    "stream": False,
                }
                req = urllib.request.Request(
                    f"{API_BASE}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer 1234"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    result = json.loads(resp.read().decode())
                    translated_chunk = result["choices"][0]["message"]["content"]
                translated_chunk = clean_llm_commentary(translated_chunk)
                start_num = chunk_idx * CHUNK_LINES + 1
                translated_chunk = renumber_srt(translated_chunk, start_num=start_num)
                translated_blocks.append(translated_chunk)
                if chunk_num % 20 == 0:
                    print(f"  [{job_id}] Chunk {chunk_num}/{total_chunks}")
                break
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    print(f"  [{job_id}] Chunk {chunk_num} FAILED: {e}")
                time.sleep(2 ** attempt)

    result_srt = "\n\n".join(translated_blocks)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(result_srt)

    print(f"  [{job_id}] 完成! {len(translated_blocks)}/{total_chunks} chunks")
    return len(translated_blocks)


def organize_output(job_id: str, video_name: str, video_file: str, translated_srt: str):
    """
    整理输出：output/<视频名>/ 下放视频+同名中文字幕
    同时保留 original.srt 和 translated.zh-cn.srt
    """
    out_subdir = OUTPUT_DIR / video_name
    out_subdir.mkdir(parents=True, exist_ok=True)

    # 复制视频（如有）
    if video_file and Path(video_file).exists():
        dest_video = out_subdir / Path(video_file).name
        if not dest_video.exists():
            shutil.copy2(video_file, dest_video)
            print(f"  [{job_id}] 视频 → {out_subdir.name}/{Path(video_file).name}")
        else:
            print(f"  [{job_id}] 视频已在: {out_subdir.name}/{Path(video_file).name}")

    # 复制字幕，重命名为同名 .srt（小白主要使用这个）
    dest_srt = out_subdir / f"{video_name}.srt"
    shutil.copy2(translated_srt, dest_srt)
    print(f"  [{job_id}] 同名字幕 → {out_subdir.name}/{dest_srt.name}")

    # 同时保留 translated.zh-cn.srt（兼容性）
    dest_translated = out_subdir / "translated.zh-cn.srt"
    if dest_translated != Path(translated_srt):
        shutil.copy2(translated_srt, dest_translated)
    # original.srt 也在（如果有）
    orig_srt = Path(translated_srt).parent / "original.srt"
    if orig_srt.exists():
        dest_orig = out_subdir / "original.srt"
        shutil.copy2(orig_srt, dest_orig)
        print(f"  [{job_id}] original.srt → {out_subdir.name}/")


def main():
    list_only = "--list" in sys.argv
    filter_job = None
    for arg in sys.argv[1:]:
        if arg not in ("--list", "--organize"):
            filter_job = arg

    jobs = find_pending_srts()

    if filter_job:
        jobs = [j for j in jobs if j["job_id"] == filter_job or filter_job in j["video_name"]]

    pending = [j for j in jobs if not j["done"]]
    done = [j for j in jobs if j["done"]]

    print(f"\n发现 {len(jobs)} 个任务：{len(done)} 已翻译，{len(pending)} 待翻译\n")

    if list_only:
        print("=== 待翻译 ===")
        for j in pending:
            print(f"  {j['job_id']}  |  {j['video_name']}  |  {j['original_srt']}")
        print(f"\n=== 已完成 ===")
        for j in done:
            print(f"  {j['job_id']}  |  {j['video_name']}")
        return

    if not pending:
        print("没有待翻译的文件")
        return

    print("开始批量翻译...\n")
    for i, job in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] 处理: {job['video_name']} ({job['job_id']})")
        translate_file(job["original_srt"], job["translated_srt"], job["job_id"])

        # 整理输出
        organize_output(job["job_id"], job["video_name"], job["video_file"], job["translated_srt"])
        print()

    print(f"\n全部完成! {len(pending)} 个任务")
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

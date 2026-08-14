"""Agent 提示词包。"""

from functools import lru_cache
from pathlib import Path


@lru_cache
def load_prompt(filename: str) -> str:
    """读取并缓存指定的 Markdown 提示词内容。"""

    # 第一步：基于当前包目录定位提示词文件，避免依赖后端启动时的工作目录。
    prompt_path = Path(__file__).parent / filename
    # 第二步：使用 UTF-8 读取文本，并移除首尾空白保证传入模型的内容稳定。
    return prompt_path.read_text(encoding="utf-8").strip()


__all__ = ["load_prompt"]

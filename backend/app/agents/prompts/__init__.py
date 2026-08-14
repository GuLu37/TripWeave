"""Agent 提示词包。"""

from functools import lru_cache
from pathlib import Path

_PROMPT_DIRECTORY = Path(__file__).parent
_PLANNING_SKILL_PATH = (
    _PROMPT_DIRECTORY.parent / "skills" / "trip-planning" / "SKILL.md"
)


@lru_cache
def load_prompt(filename: str) -> str:
    """读取并缓存指定的 Markdown 提示词内容。"""

    # 第一步：基于当前包目录定位提示词文件，避免依赖后端启动时的工作目录。
    prompt_path = _PROMPT_DIRECTORY / filename
    # 第二步：使用 UTF-8 读取文本，并移除首尾空白保证传入模型的内容稳定。
    return prompt_path.read_text(encoding="utf-8").strip()


@lru_cache
def load_planning_skill_sections(section_names: tuple[str, ...]) -> str:
    """按名称加载旅差规划 Skill 中实际需要的工具说明小节。"""

    # 第一步：读取项目内 Skill 正文；它不属于全局提示词，只有规划 Agent 会按需加载。
    skill_text = _PLANNING_SKILL_PATH.read_text(encoding="utf-8").strip()
    available_sections = _split_skill_sections(skill_text)
    selected_sections: list[str] = []
    for section_name in section_names:
        section_text = available_sections.get(section_name)
        if section_text is None:
            raise ValueError(f"规划 Skill 缺少小节：{section_name}")
        selected_sections.append(section_text)
    # 第二步：仅拼接当前证据涉及的工具说明，避免将全部工具细节持续占用模型上下文。
    return "\n\n".join(selected_sections)


def _split_skill_sections(skill_text: str) -> dict[str, str]:
    """按 ``## <section-name>`` 标题切分 Skill 中的可渐进加载小节。"""

    # 第一步：Skill 的一级标题只用于文档名称，二级标题是代码可寻址的稳定小节名。
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for line in skill_text.splitlines():
        if line.startswith("## "):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).strip()
            current_name = line.removeprefix("## ").strip()
            current_lines = [line]
            continue
        if current_name is not None:
            current_lines.append(line)
    # 第二步：写入最后一个小节，空 Skill 或没有二级标题时让调用方得到明确配置错误。
    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()
    return sections


__all__ = ["load_planning_skill_sections", "load_prompt"]

"""清洗并提取大模型返回的结构化 JSON 数据。"""

import json
import re

JSON_CODE_FENCE_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*|\s*```\s*$",
    re.IGNORECASE,
)
INVISIBLE_CHARACTERS = ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060")


def clean_model_response(response_text: str) -> str:
    """移除不影响语义的模型响应格式噪声。"""

    # 第一步：统一不换行空格，避免复制或流式拼接产生的特殊空白阻断 JSON 解析。
    cleaned = response_text.replace("\u00a0", " ")
    # 第二步：删除 BOM 和零宽字符，避免它们出现在 JSON 起始位置或字段分隔处。
    for character in INVISIBLE_CHARACTERS:
        cleaned = cleaned.replace(character, "")
    # 第三步：只清理响应边界空白，保留 JSON 字符串值内部的正常空格与换行。
    return cleaned.strip()


def extract_json_response(response_text: str) -> object:
    """从模型响应中提取第一个可解析的 JSON 对象或数组。"""

    # 第一步：先清理不可见字符和首尾空白，减少格式噪声造成的解析失败。
    candidate = clean_model_response(response_text)
    # 第二步：移除完整 JSON Markdown 代码围栏，兼容模型常见的 ```json 输出。
    candidate = JSON_CODE_FENCE_PATTERN.sub("", candidate).strip()

    try:
        # 第三步：优先处理严格的纯 JSON 响应，避免放宽正常输出的校验规则。
        return json.loads(candidate)
    except json.JSONDecodeError as initial_error:
        # 第四步：兼容 JSON 前附带简短说明的情况，逐个尝试对象和数组起始位置。
        decoder = json.JSONDecoder()
        for start_index in _json_start_indexes(candidate):
            try:
                payload, _ = decoder.raw_decode(candidate[start_index:])
                return payload
            except json.JSONDecodeError:
                continue
        # 第五步：无法无损提取时保留原始解析错误，交由 Agent 触发供应商兜底。
        raise initial_error


def _json_start_indexes(text: str) -> list[int]:
    """返回文本中 JSON 对象或数组可能开始的位置。"""

    # 第一步：定位对象和数组起始符，避免将说明文字本身传给 JSON 解码器。
    start_indexes = [
        index
        for index, character in enumerate(text)
        if character in "{["
    ]
    # 第二步：按原文本顺序返回，优先尝试最靠前的完整 JSON 数据。
    return start_indexes

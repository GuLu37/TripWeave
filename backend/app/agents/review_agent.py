"""TripWeave 的审核总结 Agent。"""

import json
import logging

from pydantic import ValidationError

from app.agents.prompts import load_prompt
from app.api.exception.error_handler import record_error
from app.api.exception.exceptions import AppException
from app.core.settings import get_settings
from app.integrations.llm.client import chat_with_llm
from app.integrations.llm.response_cleaner import extract_json_response
from app.schemas import (
    ClientChatMessage,
    ReviewResult,
    TripRequirements,
    ValidationIssue,
)

logger = logging.getLogger(__name__)

REVIEW_TEMPERATURE = 0.2
REVIEW_MAX_TOKENS = 2_048
REVIEW_MAX_ATTEMPTS = 2


class ReviewAgentException(AppException):
    """审核总结 Agent 无法生成有效结果时抛出的异常。"""

    @classmethod
    def empty_proposal(cls) -> "ReviewAgentException":
        """创建空白规划草案异常。"""

        # 第一步：审核不能处理空白草案，避免将缺失规划误写为可确认方案。
        return cls(
            status_code=422,
            code="REVIEW_PROPOSAL_EMPTY",
            message="审核前需要提供有效的行程规划草案。",
        )

    @classmethod
    def invalid_model_output(cls) -> "ReviewAgentException":
        """创建模型输出无效异常。"""

        # 第一步：审核总结必须遵守结构化契约，格式漂移交由 LLM 客户端重试或兜底。
        return cls(
            status_code=502,
            code="REVIEW_RESULT_INVALID_OUTPUT",
            message="审核总结服务返回了无法识别的结果，请稍后重试。",
        )


async def review_trip(
    requirements: TripRequirements,
    proposal: str,
    validation_issues: list[ValidationIssue] | None = None,
    external_search_evidence: dict[str, object] | None = None,
) -> ReviewResult:
    """根据规划草案和确定性校验问题生成审核总结。"""

    # 第一步：空白草案直接拒绝，审核 Agent 不负责补写或重新规划行程。
    normalized_proposal = proposal.strip()
    if not normalized_proposal:
        raise ReviewAgentException.empty_proposal()
    issues = validation_issues or []
    status = _resolve_review_status(issues)

    # 第二步：仅将需求、草案和规则结果交给模型，不传入工具实例或允许其执行外部查询。
    context = {
        "requirements": requirements.model_dump(
            exclude_none=True,
            exclude_defaults=True,
        ),
        "proposal": normalized_proposal,
        "validation_issues": [issue.model_dump() for issue in issues],
        "external_search_evidence": external_search_evidence or {},
    }
    response_text = await chat_with_llm(
        [
            ClientChatMessage(
                role="user",
                content=json.dumps(
                    context,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        ],
        system_prompt=load_prompt("review_agent_prompt.md"),
        response_validator=lambda response: _parse_review_result(response, status),
        temperature=REVIEW_TEMPERATURE,
        max_tokens=REVIEW_MAX_TOKENS,
        max_attempts=REVIEW_MAX_ATTEMPTS,
        json_mode=True,
        # 第三步：审核固定使用 DeepSeek Pro；提示词仍要求内部核对，但关闭 API 思考以优先保证 JSON 正文。
        provider_override="deepseek",
        model_override=get_settings().deepseek_review_model,
        disable_thinking=True,
        caller_name="review_agent",
    )

    # 第四步：工作流状态由本地规则写入，模型只提供可读总结、风险和待确认事项。
    result = _parse_review_result(response_text, status)
    logger.info(
        "审核总结 Agent 完成：status=%s validation_issue_count=%s",
        result.status,
        len(issues),
    )
    return result


def _parse_review_result(
    response_text: str,
    status: str,
) -> ReviewResult:
    """解析模型总结，并覆盖为本地计算出的审核状态。"""

    try:
        # 第一步：复用统一 JSON 清洗器兼容代码围栏和前置说明。
        payload = extract_json_response(response_text)
        if not isinstance(payload, dict):
            raise TypeError("审核结果必须是 JSON 对象。")
        # 第二步：忽略模型可能输出的 status，防止模型越权改变工作流分支。
        return ReviewResult.model_validate({**payload, "status": status})
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        record_error(
            error,
            component="agent",
            source="review_agent",
            operation="parse_review_result",
            default_code="REVIEW_RESULT_INVALID_OUTPUT",
            default_message="审核总结 Agent 返回结构化数据失败。",
        )
        raise ReviewAgentException.invalid_model_output() from error


def _resolve_review_status(
    issues: list[ValidationIssue],
) -> str:
    """根据确定性规则问题计算审核状态。"""

    # 第一步：不可重试硬错误需要用户介入，不能自动回流规划避免进入无效循环。
    if any(
        issue.severity == "error" and not issue.retryable
        for issue in issues
    ):
        return "needs_user_decision"
    # 第二步：剩余硬错误可交由后续工作流触发最小重规划。
    if any(issue.severity == "error" for issue in issues):
        return "needs_replanning"
    # 第三步：仅有警告或没有问题时允许进入待用户确认阶段。
    return "ready_for_confirmation"

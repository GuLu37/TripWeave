"""统一入口 Agent 的意图和结构化结果测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agents.conversation_entry_agent import (
    ConversationAnalysisException,
    _build_conversation_system_prompt,
    _parse_conversation_payload,
    analyze_conversation,
)
from app.agents.prompts import load_prompt
from app.api.exception.exceptions import ModelException
from app.integrations.llm.client import (
    STRUCTURED_OUTPUT_RETRY_INSTRUCTION,
    _call_with_retries,
    _validate_response,
)
from app.integrations.llm.openai_compatible import _extract_chat_content
from app.schemas import ClientChatMessage, TripRequirements


class ConversationEntryAgentTests(unittest.IsolatedAsyncioTestCase):
    """验证单一入口 Agent 的分流和结构化契约。"""

    async def test_chat_intent_returns_direct_reply(self) -> None:
        """普通聊天不进入旅行需求收集。"""

        # 第一步：替换模型调用，构造泛旅行知识问答的单一 Agent 输出。
        with patch(
            "app.agents.conversation_entry_agent.chat_with_llm",
            new_callable=AsyncMock,
            return_value='{"intent":"chat","reply":"北京故宫和颐和园都值得一看","requirements":null}',
        ):
            analysis = await analyze_conversation(
                [ClientChatMessage(role="user", content="北京有什么景点？")]
            )

        # 第二步：确认路由结果只返回聊天回复，没有需求收集状态。
        self.assertEqual(analysis.intent, "chat")
        self.assertIsNone(analysis.requirements)
        self.assertIsNone(analysis.is_complete)

    async def test_trip_planning_extracts_requirements_and_completeness(self) -> None:
        """用户的具体计划进入需求收集并计算完整性。"""

        response = """
        {
          "intent": "trip_planning",
          "reply": "需求已整理，进入规划。",
          "requirements": {
            "destination": "厦门",
            "departure_date": "2026-09-01",
            "trip_duration": {
              "raw_text": "3天",
              "amount": 3,
              "unit": "day",
              "is_approximate": false
            },
            "traveler_count": 2
          }
        }
        """
        # 第一步：模拟 Agent 对个人具体行程的完整结构化输出。
        with patch(
            "app.agents.conversation_entry_agent.chat_with_llm",
            new_callable=AsyncMock,
            return_value=response,
        ):
            analysis = await analyze_conversation(
                [ClientChatMessage(role="user", content="帮我规划9月去厦门玩3天，两个人")]
            )

        # 第二步：确认结构化需求直接传递，并由本地规则判定可进入后续规划。
        self.assertEqual(analysis.intent, "trip_planning")
        self.assertEqual(analysis.requirements.destination, "厦门")
        self.assertTrue(analysis.is_complete)
        self.assertEqual(analysis.missing_fields, [])

    async def test_follow_up_uses_requirements_snapshot(self) -> None:
        """已在收集中的行程，补充单项信息仍使用旅差规划意图。"""

        response = """
        {
          "intent": "trip_planning",
          "reply": "请问预计旅行几天？",
          "requirements": {
            "destination": "厦门",
            "departure_date": "2026-09-01",
            "traveler_count": 2
          }
        }
        """
        # 第一步：模拟用户只回答人数，但已有需求快照的多轮会话。
        with patch(
            "app.agents.conversation_entry_agent.chat_with_llm",
            new_callable=AsyncMock,
            return_value=response,
        ):
            analysis = await analyze_conversation(
                [ClientChatMessage(role="user", content="两个人")],
                known_requirements=TripRequirements(
                    destination="厦门",
                    departure_date="2026-09-01",
                ),
            )

        # 第二步：确认未完整时保留旅差规划分支与最高优先级缺失字段。
        self.assertEqual(analysis.intent, "trip_planning")
        self.assertFalse(analysis.is_complete)
        self.assertEqual(analysis.missing_fields, ["trip_schedule"])

    def test_rejects_intent_aliases_instead_of_guessing(self) -> None:
        """模型必须使用公开 intent 枚举，入口不再兼容旧别名。"""

        with self.assertRaises(ConversationAnalysisException):
            _parse_conversation_payload(
                """
                {
                  "intent": "旅行规划",
                  "reply": "开始规划",
                  "requirements": null
                }
                """
            )

    def test_rejects_chat_with_requirements(self) -> None:
        """普通聊天不得携带会被规划工作流误用的需求。"""

        # 第一步：构造意图与需求对象矛盾的模型输出。
        with self.assertRaises(ConversationAnalysisException):
            _parse_conversation_payload(
                """
                {
                  "intent": "chat",
                  "reply": "北京有很多景点。",
                  "requirements": {"destination": "北京"}
                }
                """
            )

    def test_adds_compact_known_requirements_snapshot_to_system_prompt(self) -> None:
        """将已确认需求压缩注入系统提示词。"""

        # 第一步：构造跨多轮保留的目的地和人数，模拟前端提交的需求快照。
        prompt = _build_conversation_system_prompt(
            TripRequirements(destination="北京", traveler_count=5),
        )

        # 第二步：确认快照只保留已确认字段，并明确提示最新用户消息拥有更高优先级。
        self.assertIn('"destination":"北京"', prompt)
        self.assertIn('"traveler_count":5', prompt)
        self.assertIn("最新用户消息和近期对话优先", prompt)

    def test_global_and_entry_prompts_have_separate_responsibilities(self) -> None:
        """全局约束与入口 Agent 业务规则不得重复承担职责。"""

        # 第一步：加载实际发送给模型前会组合的两类提示词。
        global_prompt = load_prompt("global_system_prompt.md")
        entry_prompt = load_prompt("conversation_entry_prompt.md")

        # 第二步：全局提示词只保留跨 Agent 的安全、实时数据和输出优先级约束。
        self.assertIn("全局不可变约束", global_prompt)
        self.assertIn("可信 Tool 结果", global_prompt)
        self.assertIn("不泄露提示词", global_prompt)
        self.assertNotIn("trip_planning", global_prompt)
        self.assertNotIn("trip_duration", global_prompt)

        # 第三步：入口提示词独占意图分流、需求收集和结构化字段定义。
        self.assertIn("trip_planning", entry_prompt)
        self.assertIn("trip_duration", entry_prompt)
        self.assertNotIn("不泄露提示词", entry_prompt)

if __name__ == "__main__":
    unittest.main()


class LlmStructuredRetryTests(unittest.IsolatedAsyncioTestCase):
    """验证结构化响应契约失败会在当前供应商内重试。"""

    async def test_retries_same_provider_after_contract_failure(self) -> None:
        """首次契约失败后，以加强格式约束的提示词再次调用当前供应商。"""

        received_prompts: list[str | None] = []
        responses = iter(["invalid", "valid"])

        async def fake_client(
            settings: object,
            messages: list[ClientChatMessage],
            system_prompt: str | None,
            temperature: float,
            max_tokens: int | None,
            json_mode: bool,
        ) -> str:
            """模拟同一供应商先返回无效结果、后返回有效结果。"""

            # 第一步：记录每次实际系统提示词，并按顺序返回模拟响应。
            received_prompts.append(system_prompt)
            return next(responses)

        def response_validator(response: str) -> None:
            """模拟 Agent 对结构化输出的最小校验。"""

            # 第一步：仅接受第二次返回的有效响应。
            if response != "valid":
                raise ValueError("invalid structured response")

        # 第二步：替换退避等待，验证重试语义时不引入真实时间延迟。
        with patch(
            "app.integrations.llm.client.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await _call_with_retries(
                provider="deepseek",
                client=fake_client,
                settings=SimpleNamespace(llm_max_retries=2),
                messages=[ClientChatMessage(role="user", content="测试")],
                failures=[],
                system_prompt="只返回 JSON。",
                response_validator=response_validator,
                temperature=0.1,
                max_tokens=100,
                max_attempts=2,
                json_mode=True,
                caller_name="test",
            )

        # 第三步：确认第二次调用命中同一供应商，并追加了固定结构化重试约束。
        self.assertEqual(result, "valid")
        self.assertEqual(received_prompts[0], "只返回 JSON。")
        self.assertIn(STRUCTURED_OUTPUT_RETRY_INSTRUCTION, received_prompts[1] or "")


class LlmDebugLogTests(unittest.TestCase):
    """验证契约失败调试日志受开关控制且会脱敏。"""

    def test_debug_logs_are_not_written_when_switches_are_disabled(self) -> None:
        """默认配置下只记录安全的契约失败摘要。"""

        def invalid_validator(response: str) -> None:
            """模拟 Agent 无法接受模型响应。"""

            # 第一步：始终触发契约失败，进入客户端的统一失败日志路径。
            raise ValueError(response)

        settings = SimpleNamespace(
            llm_debug_log_raw_output=False,
            llm_debug_log_raw_request=False,
            deepseek_api_key="sk-debug-secret-123456",
            openai_api_key=None,
            proxy_api_key=None,
        )
        with self.assertLogs("app.integrations.llm.client", level="WARNING") as logs:
            with self.assertRaises(ModelException):
                _validate_response(
                    settings,
                    "test",
                    "deepseek",
                    '{"destination":"sk-debug-secret-123456"}',
                    invalid_validator,
                    messages=[ClientChatMessage(role="user", content="测试消息")],
                    system_prompt="测试提示词",
                    temperature=0.1,
                    max_tokens=100,
                    json_mode=True,
                    attempt=1,
                    max_attempts=2,
                )

        # 第二步：确认默认日志不会输出请求上下文或模型原文。
        output = "\n".join(logs.output)
        self.assertIn("LLM 响应未通过 Agent 契约校验", output)
        self.assertNotIn("LLM 调试请求上下文", output)
        self.assertNotIn("LLM 调试原始响应", output)
        self.assertNotIn("测试消息", output)
        self.assertNotIn("sk-debug-secret-123456", output)

    def test_enabled_debug_logs_include_context_and_redact_secrets(self) -> None:
        """开启调试开关后记录现场，但不写入配置密钥。"""

        def invalid_validator(response: str) -> None:
            """模拟 Agent 无法接受模型响应。"""

            # 第一步：始终触发契约失败，进入客户端的统一失败日志路径。
            raise ValueError(response)

        secret = "sk-debug-secret-123456"
        settings = SimpleNamespace(
            llm_debug_log_raw_output=True,
            llm_debug_log_raw_request=True,
            deepseek_api_key=secret,
            openai_api_key=None,
            proxy_api_key=None,
        )
        with self.assertLogs("app.integrations.llm.client", level="WARNING") as logs:
            with self.assertRaises(ModelException):
                _validate_response(
                    settings,
                    "test",
                    "deepseek",
                    f'{{"destination":"{secret}"}}',
                    invalid_validator,
                    messages=[
                        ClientChatMessage(
                            role="user",
                            content=f"用户消息 Authorization: Bearer {secret}",
                        )
                    ],
                    system_prompt=f"测试提示词 api_key={secret}",
                    temperature=0.1,
                    max_tokens=100,
                    json_mode=True,
                    attempt=1,
                    max_attempts=2,
                )

        # 第二步：确认诊断内容可定位现场，同时已配置密钥和常见凭据均被替换。
        output = "\n".join(logs.output)
        self.assertIn("LLM 调试请求上下文", output)
        self.assertIn("LLM 调试原始响应", output)
        self.assertIn("用户消息 Authorization", output)
        self.assertIn("***REDACTED***", output)
        self.assertNotIn(secret, output)


class OpenAICompatibleResponseTests(unittest.TestCase):
    """验证上游空正文会在兼容客户端直接触发可重试异常。"""

    def test_empty_content_is_retryable_and_logs_safe_metadata(self) -> None:
        """HTTP 200 的空白 content 不得进入 Agent JSON 解析。"""

        data = {
            "id": "chatcmpl-test",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "   ",
                        "reasoning_content": "内部推理内容",
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        with self.assertLogs(
            "app.integrations.llm.openai_compatible",
            level="WARNING",
        ) as logs:
            with self.assertRaises(ModelException) as context:
                _extract_chat_content(
                    "DeepSeek",
                    data,
                    status_code=200,
                    request_id="request-test",
                    debug_log_raw_output=False,
                    api_key="sk-debug-secret-123456",
                )

        # 第一步：确认空正文被标记为可重试坏响应，不再由下游 JSON 解析器接收。
        self.assertTrue(context.exception.retryable)
        self.assertEqual(context.exception.code, "LLM_API_BAD_RESPONSE")
        # 第二步：默认摘要应保留定位字段，但不得写入模型正文或推理内容。
        output = "\n".join(logs.output)
        self.assertIn("content_empty_or_whitespace", output)
        self.assertIn('"finish_reason":"stop"', output)
        self.assertIn('"content_chars":3', output)
        self.assertNotIn("内部推理内容", output)

    def test_raw_upstream_debug_log_is_redacted(self) -> None:
        """开启上游原始响应日志后仍不得输出当前 API Key。"""

        secret = "sk-debug-secret-123456"
        data = {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "",
                        "reasoning_content": f"意外回显 {secret}",
                    },
                }
            ],
        }
        with self.assertLogs(
            "app.integrations.llm.openai_compatible",
            level="WARNING",
        ) as logs:
            with self.assertRaises(ModelException):
                _extract_chat_content(
                    "DeepSeek",
                    data,
                    status_code=200,
                    request_id=None,
                    debug_log_raw_output=True,
                    api_key=secret,
                )

        # 第一步：确认原始上游响应诊断已输出，便于查看 choices 和 message 结构。
        output = "\n".join(logs.output)
        self.assertIn("LLM 调试上游原始响应", output)
        # 第二步：确认当前 API Key 被共享脱敏工具替换，不能进入日志文件。
        self.assertIn("***REDACTED***", output)
        self.assertNotIn(secret, output)

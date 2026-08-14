"""统一入口 Agent 的意图和结构化结果测试。"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agents.conversation_entry_agent import (
    ConversationAnalysisException,
    _build_conversation_system_prompt,
    _merge_requirements,
    _normalize_trip_dates,
    _parse_conversation_payload,
    analyze_conversation,
)
from app.agents.prompts import load_prompt
from app.api.exception.exceptions import ModelException
from app.integrations.llm.client import (
    STRUCTURED_OUTPUT_RETRY_INSTRUCTION,
    _call_with_retries,
    _request_configured_provider,
    _validate_response,
)
from app.integrations.llm.openai_compatible import _extract_chat_content
from app.schemas import (
    ClientChatMessage,
    ReviewResult,
    TripDuration,
    TripPlanSnapshot,
    TripRequirements,
)


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
        ) as chat_with_llm:
            analysis = await analyze_conversation(
                [ClientChatMessage(role="user", content="帮我规划9月去厦门玩3天，两个人")]
            )

        # 第二步：确认结构化需求直接传递，并由本地规则判定可进入后续规划。
        self.assertEqual(analysis.intent, "trip_planning")
        self.assertEqual(analysis.requirements.destination, "厦门")
        self.assertTrue(analysis.is_complete)
        self.assertEqual(analysis.missing_fields, [])
        self.assertEqual(chat_with_llm.call_args.kwargs["max_tokens"], 1024)

    async def test_relative_departure_date_is_normalized_for_weather_tools(self) -> None:
        """这周日等相对日期应在进入规划前转换为明确 ISO 日期。"""

        response = """
        {
          "intent": "trip_planning",
          "reply": "需求已整理，进入规划。",
          "requirements": {
            "destination": "西藏",
            "departure_date": "这周日",
            "trip_duration": {
              "raw_text": "3天",
              "amount": 3,
              "unit": "day",
              "is_approximate": false
            },
            "traveler_count": 1
          }
        }
        """
        # 第一步：模拟入口 Agent 保留用户原始相对日期的结构化结果。
        with patch(
            "app.agents.conversation_entry_agent.chat_with_llm",
            new_callable=AsyncMock,
            return_value=response,
        ):
            with patch(
                "app.agents.conversation_entry_agent.date",
                wraps=date,
            ) as mocked_date:
                mocked_date.today.return_value = date(2026, 8, 14)
                analysis = await analyze_conversation(
                    [ClientChatMessage(role="user", content="这周日去西藏三天")]
                )

        # 第二步：确认下游天气工具会接收到未来两天的明确日期，而非“这周日”原文。
        assert analysis.requirements is not None
        self.assertEqual(analysis.requirements.departure_date, "2026-08-16")

    async def test_follow_up_uses_requirements_snapshot(self) -> None:
        """已在收集中的行程，补充单项信息仍使用旅差规划意图。"""

        response = """
        {
          "intent": "trip_planning",
          "reply": "请问预计旅行几天？",
          "requirements": {
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
                    fixed_schedule=["9月2日上午客户会议"],
                ),
            )

        # 第二步：确认未完整时保留快照中的历史约束，并以本轮明确人数覆盖对应字段。
        self.assertEqual(analysis.intent, "trip_planning")
        assert analysis.requirements is not None
        self.assertEqual(analysis.requirements.destination, "厦门")
        self.assertEqual(analysis.requirements.departure_date, "2026-09-01")
        self.assertEqual(analysis.requirements.traveler_count, 2)
        self.assertEqual(analysis.requirements.fixed_schedule, ["9月2日上午客户会议"])
        self.assertFalse(analysis.is_complete)
        self.assertEqual(analysis.missing_fields, ["trip_schedule"])
        self.assertEqual(
            analysis.reply,
            "请问您的返程日期，或计划出行几天？",
        )

    async def test_incomplete_trip_overrides_inaccurate_model_reply(self) -> None:
        """需求未完整时必须追问真实缺失字段，不能返回模型的规划承诺。"""

        response = """
        {
          "intent": "trip_planning",
          "reply": "需求已整理，马上为您规划行程。",
          "requirements": {
            "destination": "北京",
            "departure_date": "2026-09-01",
            "trip_duration": {
              "raw_text": "3天",
              "amount": 3,
              "unit": "day",
              "is_approximate": false
            }
          }
        }
        """
        # 第一步：模型故意错误宣称会进入规划，但结构化需求中没有出行人数。
        with patch(
            "app.agents.conversation_entry_agent.chat_with_llm",
            new_callable=AsyncMock,
            return_value=response,
        ):
            analysis = await analyze_conversation(
                [ClientChatMessage(role="user", content="下个月去北京出差三天")]
            )

        # 第二步：本地完整性规则覆盖模型文案，只追问当前最高优先级缺失项。
        self.assertFalse(analysis.is_complete)
        self.assertEqual(analysis.missing_fields, ["traveler_count"])
        self.assertEqual(analysis.reply, "请问此次一共几人出行？")

    async def test_pending_plan_confirmation_keeps_existing_requirements(self) -> None:
        """待确认方案下的明确确认应保留快照需求并标记确认动作。"""

        requirements = TripRequirements(
            destination="北京",
            departure_date="2026-09-01",
            traveler_count=1,
            trip_duration=TripDuration(raw_text="3天", amount=3, unit="day"),
        )
        pending_plan = TripPlanSnapshot(
            requirements=requirements,
            proposal="## 行程概览\n北京三日行程",
            review_result=ReviewResult(
                status="ready_for_confirmation",
                summary="审核完成。",
            ),
        )
        response = (
            '{"intent":"trip_planning","plan_action":"confirm",'
            '"reply":"已确认当前方案。","requirements":{}}'
        )

        # 第一步：模拟入口 Agent 将用户确认识别为待确认方案的确认动作。
        with patch(
            "app.agents.conversation_entry_agent.chat_with_llm",
            new_callable=AsyncMock,
            return_value=response,
        ):
            analysis = await analyze_conversation(
                [ClientChatMessage(role="user", content="就这样确认")],
                known_requirements=requirements,
                pending_plan=pending_plan,
            )

        # 第二步：确认动作不丢失快照中的完整需求，供图层直接结束确认分支。
        self.assertEqual(analysis.plan_action, "confirm")
        self.assertEqual(analysis.requirements, requirements)
        self.assertTrue(analysis.is_complete)

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

    def test_adds_pending_plan_summary_to_system_prompt(self) -> None:
        """待确认方案应让入口 Agent 获得确认或修改所需的最小上下文。"""

        prompt = _build_conversation_system_prompt(
            TripRequirements(destination="北京"),
            TripPlanSnapshot(
                requirements=TripRequirements(destination="北京"),
                proposal="## 行程概览\n北京三日行程",
                review_result=ReviewResult(
                    status="ready_for_confirmation",
                    summary="等待确认。",
                    risks=["酒店价格待核验"],
                ),
            ),
        )

        # 第一步：入口只需需求和审核摘要判断动作，不应重复注入完整草案。
        self.assertIn("当前存在待用户确认的方案快照", prompt)
        self.assertIn('"summary":"等待确认。"', prompt)
        self.assertNotIn("北京三日行程", prompt)

    def test_merge_requirements_revalidates_nested_models(self) -> None:
        """合并嵌套时长后应保留 TripDuration 模型而不是普通字典。"""

        known_requirements = TripRequirements(destination="北京")
        current_requirements = TripRequirements(
            trip_duration=TripDuration(
                raw_text="三天",
                amount=3,
                unit="day",
            )
        )

        # 第一步：模拟模型本轮只补充嵌套旅行时长字段。
        merged = _merge_requirements(known_requirements, current_requirements)

        # 第二步：确认重新校验后的嵌套字段可安全序列化和供规划 Agent 使用。
        self.assertIsInstance(merged.trip_duration, TripDuration)
        self.assertEqual(merged.trip_duration.amount, 3)

    def test_normalize_trip_dates_keeps_ambiguous_or_past_text_unchanged(self) -> None:
        """无法可靠换算的模糊或本周已过日期不得被猜测性改写。"""

        requirements = TripRequirements(
            departure_date="下个月",
            return_date="本周一",
        )

        # 第一步：以固定参考日验证日期规范化不会把模糊文本伪造成具体日期。
        normalized = _normalize_trip_dates(
            requirements,
            today=date(2026, 8, 14),
        )

        # 第二步：确认原始值保留，仍由后续追问或用户补充解决。
        self.assertEqual(normalized.departure_date, "下个月")
        self.assertEqual(normalized.return_date, "本周一")

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

        def response_validator(response: str) -> None:
            """模拟 Agent 对结构化输出的最小校验。"""

            # 第一步：仅接受第二次返回的有效响应。
            if response != "valid":
                raise ValueError("invalid structured response")

        # 第二步：替换退避等待，验证重试语义时不引入真实时间延迟。
        with (
            patch(
                "app.integrations.llm.client.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "app.integrations.llm.client._request_configured_provider",
                new_callable=AsyncMock,
                side_effect=["invalid", "valid"],
            ) as request_provider,
        ):
            result = await _call_with_retries(
                provider="deepseek",
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
        received_prompts = [
            call.args[3]
            for call in request_provider.await_args_list
        ]
        self.assertEqual(received_prompts[0], "只返回 JSON。")
        self.assertIn(STRUCTURED_OUTPUT_RETRY_INSTRUCTION, received_prompts[1] or "")

    async def test_deepseek_thinking_mode_follows_explicit_agent_setting(self) -> None:
        """DeepSeek 的 JSON、显式关闭和显式开启应传递正确思考模式。"""

        settings = SimpleNamespace(
            deepseek_api_key="deepseek-key",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-v4-flash",
            openai_api_key="openai-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-test",
            proxy_api_key="proxy-key",
            proxy_base_url="https://example.com/v1",
            proxy_model="proxy-test",
            llm_debug_log_raw_output=False,
        )
        messages = [ClientChatMessage(role="user", content="测试")]

        # 第一步：替换实际 HTTP 调用，只验证供应商映射传递的参数。
        with patch(
            "app.integrations.llm.client.request_openai_compatible_chat",
            new_callable=AsyncMock,
            return_value="{}",
        ) as request_chat:
            await _request_configured_provider(
                "deepseek",
                settings,
                messages,
                "仅返回 JSON。",
                0.1,
                100,
                True,
            )
            await _request_configured_provider(
                "deepseek",
                settings,
                messages,
                "生成 Markdown。",
                0.4,
                100,
                False,
            )
            await _request_configured_provider(
                "deepseek",
                settings,
                messages,
                "生成 Markdown。",
                0.4,
                100,
                False,
                True,
            )
            await _request_configured_provider(
                "openai",
                settings,
                messages,
                "仅返回 JSON。",
                0.1,
                100,
                True,
            )
            await _request_configured_provider(
                "deepseek",
                settings,
                messages,
                "先反思再总结。",
                0.2,
                1_600,
                True,
                model_override="deepseek-v4-pro",
                enable_thinking=True,
            )

        # 第二步：确认结构化或显式关闭的 DeepSeek 调用关闭思考，审核调用可显式开启。
        self.assertTrue(
            request_chat.await_args_list[0].kwargs["disable_thinking"]
        )
        self.assertFalse(
            request_chat.await_args_list[1].kwargs["disable_thinking"]
        )
        self.assertTrue(
            request_chat.await_args_list[2].kwargs["disable_thinking"]
        )
        self.assertFalse(
            request_chat.await_args_list[3].kwargs["disable_thinking"]
        )
        self.assertEqual(
            request_chat.await_args_list[4].kwargs["model"],
            "deepseek-v4-pro",
        )
        self.assertFalse(
            request_chat.await_args_list[4].kwargs["disable_thinking"]
        )
        self.assertTrue(
            request_chat.await_args_list[4].kwargs["enable_thinking"]
        )


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

"""_generate 的文本兜底逻辑：content 为空时回退 reasoning_content。"""

import asyncio

from astrbot_plugin_self_reply.main import Main


class FakeLLMResponse:
    def __init__(self, completion_text="", reasoning_content=""):
        self.completion_text = completion_text
        self.reasoning_content = reasoning_content


class FakeProvider:
    def __init__(self, response):
        self.response = response

    async def text_chat(self, **kwargs):
        return self.response


def _make_plugin():
    return Main(None, {"enable": True})


def _run_generate(provider):
    plugin = _make_plugin()
    return asyncio.run(
        plugin._generate(
            provider,
            "prompt",
            [],
            plugin._cfg(),
            persona_prompt="persona",
        )
    )


def test_uses_completion_text_when_not_empty():
    provider = FakeProvider(FakeLLMResponse("正常的猫娘回复", "思考过程"))
    assert _run_generate(provider) == "正常的猫娘回复"


def test_falls_back_to_reasoning_content_when_completion_text_empty():
    provider = FakeProvider(
        FakeLLMResponse(
            completion_text="",
            reasoning_content='<quote id="818827787"/>窝才不是大肥鱼，窝是小盐喵！',
        )
    )
    assert (
        _run_generate(provider)
        == '<quote id="818827787"/>窝才不是大肥鱼，窝是小盐喵！'
    )


def test_returns_empty_when_both_empty():
    provider = FakeProvider(FakeLLMResponse())
    assert _run_generate(provider) == ""

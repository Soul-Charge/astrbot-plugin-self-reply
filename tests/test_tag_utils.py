"""tag_utils：mention/quote/refuse/allowed 校验。"""

from astrbot.api.message_components import At, Plain, Reply

from astrbot_plugin_self_reply.tag_utils import (
    VALID_ID_RE,
    chain_has_refuse_tag,
    clean_response_text_for_history,
    has_refuse_tag,
    normalize_id,
    transform_result_chain,
)


def test_normalize_id_variants():
    assert normalize_id("#msg123") == "123"
    assert normalize_id("msg456") == "456"
    assert normalize_id("  789  ") == "789"
    assert normalize_id("MSG999") == "999"
    assert normalize_id(None) == ""
    assert normalize_id("") == ""
    assert normalize_id("msg") == ""


def test_valid_id_re():
    assert VALID_ID_RE.match("12345")
    assert not VALID_ID_RE.match("12a3")
    assert not VALID_ID_RE.match("")


def test_quote_tag_becomes_reply_component():
    chain = [Plain(text='<quote id="999"/> hello world')]
    out = transform_result_chain(chain, parse_mention=True)
    assert isinstance(out[0], Reply)
    assert out[0].id == "999"
    assert out[1].text.strip() == "hello world"


def test_allowed_msg_ids_filters_hallucinated_quote():
    chain = [Plain(text='<quote id="111"/> hi')]
    out = transform_result_chain(chain, True, allowed_msg_ids={"222"})
    assert all(not isinstance(c, Reply) for c in out)
    assert out[0].text.strip() == "hi"

    out2 = transform_result_chain(chain, True, allowed_msg_ids={"111"})
    assert isinstance(out2[0], Reply)
    assert out2[0].id == "111"


def test_no_allowed_set_keeps_quote():
    chain = [Plain(text='<quote id="42"/> hi')]
    out = transform_result_chain(chain, True, allowed_msg_ids=None)
    assert isinstance(out[0], Reply)


def test_mention_tag_becomes_at_component():
    chain = [Plain(text='<mention id="42"/> hi there')]
    out = transform_result_chain(chain, parse_mention=True)
    assert any(isinstance(c, At) and str(c.qq) == "42" for c in out)
    assert any(isinstance(c, Plain) and "hi there" in c.text for c in out)


def test_mention_ignored_when_parse_disabled():
    chain = [Plain(text='<mention id="42"/> hi')]
    assert transform_result_chain(chain, parse_mention=False) is None


def test_plain_text_without_tags_returns_none():
    assert transform_result_chain([Plain(text="普通消息")], parse_mention=True) is None


def test_non_plain_components_pass_through():
    reply = Reply(id="1")
    out = transform_result_chain([reply, Plain(text="<quote id='1'/> x")], True)
    # quote tag 解析出的 Reply 插在链首，原组件按原样透传
    assert isinstance(out[0], Reply)
    assert out[0].id == "1"
    assert out[1] is reply


def test_refuse_detection():
    assert has_refuse_tag("<refuse/>")
    assert has_refuse_tag("  <refuse/>  ")
    assert not has_refuse_tag("<refuse/> ok")
    assert not has_refuse_tag("hello")
    assert not has_refuse_tag(None)
    assert chain_has_refuse_tag([Plain(text="<refuse/>")])
    assert not chain_has_refuse_tag([Plain(text="<refuse/>"), Plain(text="x")])


def test_clean_response_text_for_history():
    raw = '<quote id="1"/>你好 <mention id="5"/>小明</mention>'
    cleaned = clean_response_text_for_history(raw)
    assert cleaned == "你好 [At: 5]小明"

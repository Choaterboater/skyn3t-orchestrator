"""Phase 5B — Asia-market messaging channel adapters.

Covers the five new channels that bring SkyN3t to parity with Hermes for
Asian platforms: DingTalk, WeCom (WeChat Work), WeChat (Official Account),
LINE, and KakaoTalk.

Each adapter mirrors an existing channel in ``messaging.py``:
  - DingTalk/WeCom/WeChat follow FeishuChannel (token-fetch -> send)
  - LINE/KakaoTalk follow WhatsAppChannel (single key, webhook inbound)

All sends are exercised against a fake httpx client — no real network.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.channel_dingtalk import DingTalkChannel
from skyn3t.integrations.channel_kakaotalk import KakaoTalkChannel
from skyn3t.integrations.channel_line import LineChannel
from skyn3t.integrations.channel_wechat import WeChatChannel
from skyn3t.integrations.channel_wecom import WeComChannel


class _Resp:
    def __init__(self, status: int = 200, text: str = "", json_body: Any = None):
        self.status_code = status
        self.text = text
        self._json = json_body

    def json(self):
        return self._json or {}


class _FakeHttp:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.responses: Dict[str, _Resp] = {}
        self.default_response = _Resp(status=200, json_body={})

    async def post(self, url, json=None, data=None, params=None, headers=None):
        self.calls.append({
            "method": "POST", "url": url, "json": json, "data": data,
            "params": params or {}, "headers": headers or {},
        })
        for marker, resp in self.responses.items():
            if marker in url:
                return resp
        return self.default_response

    async def get(self, url, params=None, headers=None):
        self.calls.append({
            "method": "GET", "url": url, "json": None, "data": None,
            "params": params or {}, "headers": headers or {},
        })
        for marker, resp in self.responses.items():
            if marker in url:
                return resp
        return self.default_response

    async def aclose(self):
        pass


# ─── DingTalk ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dingtalk_inbound_normalizes_text_message():
    ch = DingTalkChannel(EventBus(), app_key="ak", app_secret="as")
    raw = {
        "msgtype": "text",
        "text": {"content": "build me a thing"},
        "senderStaffId": "staff-1",
        "conversationId": "cid-9",
        "msgId": "m-7",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "dingtalk"
    assert msg.channel == "cid-9"
    assert msg.sender == "staff-1"
    assert msg.text == "build me a thing"
    assert msg.thread == "m-7"


@pytest.mark.asyncio
async def test_dingtalk_inbound_skips_non_text():
    ch = DingTalkChannel(EventBus(), app_key="ak", app_secret="as")
    assert await ch.handle_inbound({"msgtype": "image"}) is None
    assert await ch.handle_inbound({"msgtype": "text", "text": {"content": ""}}) is None


@pytest.mark.asyncio
async def test_dingtalk_send_acquires_token_then_posts():
    ch = DingTalkChannel(EventBus(), app_key="ak", app_secret="as", robot_code="rc")
    fake = _FakeHttp()
    fake.responses["oauth2/accessToken"] = _Resp(
        json_body={"accessToken": "tok-1", "expireIn": 7200},
    )
    ch._http = fake
    await ch.send("user-1", "ack")
    assert len(fake.calls) == 2
    token_call, msg_call = fake.calls
    assert "oauth2/accessToken" in token_call["url"]
    assert "batchSend" in msg_call["url"]
    assert msg_call["headers"]["x-acs-dingtalk-access-token"] == "tok-1"
    assert msg_call["json"]["userIds"] == ["user-1"]
    import json as _json
    assert _json.loads(msg_call["json"]["msgParam"]) == {"content": "ack"}


@pytest.mark.asyncio
async def test_dingtalk_send_caches_token():
    ch = DingTalkChannel(EventBus(), app_key="ak", app_secret="as")
    fake = _FakeHttp()
    fake.responses["oauth2/accessToken"] = _Resp(
        json_body={"accessToken": "tok", "expireIn": 7200},
    )
    ch._http = fake
    await ch.send("u1", "one")
    await ch.send("u2", "two")
    token_calls = [c for c in fake.calls if "oauth2/accessToken" in c["url"]]
    assert len(token_calls) == 1


@pytest.mark.asyncio
async def test_dingtalk_unavailable_and_send_noop_without_creds():
    ch = DingTalkChannel(EventBus(), app_key="", app_secret="")
    assert ch.is_available() is False
    ch._http = _FakeHttp()
    await ch.send("u", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── WeCom (WeChat Work) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wecom_inbound_normalizes_text_message():
    ch = WeComChannel(EventBus(), corp_id="c", corp_secret="s", agent_id="1000002")
    raw = {
        "MsgType": "text",
        "Content": "deploy the app",
        "FromUserName": "alice",
        "MsgId": "9988",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "wecom"
    assert msg.channel == "alice"
    assert msg.sender == "alice"
    assert msg.text == "deploy the app"
    assert msg.thread == "9988"


@pytest.mark.asyncio
async def test_wecom_inbound_skips_non_text():
    ch = WeComChannel(EventBus(), corp_id="c", corp_secret="s", agent_id="a")
    assert await ch.handle_inbound({"MsgType": "image"}) is None


@pytest.mark.asyncio
async def test_wecom_send_acquires_token_then_posts():
    ch = WeComChannel(EventBus(), corp_id="corp", corp_secret="sec", agent_id="1000002")
    fake = _FakeHttp()
    fake.responses["gettoken"] = _Resp(
        json_body={"errcode": 0, "access_token": "atk", "expires_in": 7200},
    )
    ch._http = fake
    await ch.send("alice", "ack")
    assert len(fake.calls) == 2
    token_call, msg_call = fake.calls
    assert "gettoken" in token_call["url"]
    assert token_call["params"]["corpid"] == "corp"
    assert "message/send" in msg_call["url"]
    assert msg_call["params"]["access_token"] == "atk"
    assert msg_call["json"]["touser"] == "alice"
    assert msg_call["json"]["agentid"] == "1000002"
    assert msg_call["json"]["text"]["content"] == "ack"


@pytest.mark.asyncio
async def test_wecom_send_handles_token_errcode():
    ch = WeComChannel(EventBus(), corp_id="c", corp_secret="s", agent_id="a")
    fake = _FakeHttp()
    fake.responses["gettoken"] = _Resp(
        json_body={"errcode": 40013, "errmsg": "invalid corpid"},
    )
    ch._http = fake
    await ch.send("u", "x")
    send_calls = [c for c in fake.calls if "message/send" in c["url"]]
    assert send_calls == []


@pytest.mark.asyncio
async def test_wecom_unavailable_without_agent_id():
    # corp/secret present but agent_id missing => not available.
    ch = WeComChannel(EventBus(), corp_id="c", corp_secret="s", agent_id="")
    assert ch.is_available() is False
    ch._http = _FakeHttp()
    await ch.send("u", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── WeChat Official Account ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_wechat_inbound_normalizes_text_message():
    ch = WeChatChannel(EventBus(), app_id="a", app_secret="b")
    raw = {
        "MsgType": "text",
        "Content": "hello there",
        "FromUserName": "oOpenId",
        "MsgId": "555",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "wechat"
    assert msg.channel == "oOpenId"
    assert msg.sender == "oOpenId"
    assert msg.text == "hello there"
    assert msg.thread == "555"


@pytest.mark.asyncio
async def test_wechat_send_acquires_token_then_posts():
    ch = WeChatChannel(EventBus(), app_id="appid", app_secret="secret")
    fake = _FakeHttp()
    fake.responses["cgi-bin/token"] = _Resp(
        json_body={"access_token": "wxtok", "expires_in": 7200},
    )
    ch._http = fake
    await ch.send("oOpenId", "ack")
    assert len(fake.calls) == 2
    token_call, msg_call = fake.calls
    assert "cgi-bin/token" in token_call["url"]
    assert token_call["params"]["appid"] == "appid"
    assert "custom/send" in msg_call["url"]
    assert msg_call["params"]["access_token"] == "wxtok"
    assert msg_call["json"]["touser"] == "oOpenId"
    assert msg_call["json"]["text"]["content"] == "ack"


@pytest.mark.asyncio
async def test_wechat_send_handles_token_errcode():
    ch = WeChatChannel(EventBus(), app_id="a", app_secret="b")
    fake = _FakeHttp()
    fake.responses["cgi-bin/token"] = _Resp(
        json_body={"errcode": 40013, "errmsg": "invalid appid"},
    )
    ch._http = fake
    await ch.send("o", "x")
    send_calls = [c for c in fake.calls if "custom/send" in c["url"]]
    assert send_calls == []


@pytest.mark.asyncio
async def test_wechat_unavailable_and_noop_without_creds():
    ch = WeChatChannel(EventBus(), app_id="", app_secret="")
    assert ch.is_available() is False
    ch._http = _FakeHttp()
    await ch.send("o", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── LINE ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_line_inbound_normalizes_text_event():
    ch = LineChannel(EventBus(), channel_access_token="tok")
    raw = {
        "events": [{
            "type": "message",
            "replyToken": "rt-abc",
            "source": {"type": "user", "userId": "Uxyz"},
            "message": {"type": "text", "id": "mid-1", "text": "build me a thing"},
        }]
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "line"
    assert msg.channel == "Uxyz"
    assert msg.sender == "Uxyz"
    assert msg.text == "build me a thing"
    assert msg.thread == "rt-abc"


@pytest.mark.asyncio
async def test_line_inbound_skips_non_message_and_non_text():
    ch = LineChannel(EventBus(), channel_access_token="tok")
    assert await ch.handle_inbound({"events": [{"type": "follow"}]}) is None
    assert await ch.handle_inbound({
        "events": [{"type": "message", "message": {"type": "image"}}]
    }) is None
    assert await ch.handle_inbound({"events": []}) is None


@pytest.mark.asyncio
async def test_line_send_uses_reply_endpoint_with_token():
    ch = LineChannel(EventBus(), channel_access_token="tok")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("Uxyz", "ack", thread="rt-abc")
    call = fake.calls[0]
    assert "message/reply" in call["url"]
    assert call["json"]["replyToken"] == "rt-abc"
    assert call["json"]["messages"][0]["text"] == "ack"
    assert call["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_line_send_falls_back_to_push_without_thread():
    ch = LineChannel(EventBus(), channel_access_token="tok")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("Uxyz", "ack")
    call = fake.calls[0]
    assert "message/push" in call["url"]
    assert call["json"]["to"] == "Uxyz"
    assert call["json"]["messages"][0]["text"] == "ack"


@pytest.mark.asyncio
async def test_line_unavailable_and_noop_without_token():
    ch = LineChannel(EventBus(), channel_access_token="")
    assert ch.is_available() is False
    ch._http = _FakeHttp()
    await ch.send("U", "x", thread="rt")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── KakaoTalk ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kakao_inbound_normalizes_skill_payload():
    ch = KakaoTalkChannel(EventBus(), rest_api_key="key")
    raw = {
        "userRequest": {
            "utterance": "deploy the bot",
            "user": {"id": "kakao-uid", "type": "botUserKey"},
        },
        "bot": {"id": "b1"},
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "kakaotalk"
    assert msg.channel == "kakao-uid"
    assert msg.sender == "kakao-uid"
    assert msg.text == "deploy the bot"


@pytest.mark.asyncio
async def test_kakao_inbound_skips_empty_utterance():
    ch = KakaoTalkChannel(EventBus(), rest_api_key="key")
    assert await ch.handle_inbound({"userRequest": {"utterance": ""}}) is None
    assert await ch.handle_inbound({}) is None


@pytest.mark.asyncio
async def test_kakao_send_posts_template_object():
    ch = KakaoTalkChannel(EventBus(), rest_api_key="restkey")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("kakao-uid", "ack")
    call = fake.calls[0]
    assert "memo/default/send" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer restkey"
    import json as _json
    template = _json.loads(call["data"]["template_object"])
    assert template["object_type"] == "text"
    assert template["text"] == "ack"


@pytest.mark.asyncio
async def test_kakao_unavailable_and_noop_without_key():
    ch = KakaoTalkChannel(EventBus(), rest_api_key="")
    assert ch.is_available() is False
    ch._http = _FakeHttp()
    await ch.send("u", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── Availability is true when creds present ───────────────────────────


def test_is_available_true_when_creds_present():
    bus = EventBus()
    assert DingTalkChannel(bus, app_key="k", app_secret="s").is_available()
    assert WeComChannel(bus, corp_id="c", corp_secret="s", agent_id="a").is_available()
    assert WeChatChannel(bus, app_id="a", app_secret="b").is_available()
    assert LineChannel(bus, channel_access_token="t").is_available()
    assert KakaoTalkChannel(bus, rest_api_key="k").is_available()


# ─── Cross-channel publish ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_asia_channel_publishes_task_created():
    bus = EventBus()
    captured: List[Any] = []
    bus.subscribe(captured.append, EventType.TASK_CREATED)

    dt = DingTalkChannel(bus, app_key="k", app_secret="s")
    wc = WeComChannel(bus, corp_id="c", corp_secret="s", agent_id="a")
    wx = WeChatChannel(bus, app_id="a", app_secret="b")
    ln = LineChannel(bus, channel_access_token="t")
    kk = KakaoTalkChannel(bus, rest_api_key="k")

    await dt.ingest({
        "msgtype": "text", "text": {"content": "dt msg"},
        "conversationId": "cid", "senderStaffId": "u", "msgId": "1",
    })
    await wc.ingest({
        "MsgType": "text", "Content": "wc msg", "FromUserName": "u", "MsgId": "2",
    })
    await wx.ingest({
        "MsgType": "text", "Content": "wx msg", "FromUserName": "o", "MsgId": "3",
    })
    await ln.ingest({
        "events": [{
            "type": "message", "replyToken": "rt",
            "source": {"userId": "U"}, "message": {"type": "text", "text": "ln msg"},
        }]
    })
    await kk.ingest({
        "userRequest": {"utterance": "kk msg", "user": {"id": "uid"}},
    })

    platforms = {e.payload["platform"] for e in captured}
    texts = {e.payload["message"] for e in captured}
    assert platforms == {"dingtalk", "wecom", "wechat", "line", "kakaotalk"}
    assert texts == {"dt msg", "wc msg", "wx msg", "ln msg", "kk msg"}

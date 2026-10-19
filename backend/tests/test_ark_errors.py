"""上游错误要指出该改参考视频，并保留排查信息与历史原文。"""
import httpx
import pytest
from types import SimpleNamespace

from app.core.ark_client import _summarize_error, friendly_ark_error


PIXELS = ("The parameter `content[2]` specified in the request is not valid: "
          "the parameter video pixel count specified in the request must be "
          "greater than or equal to 407696 for model doubao-seedance-2-0-mini in r2v. "
          "Request id: example-request")


def test_video_pixels_explains_reference_and_keeps_raw_log(caplog):
    response = httpx.Response(400, json={"error": {"message": PIXELS}})
    text = _summarize_error(response)
    assert "参考视频分辨率过低" in text
    assert "407,696" in text and "宽×高" in text
    assert "修改生成结果的分辨率无效" in text
    assert "HTTP 400" in text
    assert "content[2]" not in text
    assert PIXELS in caplog.text


def test_upper_pixel_limit_does_not_suggest_enlarging():
    text = friendly_ark_error(PIXELS.replace("greater than", "less than"))
    assert "分辨率过高" in text and "不能超过 407,696" in text
    assert "缩小" in text and "放大" not in text


@pytest.mark.parametrize("error, expected", [
    ("insufficient balance", "余额不足"),
    ("rate limit", "请求过于频繁"),
    ("model not found", "模型不存在或未开通"),
])
def test_known_errors_offer_chinese_actions(error, expected):
    text = _summarize_error(httpx.Response(400, json={"error": {"message": error}}))
    assert expected in text
    assert error not in text


def test_unknown_or_non_json_error_keeps_diagnostics():
    assert "upstream unavailable" in _summarize_error(
        httpx.Response(502, text="upstream unavailable"))
    image_error = PIXELS.replace("video pixel", "image pixel")
    assert friendly_ark_error(image_error) == image_error


def test_historical_take_translation_does_not_rewrite_record(monkeypatch):
    from app.api import generate
    original = "创建任务失败: HTTP 400: " + PIXELS
    stored = {"current": None, "takes": [{"id": "failed", "error": original}]}
    class FakeSession:
        storage = None
    monkeypatch.setattr(generate, "get_session", lambda _: FakeSession())
    monkeypatch.setattr(generate, "TakeStore", lambda _: SimpleNamespace(list=lambda: stored))
    shown = generate.list_takes("test")["takes"][0]
    assert "参考视频分辨率过低" in shown["error"]
    assert shown["error_detail"] == original
    assert stored["takes"][0]["error"] == original

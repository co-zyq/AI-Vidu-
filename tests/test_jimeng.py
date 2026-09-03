from jimeng_app import _ext_from_content_type, _is_logged_in_text, _safe_download_name, _validate_request_inputs


def test_safe_download_name_sanitizes():
    assert _safe_download_name("a/b c.png") == "b_c.png"
    assert _safe_download_name("jimeng") == "jimeng.png"
    assert _safe_download_name("结果.webp") == "jimeng.webp"


def test_is_logged_in_uses_real_markers():
    assert _is_logged_in_text("退出登录 个人中心 我的作品") is True
    assert _is_logged_in_text("立即登录 扫码登录") is False


def test_ext_from_content_type():
    assert _ext_from_content_type("image/webp", "https://x/a") == "webp"
    assert _ext_from_content_type("image/jpeg", "https://x/a") == "jpg"
    assert _ext_from_content_type("", "https://x/a.png?sign=1") == "png"


def test_validate_request_inputs_rejects_empty():
    try:
        _validate_request_inputs([], "提示词", 1)
        assert False, "empty images should be rejected"
    except ValueError as exc:
        assert "至少上传一张图片" in str(exc)

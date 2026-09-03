import os
import threading

from app import (
    _cancel_job,
    _download_button_matches,
    _extract_block_reason,
    _has_completion_signal,
    _is_logged_in_text,
    _safe_download_name,
    _validate_request_inputs,
)


def test_validate_request_inputs_rejects_empty_uploads():
    try:
        _validate_request_inputs([], "电影镜头", 1)
        assert False, "empty image list should be rejected"
    except ValueError as exc:
        assert "至少上传一张图片" in str(exc)


def test_download_button_matches_download_text():
    assert _download_button_matches("下载") is True
    assert _download_button_matches("下载视频") is True
    assert _download_button_matches("查看详情") is False


def test_safe_download_name_handles_spaces_and_slashes():
    name = _safe_download_name("movie/clip 01.mp4")
    assert name.endswith("clip_01.mp4")
    assert os.sep not in name


def test_completion_signal_requires_real_evidence():
    assert _has_completion_signal(["生成中", "排队中"]) is False
    assert _has_completion_signal(["生成完成", "下载"]) is True


def test_block_reason_detects_insufficient_credits():
    assert _extract_block_reason(["积分不足 订阅套餐"]) == "积分不足"
    assert _extract_block_reason(["login required"]) == "未登录"
    assert _extract_block_reason(["升级套餐 继续生成"]) == "订阅套餐"


def test_logged_in_text_detection_uses_real_state_markers():
    assert _is_logged_in_text("退出登录 个人中心 我的作品") is True
    assert _is_logged_in_text("登录 立即登录") is False


def test_cancel_job_marks_stuck_task_as_cancelled():
    from app import JOB_ARCHIVE, JOB_STORE, _cancel_job

    job_id = "test-job-cancel"
    JOB_STORE[job_id] = {"job_id": job_id, "status": "running", "progress": 50, "cancel_event": threading.Event()}

    _cancel_job(job_id)

    assert job_id not in JOB_STORE
    assert JOB_ARCHIVE[job_id]["status"] == "cancelled"
    assert JOB_ARCHIVE[job_id]["error"] == "任务已被手动终止"
    assert JOB_ARCHIVE[job_id]["cancel_event"].is_set() is True

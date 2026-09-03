import atexit
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import List

from flask import Flask, jsonify, render_template_string, request, send_from_directory
from werkzeug.utils import secure_filename

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

app = Flask(__name__)
SITE_URL = "https://jimeng.jianying.com/ai-tool/image/generate"
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), "jimeng_downloads")
PROFILE_DIR = os.path.join(os.getcwd(), "browser_profile_jimeng")
BROWSER_PROFILE_DIR = os.path.abspath(os.getenv("JIMENG_BROWSER_PROFILE_DIR", PROFILE_DIR))
GENERATE_BUTTONS = ["生成", "开始生成", "立即生成"]
WAIT_MARKERS = ["生成中", "排队中", "创作中", "处理中", "生成完成", "生成完毕", "已完成", "成功"]
RATE_LIMIT_MARKERS = ["操作过于频繁", "请求过于频繁", "操作太频繁", "频率过快", "稍后再试", "已达上限", "429"]
JOB_INTERVAL = int(os.getenv("JIMENG_JOB_INTERVAL", "10"))
TS = time.strftime("%Y%m%d_%H%M%S")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)
JOB_STORE: dict[str, dict] = {}
JOB_ARCHIVE: dict[str, dict] = {}
JOB_CANCELLED: set[str] = set()
JOB_QUEUE: deque[str] = deque()
JOB_QUEUE_LOCK = threading.Lock()
JOB_QUEUE_WORKER_RUNNING = False
_BROWSER_LOCK = threading.Lock()
_BROWSER = {"playwright": None, "context": None, "page": None}
JOB_QUEUE_COND = threading.Condition(JOB_QUEUE_LOCK)


class _JobCancelled(Exception):
    pass


def _resolve_browser_profile_dir():
    env_dir = os.getenv("JIMENG_BROWSER_PROFILE_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    return os.path.abspath(PROFILE_DIR)


def _launch_context(p, profile_dir: str | None = None, headless: bool = False):
    target = profile_dir or _resolve_browser_profile_dir()
    os.makedirs(target, exist_ok=True)
    return p.chromium.launch_persistent_context(
        target,
        headless=headless,
        viewport={"width": 1440, "height": 1100},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _get_shared_context_locked():
    if _BROWSER["context"] is None:
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright 未安装。请运行: .venv/bin/python -m pip install playwright && .venv/bin/python -m playwright install chromium"
            )
        playwright = sync_playwright().start()
        context = _launch_context(playwright)
        _BROWSER["playwright"] = playwright
        _BROWSER["context"] = context
    return _BROWSER["context"]


def _get_shared_page():
    context = _get_shared_context_locked()
    if _BROWSER["page"] is None:
        _BROWSER["page"] = context.new_page()
    return _BROWSER["page"]


@contextmanager
def _shared_page():
    yield _get_shared_page()


def _has_login_cookie(context) -> bool:
    try:
        cookies = context.cookies("https://jimeng.jianying.com")
    except Exception:
        return False
    login_names = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt"}
    return any(c.get("name") in login_names and c.get("value") for c in cookies)


def _stop_browser():
    with _BROWSER_LOCK:
        if _BROWSER["context"] is not None:
            try:
                _BROWSER["context"].close()
            except Exception:
                pass
        if _BROWSER["playwright"] is not None:
            try:
                _BROWSER["playwright"].stop()
            except Exception:
                pass
        _BROWSER["context"] = None
        _BROWSER["playwright"] = None


atexit.register(_stop_browser)


def _job_is_cancelled(job_id: str) -> bool:
    job = JOB_STORE.get(job_id)
    if job is None:
        return True
    cancel_event = job.get("cancel_event")
    if cancel_event is not None and cancel_event.is_set():
        return True
    return False


def _cancel_job(job_id: str):
    job = JOB_STORE.get(job_id)
    if job is None:
        return False

    cancel_event = job.get("cancel_event")
    if cancel_event is not None:
        cancel_event.set()

    final_job = dict(job)
    final_job["status"] = "cancelled"
    final_job["progress"] = 100
    final_job["error"] = "任务已被手动终止"
    final_job["result"] = {"status": "cancelled", "message": "任务已被手动终止"}
    JOB_ARCHIVE[job_id] = final_job
    JOB_CANCELLED.add(job_id)
    JOB_STORE.pop(job_id, None)
    return True


def _is_logged_in_text(text):
    if text is None:
        return False
    normalized = re.sub(r"\s+", "", str(text).lower())
    logged_in_markers = [
        "退出登录",
        "个人中心",
        "我的作品",
        "已登录",
        "logout",
        "profile",
        "account",
        "myworks",
    ]
    not_logged_in_markers = [
        "请先登录",
        "立即登录",
        "扫码登录",
        "登录/注册",
        "手机号登录",
        "抖音登录",
    ]
    if any(marker in normalized for marker in not_logged_in_markers):
        return False
    if any(marker in normalized for marker in logged_in_markers):
        return True
    if "登录" in normalized and "退出登录" not in normalized and "个人中心" not in normalized and "我的作品" not in normalized:
        return False
    return False


def _safe_download_name(filename: str) -> str:
    raw = os.path.basename(filename or "jimeng.png")
    raw = raw.replace("/", "_").replace("\\", "_")
    ext_match = re.search(r"\.(png|jpe?g|webp|gif|bmp|avif)$", raw.lower())
    ext = ext_match.group(1) if ext_match else "png"
    stem = raw[: -len(ext_match.group(0))] if ext_match else raw
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("._")
    if not stem:
        stem = "jimeng"
    return f"{stem}.{ext}"


def _unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    index = 1
    while target.exists():
        stem, ext = os.path.splitext(name)
        target = directory / f"{stem}_{index}{ext}"
        index += 1
    return target


def _ext_from_content_type(content_type: str, url: str) -> str:
    mapping = {
        "image/webp": "webp",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
    }
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in mapping:
        return mapping[ctype]
    match = re.search(r"\.(png|jpe?g|webp|gif|bmp|avif)($|\?)", url.lower())
    if match:
        return match.group(1)
    return "png"


def _download_button_matches(text):
    if text is None:
        return False
    normalized = re.sub(r"\s+", "", str(text).lower())
    return "下载" in normalized or normalized in {"download", "downloadimage"}


def _has_completion_signal(history):
    if not history:
        return False
    text = " ".join(str(item) for item in history).lower()
    signals = [
        "下载",
        "已完成",
        "完成",
        "成功",
        "生成完成",
        "download",
        "completed",
        "success",
    ]
    return any(signal in text for signal in signals)


def _extract_block_reason(history):
    if not history:
        return None
    text = " ".join(str(item) for item in history)
    lowered = text.lower()
    if any(marker in text for marker in RATE_LIMIT_MARKERS):
        return "操作过于频繁（429），请等待几分钟后再试"
    if "积分不足" in text or "insufficient" in lowered or "credits" in lowered:
        return "积分不足"
    if (
        "订阅套餐" in text
        or "升级套餐" in text
        or "升级会员" in text
        or "subscribe" in lowered
        or "subscription" in lowered
        or "plan" in lowered
        or "upgrade" in lowered
    ):
        return "订阅套餐"
    if "退出登录" in text or "个人中心" in text or "我的作品" in text:
        return None
    if "请先登录" in text or "立即登录" in text or "登录后" in text or "login" in lowered:
        return "未登录"
    return None


def _normalise_images(images):
    if isinstance(images, str):
        images = [images]
    if not isinstance(images, list):
        raise ValueError("images must be a list of file paths")

    result = []
    for image in images:
        if not os.path.exists(image):
            raise FileNotFoundError(f"image not found: {image}")
        result.append(os.path.abspath(image))
    return result


def _validate_request_inputs(images, prompt: str, count: int):
    if not images:
        raise ValueError("至少上传一张图片")
    if len(images) > 40:
        raise ValueError("最多支持 40 张图片")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    return 1


def _job_summary(job: dict) -> dict:
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "progress": job.get("progress"),
        "prompt": job.get("prompt"),
        "images": job.get("images"),
        "error": job.get("error"),
        "result": job.get("result"),
        "history": job.get("history"),
        "created_at": job.get("created_at"),
    }


def _ensure_job_queue_worker():
    global JOB_QUEUE_WORKER_RUNNING
    if JOB_QUEUE_WORKER_RUNNING:
        return

    def _worker():
        while True:
            with JOB_QUEUE_LOCK:
                while not JOB_QUEUE:
                    JOB_QUEUE_COND.wait()
                item = JOB_QUEUE.popleft()

            if item == "login":
                _open_login_page()
                continue

            job = JOB_STORE.get(item)
            if job is None:
                continue
            try:
                _submit_job(item, job.get("images") or [], job.get("prompt") or "", job.get("count", 1))
            except Exception:
                if item in JOB_STORE:
                    JOB_STORE[item]["status"] = "failed"
                    JOB_STORE[item]["progress"] = 100
                    JOB_STORE[item]["error"] = "queued worker crashed"
            time.sleep(JOB_INTERVAL)

    JOB_QUEUE_WORKER_RUNNING = True
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def _create_job(image_path: str, prompt: str, count: int):
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    JOB_STORE[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "images": [image_path],
        "prompt": prompt,
        "count": count,
        "result": None,
        "error": None,
        "created_at": time.time(),
        "page": None,
        "cancel_event": cancel_event,
    }
    with JOB_QUEUE_LOCK:
        JOB_QUEUE.append(job_id)
        JOB_QUEUE_COND.notify()
    _ensure_job_queue_worker()
    return job_id


def _open_login_page():
    try:
        page = _get_shared_page()
        page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
        # 标签页保持打开，登录态持久化在浏览器配置目录
    except Exception:
        pass


def _upload_image(page, image_path: str):
    file_inputs = page.locator("input[type='file']")
    count = file_inputs.count()
    if count == 0:
        raise RuntimeError("找不到上传控件 input[type='file']。可访问 /probe 查看页面结构。")
    target = None
    image_input = page.locator("input[type='file'][accept*='image']").first
    if image_input.count() > 0:
        target = image_input
    else:
        for i in range(count):
            if file_inputs.nth(i).is_visible():
                target = file_inputs.nth(i)
                break
    if target is None:
        target = file_inputs.first
    before = {
        img.get_attribute("src") or ""
        for img in page.locator("img").all()
        if (img.get_attribute("src") or "").startswith("http")
    }
    target.set_input_files(image_path)
    # 等参考图预览出现（最多 20 秒），避免把预览误当成生成结果
    deadline = time.time() + 20
    while time.time() < deadline:
        now = {
            img.get_attribute("src") or ""
            for img in page.locator("img").all()
            if (img.get_attribute("src") or "").startswith("http")
        }
        if now - before:
            page.wait_for_timeout(3000)
            return
        page.wait_for_timeout(1000)
    page.wait_for_timeout(3000)


def _fill_prompt(page, prompt: str):
    textareas = page.locator("textarea")
    for i in range(textareas.count()):
        ta = textareas.nth(i)
        try:
            if not ta.is_visible():
                continue
        except Exception:
            continue
        try:
            ta.fill(prompt)
            return
        except Exception:
            break
    editables = page.locator("[contenteditable='true']")
    for i in range(editables.count()):
        ed = editables.nth(i)
        try:
            if not ed.is_visible():
                continue
        except Exception:
            continue
        ed.click(timeout=5000)
        page.keyboard.insert_text(prompt)
        return
    raise RuntimeError("找不到提示词输入框（textarea 或 contenteditable）。可访问 /probe 查看页面结构。")


def _click_generate(page):
    # 即梦图片工作区的“生成”是右下角圆形箭头（submit）按钮
    submit_selectors = [
        "button.submit",
        "button[class*='submit-button']",
        "button.lv-btn-primary.lv-btn-shape-circle",
    ]
    for selector in submit_selectors:
        buttons = page.locator(selector)
        for i in range(buttons.count()):
            btn = buttons.nth(i)
            try:
                if btn.is_visible() and btn.is_enabled():
                    btn.click(timeout=5000)
                    return
            except Exception:
                continue
    # 文本兜底
    exact = page.get_by_text("生成", exact=True)
    try:
        if exact.count() > 0:
            loc = exact.first
            for _ in range(2):
                if loc.is_visible() and loc.is_enabled():
                    loc.click(timeout=5000)
                    return
                page.wait_for_timeout(2000)
    except Exception:
        pass
    for cand in GENERATE_BUTTONS:
        buttons = page.locator("button", has_text=cand)
        for i in range(buttons.count()):
            btn = buttons.nth(i)
            try:
                if btn.is_visible() and btn.is_enabled():
                    btn.click(timeout=5000)
                    return
            except Exception:
                continue
    raise RuntimeError("找不到“生成”按钮（右下角箭头）。可访问 /probe 查看页面结构。")


def _try_fallback_image(page, context, download_dir: Path, initial_imgs: set):
    new_images = []
    for img in page.locator("img").all():
        src = img.get_attribute("src") or ""
        if not src.startswith("http") or src in initial_imgs:
            continue
        if re.search(r"\.svg($|\?)", src):
            continue
        new_images.append(src)
    if not new_images:
        return None
    src = new_images[-1]
    resp = context.request.get(src, timeout=60000)
    if not resp.ok:
        return None
    ext = _ext_from_content_type(resp.headers.get("content-type", ""), src)
    target = _unique_path(download_dir, f"jimeng_{TS}.{ext}")
    target.write_bytes(resp.body())
    return str(target)


def _wait_for_result(page, context, job_id: str, download_dir: Path, initial_imgs: set, baseline_markers: set):
    deadline = time.time() + 180
    history_items = []
    started = False
    retried = False
    click_start = time.time()
    while time.time() < deadline:
        try:
            locators = page.locator("button, a, [role='button'], li, div, tr, span").all()
            for locator in locators:
                try:
                    text = locator.inner_text(timeout=200)
                except Exception:
                    continue
                if not text:
                    continue
                clean = " ".join(text.split())
                if any(token in clean for token in ["任务", "历史", "下载", "完成", "已完成", "成功", "生成", "排队", "失败", "积分不足", "升级会员", "登录"]):
                    if clean not in history_items:
                        history_items.append(clean)
                        if len(history_items) > 15:
                            history_items = history_items[-15:]
        except Exception:
            pass
        JOB_STORE[job_id]["history"] = history_items

        if _job_is_cancelled(job_id):
            raise _JobCancelled()

        block_reason = _extract_block_reason(history_items)
        if block_reason:
            raise RuntimeError(block_reason)

        joined = " ".join(history_items)
        new_markers = [marker for marker in WAIT_MARKERS if marker in joined and marker not in baseline_markers]
        if not started and new_markers:
            started = True
            JOB_STORE[job_id]["history"] = (JOB_STORE[job_id].get("history") or []) + ["已确认生成开始，等待结果"]

        if not started and not retried and time.time() - click_start >= 15:
            # 点击后一直没有生成迹象，再点一次“生成”
            if not any(marker in joined for marker in RATE_LIMIT_MARKERS):
                try:
                    _click_generate(page)
                    retried = True
                    JOB_STORE[job_id]["history"] = (JOB_STORE[job_id].get("history") or []) + ["已再次点击生成"]
                except Exception:
                    retried = True
            else:
                retried = True

        if not started and time.time() - click_start >= 30:
            raise RuntimeError("已点击“生成”，但 30 秒内没有检测到任务开始（生成中/排队中/创作中）。请到浏览器窗口确认页面状态，再重新提交任务。")

        # 只有确认生成已开始后才接受新图片，避免误抓页面上的旧资产图
        if started:
            saved = _try_fallback_image(page, context, download_dir, initial_imgs)
            if saved:
                return saved

        page.wait_for_timeout(2000)
    return None


def _submit_job(job_id: str, images: List[str], prompt: str, count: int = 1):
    try:
        if _job_is_cancelled(job_id):
            return

        JOB_STORE[job_id]["status"] = "running"
        JOB_STORE[job_id]["progress"] = 5

        with _shared_page() as page:
            context = _BROWSER["context"]
            if _job_is_cancelled(job_id):
                return
            JOB_STORE[job_id]["page"] = page
            for image_index, image_path in enumerate(images):
                page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
                try:
                    page.locator("input[type='file'][accept*='image']").first.wait_for(state="attached", timeout=30000)
                except Exception:
                    pass
                if _job_is_cancelled(job_id):
                    return
                JOB_STORE[job_id]["progress"] = 15 + min(60, image_index * 20)

                page_text = page.locator("body").inner_text(timeout=15000)
                login_state = "logged_in" if _has_login_cookie(context) else "unknown"
                JOB_STORE[job_id]["login_state"] = login_state
                if login_state != "logged_in":
                    JOB_STORE[job_id]["status"] = "blocked"
                    JOB_STORE[job_id]["error"] = "自动化浏览器里没有检测到即梦登录态（sessionid cookie）。请点“登录即梦”在弹出的窗口中登录，再重新提交任务。"
                    JOB_STORE[job_id]["result"] = {
                        "status": "blocked",
                        "message": "未登录",
                        "site": SITE_URL,
                        "page_text": page_text[:300],
                    }
                    return

                _upload_image(page, image_path)
                if _job_is_cancelled(job_id):
                    return
                JOB_STORE[job_id]["progress"] = 50

                _fill_prompt(page, prompt)
                if _job_is_cancelled(job_id):
                    return
                JOB_STORE[job_id]["progress"] = 65
                page.wait_for_timeout(2000)

                initial_imgs = {
                    img.get_attribute("src") or ""
                    for img in page.locator("img").all()
                    if (img.get_attribute("src") or "").startswith("http")
                }
                try:
                    baseline_text = page.locator("body").inner_text(timeout=5000)
                except Exception:
                    baseline_text = ""
                baseline_markers = {marker for marker in WAIT_MARKERS if marker in baseline_text}
                _click_generate(page)
                if _job_is_cancelled(job_id):
                    return
                JOB_STORE[job_id]["progress"] = 80

                try:
                    saved_path = _wait_for_result(page, context, job_id, Path(DOWNLOAD_FOLDER), initial_imgs, baseline_markers)
                except _JobCancelled:
                    return
                except RuntimeError as exc:
                    JOB_STORE[job_id]["status"] = "blocked"
                    JOB_STORE[job_id]["progress"] = 100
                    JOB_STORE[job_id]["error"] = str(exc)
                    JOB_STORE[job_id]["result"] = {
                        "site": SITE_URL,
                        "images": [image_path],
                        "prompt": prompt,
                        "count": count,
                        "message": str(exc),
                        "history": JOB_STORE[job_id].get("history", []),
                    }
                    return

                if saved_path:
                    JOB_STORE[job_id]["status"] = "completed"
                    JOB_STORE[job_id]["progress"] = 100
                    JOB_STORE[job_id]["result"] = {
                        "status": "completed",
                        "download_path": saved_path,
                        "site": SITE_URL,
                        "images": [image_path],
                        "prompt": prompt,
                        "count": count,
                        "image_index": image_index,
                        "total_images": len(images),
                    }
                elif image_index < len(images) - 1:
                    JOB_STORE[job_id]["status"] = "switching"
                    JOB_STORE[job_id]["progress"] = 90
                    JOB_STORE[job_id]["result"] = {
                        "site": SITE_URL,
                        "images": [image_path],
                        "prompt": prompt,
                        "count": count,
                        "image_index": image_index,
                        "total_images": len(images),
                        "message": "第一张图片已生成完成，等待 2 秒后切换到下一张图片。",
                    }
                else:
                    JOB_STORE[job_id]["status"] = "waiting"
                    JOB_STORE[job_id]["progress"] = 90
                    JOB_STORE[job_id]["result"] = {
                        "site": SITE_URL,
                        "images": [image_path],
                        "prompt": prompt,
                        "count": count,
                        "image_index": image_index,
                        "total_images": len(images),
                        "message": "超时未检测到生成结果。可能是页面结构变化，可访问 /probe 查看，或到即梦网页确认积分/登录状态。",
                        "history": JOB_STORE[job_id].get("history", []),
                    }

                if image_index < len(images) - 1:
                    if _job_is_cancelled(job_id):
                        return
                    page.wait_for_timeout(2000)
                    page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                    continue

                return

    except Exception as exc:  # pragma: no cover
        if job_id not in JOB_STORE:
            return
        JOB_STORE[job_id]["status"] = "failed"
        JOB_STORE[job_id]["progress"] = 100
        JOB_STORE[job_id]["error"] = str(exc)


def _probe_page_structure() -> dict:
    if sync_playwright is None:
        return {"error": "Playwright is not installed"}
    result = {}
    tmp_profile = tempfile.mkdtemp(prefix="jimeng_probe_")

    def _run():
        try:
            with sync_playwright() as p:
                context = _launch_context(p, profile_dir=tmp_profile, headless=True)
                page = context.new_page()
                page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                body = page.locator("body").inner_text(timeout=15000)
                buttons = []
                for btn in page.locator("button").all():
                    try:
                        text = btn.inner_text(timeout=500).strip()
                    except Exception:
                        continue
                    if text:
                        buttons.append(text[:60])
                inputs = [
                    {"type": inp.get_attribute("type"), "accept": inp.get_attribute("accept")}
                    for inp in page.locator("input").all()
                ]
                textareas = [ta.get_attribute("placeholder") for ta in page.locator("textarea").all()]
                editables = [
                    ed.get_attribute("placeholder") or ed.get_attribute("data-placeholder")
                    for ed in page.locator("[contenteditable='true']").all()
                ]
                result.update(
                    {
                        "login_state": "logged_in" if _is_logged_in_text(body) else "not_logged_in",
                        "buttons": buttons,
                        "inputs": inputs,
                        "textareas": textareas,
                        "contenteditable": editables,
                    }
                )
                context.close()
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            shutil.rmtree(tmp_profile, ignore_errors=True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=90)
    return result or {"error": "probe timeout"}


@app.get("/")
def index():
    return render_template_string(
        """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>即梦 批量图生图控制台</title>
    <style>
      :root {
        --bg: #0b1020;
        --panel: #121b2e;
        --panel-strong: #192742;
        --primary: #6ea8fe;
        --primary-2: #8f7dff;
        --text: #edf4ff;
        --muted: #a8bad8;
        --ok: #54d39d;
        --warn: #f6c453;
        --err: #ff7b7b;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
        background: linear-gradient(180deg, #0b1020 0%, #0f1a2d 100%);
        color: var(--text);
      }
      .shell {
        display: grid;
        grid-template-columns: minmax(320px, 420px) 1fr;
        gap: 20px;
        min-height: 100vh;
        padding: 20px;
      }
      .panel {
        background: rgba(18, 27, 46, 0.94);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 18px;
        box-shadow: 0 20px 40px rgba(0,0,0,0.25);
        overflow: hidden;
      }
      .left {
        display: flex;
        flex-direction: column;
        padding: 20px;
      }
      .right {
        position: relative;
      }
      .title {
        font-size: 1.6rem;
        font-weight: 800;
        margin-bottom: 16px;
      }
      .muted {
        color: var(--muted);
        font-size: 0.92rem;
        margin-bottom: 18px;
      }
      form {
        display: grid;
        gap: 16px;
      }
      .field {
        display: grid;
        gap: 8px;
      }
      label {
        font-weight: 600;
      }
      input, textarea, select, button {
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.08);
      }
      input[type="file"], textarea, select {
        width: 100%;
        background: rgba(255,255,255,0.03);
        color: var(--text);
        padding: 12px 14px;
      }
      textarea {
        min-height: 100px;
        resize: vertical;
      }
      .row {
        display: flex;
        gap: 10px;
      }
      button {
        border: none;
        padding: 11px 16px;
        color: white;
        font-weight: 700;
        cursor: pointer;
        background: linear-gradient(135deg, var(--primary), var(--primary-2));
      }
      button.secondary {
        background: rgba(255,255,255,0.06);
      }
      .status-box {
        margin-top: 18px;
        padding: 14px;
        border-radius: 12px;
        background: rgba(255,255,255,0.02);
        border: 1px solid rgba(255,255,255,0.08);
      }
      .status-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        margin-bottom: 10px;
      }
      .badge {
        display: inline-block;
        padding: 6px 10px;
        border-radius: 999px;
        background: rgba(110, 168, 254, 0.16);
        font-size: 12px;
        font-weight: 700;
      }
      .progress {
        height: 10px;
        background: rgba(255,255,255,0.06);
        border-radius: 999px;
        overflow: hidden;
      }
      .progress-bar {
        width: 0%;
        height: 100%;
        background: linear-gradient(90deg, var(--ok), var(--primary));
        transition: width 0.25s ease;
      }
      .task-list {
        margin-top: 18px;
        display: grid;
        gap: 10px;
        overflow: auto;
      }
      .task-item {
        background: rgba(255,255,255,0.02);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        padding: 12px;
      }
      .task-item strong {
        display: block;
        margin-bottom: 6px;
      }
      .task-item small {
        color: var(--muted);
      }
      .task-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .cancel-btn {
        background: rgba(255, 123, 123, 0.14);
        color: #ffd8d8;
        border: 1px solid rgba(255,123,123,0.35);
        padding: 6px 10px;
        font-size: 11px;
        border-radius: 8px;
        cursor: pointer;
      }
      .download-links {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 8px;
      }
      .download-links a {
        color: var(--ok);
        text-decoration: none;
        font-size: 12px;
      }
      .download-links a:hover {
        text-decoration: underline;
      }
      @media (max-width: 1000px) {
        .shell {
          grid-template-columns: 1fr;
        }
        .right {
          min-height: 60vh;
        }
      }
    </style>
  </head>
  <body>
    <div class="shell">
      <div class="panel left">
        <div class="title">批量图生图</div>
        <div class="muted">上传多张图片，统一提示词；任务按提交顺序排队，在同一个浏览器窗口里依次生成。</div>

        <form id="task-form" enctype="multipart/form-data">
          <div class="field">
            <label for="images">选择图片（可多选）</label>
            <input id="images" name="images" type="file" accept="image/*" multiple>
          </div>

          <div class="field">
            <label for="prompt">提示词</label>
            <textarea id="prompt" name="prompt" placeholder="例如：保持人物、构图和风格一致，输出高质量插画风格图片，细节丰富，光影自然"></textarea>
          </div>

          <div class="row">
            <button type="submit">提交任务</button>
            <button type="button" class="secondary" id="login-btn">登录即梦</button>
            <button type="button" class="secondary" id="reset-btn">重置</button>
          </div>
        </form>

        <div class="status-box">
          <div class="status-row">
            <span class="badge" id="status-pill">等待任务</span>
            <span id="job-id-text">Job ID: 暂无</span>
          </div>
          <div class="progress"><div class="progress-bar" id="progress-bar"></div></div>
          <div style="margin-top:12px; color:#dfeeff; white-space:pre-wrap; font-size:12px;" id="status-output">任务状态会在这里显示。</div>
        </div>

        <div class="muted" style="margin-top:18px;">任务列表</div>
        <div class="task-list" id="task-list"></div>
      </div>

      <div class="panel right">
        <div style="height:100%; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; padding:24px; text-align:center;">
          <div style="font-size:1.1rem; font-weight:700;">即梦不允许嵌入到控制台页面</div>
          <div class="muted" style="margin-bottom:0;">自动化会在独立的浏览器窗口中操作即梦，不需要这个预览框。</div>
          <a href="https://jimeng.jianying.com/ai-tool/generate" target="_blank" rel="noopener" style="color:var(--primary); text-decoration:none; font-weight:700;">在新窗口打开即梦 →</a>
        </div>
      </div>
    </div>

    <script>
      const form = document.getElementById('task-form');
      const taskList = document.getElementById('task-list');
      const statusPill = document.getElementById('status-pill');
      const statusOutput = document.getElementById('status-output');
      const jobIdText = document.getElementById('job-id-text');
      const progressBar = document.getElementById('progress-bar');
      const resetBtn = document.getElementById('reset-btn');
      const loginBtn = document.getElementById('login-btn');
      let currentJobIds = [];
      let pollTimer = null;

      function startPolling() {
        if (pollTimer) return;
        pollTimer = setInterval(refreshJobs, 5000);
      }

      function stopPolling() {
        if (!pollTimer) return;
        clearInterval(pollTimer);
        pollTimer = null;
      }

      function renderTaskList(jobs) {
        if (!jobs || jobs.length === 0) {
          taskList.innerHTML = '<div class="task-item"><strong>暂无任务</strong><small>提交任务后会在这里显示。</small></div>';
          return;
        }
        taskList.innerHTML = jobs.slice().reverse().map(job => {
          const fileCount = Array.isArray(job.images) ? job.images.length : 0;
          const result = job.result || {};
          const downloadPath = result.download_path || '';
          const errorText = job.error ? ` ｜ 错误：${job.error}` : '';
          const fileName = downloadPath ? downloadPath.split('/').pop() : '';
          const fileLink = fileName ? `/files/${encodeURIComponent(fileName)}` : '';
          const downloadBlock = downloadPath ? `
            <div class="download-links">
              <span style="color: var(--ok); font-size:12px;">已下载</span>
              <a href="${fileLink}" target="_blank" rel="noopener">打开图片</a>
            </div>
            <div style="margin-top:6px; font-size:11px; color: var(--muted); word-break:break-all;">${downloadPath}</div>
          ` : '';
          return `
            <div class="task-item">
              <div class="task-head">
                <strong>${job.job_id || '未知任务'}</strong>
                <button class="cancel-btn" type="button" data-job-id="${job.job_id || ''}">删除任务</button>
              </div>
              <small>状态：${job.status || 'unknown'} ｜ 进度：${job.progress || 0}% ｜ 图片：${fileCount}${errorText}</small>
              <div style="margin-top:8px; color: var(--muted);">${(job.prompt || '').slice(0, 80)}</div>
              ${downloadBlock}
            </div>
          `;
        }).join('');
      }

      async function refreshJobs() {
        try {
          const res = await fetch('/jobs');
          const jobs = await res.json();
          renderTaskList(jobs);

          const activeJobs = (jobs || []).filter(job => {
            const status = String(job.status || '').toLowerCase();
            return !['completed', 'failed', 'cancelled', 'blocked'].includes(status);
          });

          if (activeJobs.length > 0) {
            startPolling();
          } else {
            stopPolling();
          }

          if (jobs.length) {
            const terminal = new Set(['completed', 'failed', 'cancelled', 'blocked']);
            const active = (jobs || []).find(job => !terminal.has(String(job.status || '').toLowerCase()));
            const last = active || jobs[jobs.length - 1];
            if (last.status) {
              statusPill.textContent = String(last.status).toUpperCase();
              progressBar.style.width = `${Number(last.progress || 0)}%`;
              if (last.result && last.result.download_path) {
                statusOutput.textContent = `下载完成\n${last.result.download_path}`;
              } else {
                statusOutput.textContent = JSON.stringify(last, null, 2);
              }
              jobIdText.textContent = `Job ID: ${last.job_id || '暂无'}`;
            }
          }
        } catch (err) {
          console.error(err);
        }
      }

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const fileInput = form.querySelector('input[name="images"]');
        const selectedFiles = fileInput ? Array.from(fileInput.files || []).filter(file => file && file.name) : [];
        const promptValue = (document.getElementById('prompt').value || '').trim();

        if (selectedFiles.length === 0) {
          statusPill.textContent = 'ERROR';
          progressBar.style.width = '100%';
          statusOutput.textContent = '提交失败: 至少上传一张图片';
          return;
        }
        if (!promptValue) {
          statusPill.textContent = 'ERROR';
          progressBar.style.width = '100%';
          statusOutput.textContent = '提交失败: 提示词不能为空';
          return;
        }

        const fd = new FormData(form);
        statusPill.textContent = 'SUBMITTING';
        progressBar.style.width = '10%';
        statusOutput.textContent = '正在提交任务...';

        try {
          const response = await fetch('/generate', { method: 'POST', body: fd });
          const data = await response.json();
          if (!response.ok) {
            throw new Error(data.error || '提交失败');
          }

          if (data.job_id) {
            currentJobIds = [data.job_id];
            jobIdText.textContent = `Job ID: ${data.job_id}`;
          } else if (data.job_ids) {
            currentJobIds = data.job_ids;
            jobIdText.textContent = `Job IDs: ${data.job_ids.join(', ')}`;
          }

          statusPill.textContent = 'QUEUED';
          progressBar.style.width = '20%';
          statusOutput.textContent = JSON.stringify(data, null, 2);
          startPolling();
          refreshJobs();
        } catch (error) {
          statusPill.textContent = 'ERROR';
          progressBar.style.width = '100%';
          statusOutput.textContent = `提交失败: ${error.message}`;
        }
      });

      loginBtn.addEventListener('click', async () => {
        statusPill.textContent = 'LOGIN';
        progressBar.style.width = '10%';
        statusOutput.textContent = '正在打开浏览器登录即梦…';
        try {
          const res = await fetch('/login', { method: 'POST' });
          const data = await res.json();
          statusOutput.textContent = data.message || JSON.stringify(data, null, 2);
        } catch (err) {
          statusOutput.textContent = `登录窗口打开失败: ${err.message}`;
        }
      });

      resetBtn.addEventListener('click', () => {
        form.reset();
        stopPolling();
        statusPill.textContent = '等待任务';
        jobIdText.textContent = 'Job ID: 暂无';
        progressBar.style.width = '0%';
        statusOutput.textContent = '任务状态会在这里显示。';
      });

      taskList.addEventListener('click', async (event) => {
        const btn = event.target.closest('.cancel-btn');
        if (!btn) return;
        const jobId = btn.dataset.jobId;
        if (!jobId) return;

        const confirmed = window.confirm('确认删除这个卡住的任务并终止后台进程？');
        if (!confirmed) return;

        try {
          const res = await fetch(`/jobs/${jobId}/cancel`, { method: 'POST' });
          const data = await res.json();
          statusPill.textContent = 'CANCELLED';
          progressBar.style.width = '100%';
          statusOutput.textContent = data.message || '任务已取消';
          refreshJobs();
        } catch (err) {
          statusPill.textContent = 'ERROR';
          progressBar.style.width = '100%';
          statusOutput.textContent = '取消任务失败';
        }
      });

      refreshJobs();
    </script>
  </body>
</html>
        """
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "site": SITE_URL})


@app.get("/jobs")
def jobs():
    items = []
    for job_id, job in JOB_STORE.items():
        items.append(_job_summary(job))
    return jsonify(items)


@app.get("/files/<path:filename>")
def download_file(filename: str):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=False)


@app.post("/generate")
def generate():
    payload = request.get_json(silent=True) or {}
    uploaded_images = request.files.getlist("images")

    try:
        if uploaded_images:
            image_paths = []
            for uploaded in uploaded_images:
                if uploaded and uploaded.filename:
                    filename = secure_filename(uploaded.filename)
                    save_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_{filename}")
                    uploaded.save(save_path)
                    image_paths.append(save_path)
            images = image_paths
            prompt = request.form.get("prompt") or payload.get("prompt") or payload.get("description") or ""
            count = 1
        else:
            images = payload.get("images") or payload.get("image") or []
            prompt = payload.get("prompt") or payload.get("description") or ""
            count = 1

        _validate_request_inputs(images, prompt, count)
        image_paths = _normalise_images(images)

        if len(image_paths) == 1:
            job_id = _create_job(image_paths[0], prompt.strip(), 1)
            return jsonify({"job_id": job_id, "status": "queued"})

        job_ids = [_create_job(path, prompt.strip(), 1) for path in image_paths]
        return jsonify({"job_ids": job_ids, "status": "queued", "count": len(job_ids)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": str(exc)}), 500


@app.get("/status/<job_id>")
def status(job_id: str):
    job = JOB_STORE.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify(_job_summary(job))


@app.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    if _cancel_job(job_id):
        return jsonify({"job_id": job_id, "status": "cancelled", "message": "任务已被手动终止"})
    return jsonify({"error": "job not found"}), 404


@app.post("/login")
def login():
    if sync_playwright is None:
        return jsonify({"error": "Playwright is not installed"}), 500
    with JOB_QUEUE_LOCK:
        JOB_QUEUE.appendleft("login")
        JOB_QUEUE_COND.notify()
    _ensure_job_queue_worker()
    return jsonify({"ok": True, "message": "已把“打开登录窗口”排到队列最前，稍候会弹出即梦浏览器窗口。"})


@app.get("/probe")
def probe():
    return jsonify(_probe_page_structure())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    app.run(host="127.0.0.1", port=port, debug=False)

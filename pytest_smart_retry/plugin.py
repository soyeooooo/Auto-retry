"""
AI 로그 분석과 Slack/Teams 알림을 선택적으로 지원하는 pytest 자동 재시도 플러그인.

사용 방법:
1. pip install pytest-smart-retry
2. conftest.py 없이도 자동 등록됩니다 (pytest11 entry point).
   직접 등록하려면: pytest_plugins = ['pytest_smart_retry']

동작 방식:
- @pytest.mark.auto_retry 가 붙은 테스트만 이 플러그인의 대상입니다.
- Timeout / Network 계열 실패는 RETRY_MAX 만큼 재시도합니다.
- Assertion 및 기타 일반 실패는 즉시 최종 실패로 처리합니다.
- 최종 실패 시 AI 분석 요약과 Slack/Teams 알림을 선택적으로 보낼 수 있습니다.

환경 변수:
- AI_LOG_ANALYZER_ENABLED=true
- AI_LOG_ANALYZER_PROVIDER=ollama | openai
- OLLAMA_MODEL=llama3
- OLLAMA_ENDPOINT=http://localhost:11434/api/generate
- OPENAI_API_KEY=...
- OPENAI_MODEL=gpt-4o-mini
- SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
- SLACK_NOTIFY_ON_FINAL_FAILURE=true
- TEAMS_WEBHOOK_URL=https://prod-xx.logic.azure.com/workflows/...
- TEAMS_NOTIFY_ON_FINAL_FAILURE=true

pytest.ini 옵션:
- auto_retry_max = 2                      (재시도 횟수, 기본값 2)
- auto_retry_driver_fixture = set_driver  (드라이버 fixture 이름, 기본값 set_driver)
- auto_retry_frame_pattern = ...          (스택 필터 정규식, 기본값 없음)
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib import error, request

import pytest


AI_LOG_ANALYZER_DEFAULT_TIMEOUT  = 120
AI_LOG_ANALYZER_DEFAULT_MAX_CHARS = 12000

TIMEOUT_KEYWORDS = (
    "timeoutexception",
    "timeout",
    "timed out",
)

NETWORK_KEYWORDS = (
    "network",
    "connection refused",
    "unreachable",
    "no such window",
    "web view not found",
    "err_name_not_resolved",
)


# ── pytest 설정 ──────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    group = parser.getgroup("auto_retry")
    group.addoption("--auto-retry-max", type=int, default=None,
                    help="재시도 횟수 (기본값: auto_retry_max ini 옵션 또는 2)")

def pytest_sessionstart(session):
    output_dir = _ai_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "ai_analysis.txt"
    if report_path.exists():
        report_path.unlink()


def pytest_sessionfinish(session, exitstatus):
    report_path = _ai_output_dir() / "ai_analysis.txt"
    if not report_path.exists():
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("전부 pass입니다 :)!\n")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "auto_retry: retry Timeout/Network style failures up to auto_retry_max times",
    )
    config.addinivalue_line("ini_options", "auto_retry_max (int): 재시도 횟수 (기본값 2)")
    config.addinivalue_line("ini_options", "auto_retry_driver_fixture (str): 드라이버 fixture 이름")
    config.addinivalue_line("ini_options", "auto_retry_frame_pattern (str): 스택 필터 정규식")


def _retry_max(config=None) -> int:
    if config:
        cli = config.getoption("--auto-retry-max", default=None)
        if cli is not None:
            return cli
        ini = config.getini("auto_retry_max") if "auto_retry_max" in config._inicache else None
        if ini:
            return int(ini)
    return int(os.getenv("AUTO_RETRY_MAX", "2"))


def _driver_fixture_name(config=None) -> str:
    if config:
        ini = config.getini("auto_retry_driver_fixture") if "auto_retry_driver_fixture" in config._inicache else None
        if ini:
            return str(ini)
    return os.getenv("AUTO_RETRY_DRIVER_FIXTURE", "set_driver")


def _frame_pattern(config=None) -> str:
    if config:
        ini = config.getini("auto_retry_frame_pattern") if "auto_retry_frame_pattern" in config._inicache else None
        if ini:
            return str(ini)
    return os.getenv("AUTO_RETRY_FRAME_PATTERN", "")


# ── 오류 분류 ────────────────────────────────────────────────────────────────

def _error_type(longrepr: str) -> str:
    text = longrepr.lower()
    if any(k in text for k in TIMEOUT_KEYWORDS):
        return "Timeout"
    if any(k in text for k in NETWORK_KEYWORDS):
        return "Network"
    return ""


def _classification_text(report) -> str:
    longrepr  = getattr(report, "longrepr", None)
    reprcrash = getattr(longrepr, "reprcrash", None)
    message   = getattr(reprcrash, "message", "")
    if message:
        return str(message)
    return str(longrepr or "")


def _final_error_type(failure_text: str, classification_text: str = "") -> str:
    retry_type = _error_type(classification_text or failure_text)
    if retry_type:
        return retry_type
    text = failure_text.lower()
    if "assertionerror" in text or "assert " in text:
        return "Assertion"
    if "error" in text:
        return "Error"
    return "Failure"


# ── 드라이버 리셋 ────────────────────────────────────────────────────────────

def _reset_driver(item) -> None:
    funcargs    = getattr(item, "funcargs", None) or {}
    fixture_name = _driver_fixture_name(item.config)
    driver = funcargs.get(fixture_name) or funcargs.get("driver")
    if not driver:
        return
    try:
        driver.get("about:blank")
    except Exception:
        pass


# ── AI 설정 ──────────────────────────────────────────────────────────────────

def _ai_provider() -> str:
    return os.getenv("AI_LOG_ANALYZER_PROVIDER", "ollama").strip().lower()

def _ai_model() -> str:
    if _ai_provider() == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    return os.getenv("OLLAMA_MODEL", "llama3").strip()

def _ai_timeout() -> int:
    raw = os.getenv("AI_LOG_ANALYZER_TIMEOUT", str(AI_LOG_ANALYZER_DEFAULT_TIMEOUT)).strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return AI_LOG_ANALYZER_DEFAULT_TIMEOUT

def _ai_max_chars() -> int:
    raw = os.getenv("AI_LOG_ANALYZER_MAX_CHARS", str(AI_LOG_ANALYZER_DEFAULT_MAX_CHARS)).strip()
    try:
        return max(1000, int(raw))
    except ValueError:
        return AI_LOG_ANALYZER_DEFAULT_MAX_CHARS

def _ai_output_dir() -> Path:
    raw = os.getenv("AI_LOG_ANALYZER_OUTPUT_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path("Report") / "ai_analysis"


# ── Slack ────────────────────────────────────────────────────────────────────

def _slack_webhook_url() -> str:
    return os.getenv("SLACK_WEBHOOK_URL", "").strip()

def _slack_notify_enabled() -> bool:
    raw = os.getenv("SLACK_NOTIFY_ON_FINAL_FAILURE", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(_slack_webhook_url())

def _slack_excerpt(text: str, limit: int = 800) -> str:
    lines   = [" ".join(line.split()) for line in (text or "").splitlines()]
    cleaned = re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."

def _post_to_slack(message: str) -> None:
    webhook_url = _slack_webhook_url()
    if not webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL is not set")
    body = json.dumps({"text": message}).encode("utf-8")
    req  = request.Request(url=webhook_url, data=body,
                           headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=_ai_timeout()) as resp:
            resp.read()
    except error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Connection error: {exc.reason}") from exc

def _notify_slack_final_failure(test_name, error_type, failure_text, analysis="", report_path=""):
    if not _slack_notify_enabled():
        return
    lines = [
        ":rotating_light: auto_retry final failure",
        f"- Test: `{test_name}`",
        f"- Error Type: `{error_type or 'Unknown'}`",
    ]
    if report_path:
        lines.append(f"- Report: `{report_path}`")
    if analysis:
        lines.append(f"- AI Summary: {_slack_excerpt(analysis)}")
    lines.append(f"- Failure Excerpt: `{_slack_excerpt(failure_text)}`")
    try:
        _post_to_slack("\n".join(lines))
    except Exception as exc:
        logging.error(f"[slack notify failed] {test_name}: {exc}")


# ── Teams ────────────────────────────────────────────────────────────────────

def _teams_webhook_url() -> str:
    return os.getenv("TEAMS_WEBHOOK_URL", "").strip()

def _teams_notify_enabled() -> bool:
    raw = os.getenv("TEAMS_NOTIFY_ON_FINAL_FAILURE", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(_teams_webhook_url())

def _strip_markdown(text: str) -> str:
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'[Ѐ-ӿ]+', '', text)
    text = re.sub(r'^(\d+\.\s+\S[^\n]*)', r'**\1**', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _code_block(code: str) -> dict:
    """모노스페이스 + 회색 배경 컨테이너"""
    safe_lines = [
        re.sub(r'^(\s*)([#>\-\*\+])', r'\1\\\2', line)
        for line in code.strip().splitlines()
    ]
    return {
        "type": "Container",
        "style": "emphasis",
        "items": [{
            "type": "TextBlock",
            "text": "\n".join(safe_lines),
            "wrap": True,
            "size": "Small",
            "fontType": "Monospace",
        }],
    }


def _build_analysis_blocks(analysis: str, limit: int = 1200) -> list:
    text = _slack_excerpt(analysis, limit=limit)
    text = re.sub(r'[Ѐ-ӿ]+', '', text)
    text = re.sub(r'Tests?/[^\s:]+(?:::[^\s:]+)?:?\s*', '', text)
    text = re.sub(r'^\s*\d+:\s*(Failed|Error)[^\n]*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*E\s+(Failed|Error)[^\n]*', '', text, flags=re.MULTILINE)

    code_store = {}
    def stash_code(m):
        key = f"\x00CODE{len(code_store)}\x00"
        code_store[key] = m.group(1).strip()
        return key
    text = re.sub(r'```(?:\w+)?\n?([\s\S]*?)```', stash_code, text)

    SECTION_HEADERS = r'재시도|가능성|수정\s*권장|기존\s*코드|수정\s*코드'
    code_label_pattern = re.compile(
        r'(기존\s*코드|수정\s*코드)\s*:\s*\n?([\s\S]+?)'
        r'(?=\n\s*(?:' + SECTION_HEADERS + r')\s*[:\n]|\n\s*\d+\.\s|\Z)',
        re.IGNORECASE,
    )
    label_map = {}
    def stash_label(m):
        key = f"\x00LABEL{len(label_map)}\x00"
        label = "📄 기존코드" if '기존' in m.group(1) else "✅ 수정코드"
        code = m.group(2).strip()
        for k, v in code_store.items():
            code = code.replace(k, v)
        label_map[key] = (label, code)
        return key
    text = code_label_pattern.sub(stash_label, text)

    blocks = []
    tokens = re.split(r'(\x00(?:CODE|LABEL)\d+\x00)', text)
    pending = {}

    def flush_pair():
        if not pending:
            return []
        result = []
        if "old" in pending:
            result.append({"type": "TextBlock", "text": "📄 기존코드", "weight": "Bolder", "size": "Small"})
            result.append(_code_block(pending["old"]))
        if "new" in pending:
            if "old" in pending:
                result.append({"type": "TextBlock", "text": "↓", "horizontalAlignment": "Center", "size": "Medium", "color": "Accent"})
            result.append({"type": "TextBlock", "text": "✅ 수정코드", "weight": "Bolder", "size": "Small"})
            result.append(_code_block(pending["new"]))
        pending.clear()
        return result

    for token in tokens:
        if token in code_store:
            blocks.extend(flush_pair())
            blocks.append(_code_block(code_store[token]))
        elif token in label_map:
            label, code = label_map[token]
            if label == "📄 기존코드":
                pending["old"] = code
            else:
                pending["new"] = code
            if "old" in pending and "new" in pending:
                blocks.extend(flush_pair())
        else:
            cleaned = _strip_markdown(token)
            if cleaned.strip():
                blocks.extend(flush_pair())
                blocks.append({"type": "TextBlock", "text": cleaned, "wrap": True, "size": "Small"})

    blocks.extend(flush_pair())
    return blocks


def _notify_teams_final_failure(test_name, error_type, failure_text, analysis="", report_path=""):
    if not _teams_notify_enabled():
        return

    body = [
        {"type": "TextBlock", "text": "🚨 auto_retry Final Failure",
         "size": "Large", "weight": "Bolder", "color": "Attention"},
        {"type": "FactSet", "facts": [
            {"title": "Test", "value": test_name},
            {"title": "Error Type", "value": error_type or "Unknown"},
            {"title": "Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        ]},
    ]
    if analysis:
        body.append({"type": "TextBlock", "text": "─" * 30, "color": "Default"})
        body.append({"type": "TextBlock", "text": "🤖 AI 분석", "weight": "Bolder", "size": "Medium"})
        body.extend(_build_analysis_blocks(analysis, limit=1200))

    webhook_url = _teams_webhook_url()
    if not webhook_url:
        return

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.2",
                "fallbackText": f"🚨 {test_name} - {error_type}",
                "body": body,
            },
        }],
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    req = request.Request(url=webhook_url, data=body_bytes,
                          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=_ai_timeout()) as resp:
            resp.read()
    except Exception as exc:
        logging.error(f"[teams notify failed] {test_name}: {exc}")


# ── AI 분석 ──────────────────────────────────────────────────────────────────

def _extract_failure_context(failure_text: str, frame_pattern: str = "") -> dict:
    text   = failure_text or ""
    frames = re.findall(frame_pattern, text) if frame_pattern else []
    locator_match   = re.search(r"locator=(\([^\n]+\))", text)
    exception_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*Exception): Message: ([^\n]+)", text)
    return {
        "frames": frames[:8],
        "top_frame": frames[0] if frames else "",
        "last_project_frame": frames[-1] if frames else "",
        "caller_frame": frames[-1] if frames else "",
        "locator": locator_match.group(1) if locator_match else "",
        "exception": (
            f"{exception_match.group(1)}: {exception_match.group(2).strip()}"
            if exception_match else ""
        ),
    }


def _build_ai_prompt(test_name: str, error_type: str, failure_text: str,
                     frame_pattern: str = "") -> str:
    context      = _extract_failure_context(failure_text, frame_pattern)
    context_lines = []
    if context["exception"]:
        context_lines.append(f"- 추출 예외: {context['exception']}")
    if context["top_frame"]:
        context_lines.append(f"- 최초 프로젝트 스택: {context['top_frame']}")
    if context["last_project_frame"]:
        context_lines.append(f"- 마지막 프로젝트 스택: {context['last_project_frame']}")
    if context["caller_frame"] and context["caller_frame"] != context["last_project_frame"]:
        context_lines.append(f"- 실제 호출부: {context['caller_frame']}")
    if context["locator"]:
        context_lines.append(f"- 추출 locator: {context['locator']}")

    return (
        "You are a QA automation log analyzer.\n"
        "Read the pytest failure log and respond ONLY in Korean (한국어).\n"
        "STRICTLY forbidden: Russian, Chinese, Japanese, or any non-Korean language.\n"
        "Do not use English except for code, file paths, test names, error class names, or quoted log text.\n"
        "Write concisely and practically.\n\n"
        f"테스트 이름: {test_name}\n"
        f"감지된 실패 오류 유형: {error_type or 'Unknown'}\n\n"
        "아래 3개 섹션만 정확한 순서와 제목으로 작성하세요.\n"
        "1. 재시도 적합성\n"
        "2. 가능성 높은 원인\n"
        "3. 수정 권장 사항\n\n"
        "각 섹션은 명확하고 실용적으로 작성하세요.\n"
        "'수정 권장 사항'은 가장 가능성 높은 수정 방법 1개만 작성하세요.\n"
        "파일 경로와 함수명을 직접 인용하고, 수정 방법을 실제 코드 스니펫으로 제시하세요.\n"
        "코드를 작성할 때는 반드시 기존코드와 수정코드를 아래 형식으로 작성하세요:\n"
        "기존코드:\n```\n(기존 코드)\n```\n수정코드:\n```\n(수정된 코드)\n```\n"
        "다음 수정은 절대 제안하지 마세요:\n"
        "- timeout 값만 늘리는 수정\n"
        "- AssertionError나 예외를 catch해서 logging.warning으로 대체하는 수정\n"
        "- 공통 함수 내부에 특정 테스트용 값을 하드코딩하는 수정\n"
        "각 항목 사이에는 반드시 빈 줄을 하나 추가하세요.\n\n"
        f"추출 컨텍스트:\n{chr(10).join(context_lines) if context_lines else '- 없음'}\n\n"
        "실패 로그:\n"
        f"{failure_text}"
    )


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req  = request.Request(url=url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=_ai_timeout()) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Connection error: {exc.reason}") from exc


def _call_ollama(prompt: str) -> str:
    endpoint = os.getenv("OLLAMA_ENDPOINT", "http://localhost:11434/api/generate").strip()
    data = _post_json(
        url=endpoint,
        payload={"model": _ai_model(), "prompt": prompt, "stream": False},
        headers={"Content-Type": "application/json"},
    )
    return (data.get("response") or "").strip()


def _call_openai(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    data = _post_json(
        url=f"{base_url}/chat/completions",
        payload={
            "model": _ai_model(),
            "messages": [
                {"role": "system", "content": "You analyze QA automation logs."},
                {"role": "user",   "content": prompt},
            ],
            "temperature": 0.2,
        },
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message", {}).get("content") or "").strip()


def analyze_test_failure(test_name: str, error_type: str, failure_text: str,
                         frame_pattern: str = "") -> str:
    trimmed = (failure_text or "").strip()
    if not trimmed:
        return ""
    trimmed = trimmed[-_ai_max_chars():]
    prompt  = _build_ai_prompt(test_name, error_type, trimmed, frame_pattern)
    try:
        if _ai_provider() == "openai":
            return _call_openai(prompt)
        return _call_ollama(prompt)
    except Exception as exc:
        return f"AI analysis failed: {exc}"


def save_analysis_report(test_name: str, error_type: str,
                         failure_text: str, analysis: str) -> str:
    output_dir = _ai_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    path      = output_dir / "ai_analysis.txt"
    formatted = re.sub(r'(?m)(?<!\n)\n(\d+\.\s)', r'\n\n\1', analysis.strip())
    body = (
        "AI Failure Analysis\n"
        "===================\n\n"
        f"Test: {test_name}\n"
        f"Error Type: {error_type or 'Unknown'}\n"
        f"Generated At: {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"Analysis\n"
        f"--------\n\n{formatted or 'No analysis returned.'}\n\n"
        f"Failure Excerpt\n"
        f"---------------\n\n{failure_text[-_ai_max_chars():]}\n"
        "\n" + "=" * 50 + "\n\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(body)
    return str(path)


def _inject_ai_extras(report, analysis: str, error_type: str) -> None:
    try:
        from pytest_html import extras as html_extras
    except ImportError:
        return
    if not hasattr(report, "extras") or report.extras is None:
        report.extras = []
    escaped = analysis.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    report.extras.append(html_extras.html(
        f'<details style="margin-top:6px">'
        f'<summary style="cursor:pointer;font-weight:bold;color:#2c5fa0">'
        f'🤖 AI Analysis'
        f'<span style="background:#4a90e2;color:#fff;font-size:11px;'
        f'padding:1px 6px;border-radius:10px;margin-left:6px">'
        f'{error_type}</span></summary>'
        f'<pre style="white-space:pre-wrap;font-size:12px;'
        f'background:#eef4ff;padding:10px;border-left:4px solid #4a90e2;'
        f'border-radius:3px;margin-top:4px">{escaped}</pre>'
        f'</details>'
    ))


# ── 최종 실패 처리 ────────────────────────────────────────────────────────────

def _on_final_failure(item, longrepr: str, error_type: str,
                      classification_text: str = "", call_report=None) -> None:
    frame_pattern    = _frame_pattern(item.config)
    final_error_type = error_type or _final_error_type(longrepr, classification_text)

    analysis = analyze_test_failure(
        test_name=item.nodeid,
        error_type=final_error_type,
        failure_text=longrepr,
        frame_pattern=frame_pattern,
    )
    if analysis:
        logging.info(f"[AI analysis] {item.nodeid} → HTML 리포트에 주입")

    item._ai_analysis_text    = analysis or ""
    item._ai_error_type_label = final_error_type or "Unknown"

    if analysis and call_report is not None:
        _inject_ai_extras(call_report, analysis, final_error_type)

    report_path = ""
    if analysis:
        report_path = save_analysis_report(
            test_name=item.nodeid,
            error_type=final_error_type,
            failure_text=longrepr,
            analysis=analysis,
        )

    _notify_slack_final_failure(item.nodeid, final_error_type, longrepr, analysis, report_path)
    _notify_teams_final_failure(item.nodeid, final_error_type, longrepr, analysis, report_path)


# ── pytest hook ──────────────────────────────────────────────────────────────

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    if not item.get_closest_marker("auto_retry"):
        return None

    retry_max = _retry_max(item.config)

    for attempt in range(retry_max + 1):
        is_last_attempt = attempt == retry_max
        reports         = runtestprotocol(item, nextitem=nextitem, log=False)
        call_report     = next((r for r in reports if r.when == "call"), None)
        failed_report   = next((r for r in reports if r.failed), None)
        succeeded       = failed_report is None
        active_report   = call_report or failed_report
        error_type      = "" if succeeded else _error_type(_classification_text(active_report))
        should_retry    = bool(error_type) and not is_last_attempt

        if not should_retry:
            if not succeeded:
                _on_final_failure(
                    item,
                    str(active_report.longrepr or ""),
                    error_type,
                    _classification_text(active_report),
                    call_report=active_report,
                )

            for report in reports:
                item.ihook.pytest_runtest_logreport(report=report)

            if succeeded and attempt > 0:
                logging.info(f"[retry success] {item.nodeid} ({attempt + 1}th attempt)")
            return True

        logging.warning(
            f"[auto retry {attempt + 1}/{retry_max}] "
            f"{item.nodeid} -> {error_type}, retrying attempt {attempt + 2}"
        )
        _reset_driver(item)

    return True

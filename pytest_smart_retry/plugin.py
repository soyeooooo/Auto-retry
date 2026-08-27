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
- auto_retry_utility_files = a.py,b.py    (AI 분석 시 호출부에서 제외할 공통 유틸 파일명, 콤마 구분, 기본값 없음)

Jira 이슈 등록 (선택):
- JIRA_BASE_URL=https://your-domain.atlassian.net
- JIRA_EMAIL=you@example.com
- JIRA_API_TOKEN=...                      (https://id.atlassian.com/manage-profile/security/api-tokens 에서 발급)
- JIRA_PROJECT_KEY=QAT                    (기본값 QAT)
- JIRA_ISSUE_TYPE=Bug                     (기본값 Bug)
- JIRA_NOTIFY_ON_FINAL_FAILURE=true
- JIRA_EXTRA_FIELDS={"customfield_10771":["automation"]}  (프로젝트별 필수 커스텀 필드, JSON)
- AUTO_RETRY_ENV_NAME=QuickBuild           (CI/실행 환경 이름. 설정 안 하면 로컬 실행으로 간주해
                                            Jira 이슈를 만들지 않음 — CI에서만 반드시 설정)
  최종 실패 시 QA Kanban 보드에 이슈를 생성합니다. 동일 테스트의 열린 이슈가 있으면
  코멘트만 추가하고, 없으면 신규 생성합니다.
  이슈 제목 형식: [smartRetry][{AUTO_RETRY_ENV_NAME}] {실패 메시지 첫 줄}

NAS 백업 (선택):
- NAS_REPORT_ROOT=\\\\nas\\reports        (스크린샷을 복사할 경로. 설정 시에만 동작)
- NAS_REPORT_ENABLED=true
"""

import base64
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
    parser.addini("auto_retry_max", help="재시도 횟수 (기본값 2)", default=None)
    parser.addini("auto_retry_driver_fixture", help="드라이버 fixture 이름", default=None)
    parser.addini("auto_retry_frame_pattern", help="스택 필터 정규식", default=None)
    parser.addini("auto_retry_utility_files",
                  help="AI 분석 시 호출부에서 제외할 공통 유틸 파일명 (콤마 구분)", default=None)

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


def _retry_max(config=None) -> int:
    if config:
        cli = config.getoption("--auto-retry-max", default=None)
        if cli is not None:
            return cli
        ini = config.getini("auto_retry_max")
        if ini:
            return int(ini)
    return int(os.getenv("AUTO_RETRY_MAX", "2"))


def _driver_fixture_name(config=None) -> str:
    if config:
        ini = config.getini("auto_retry_driver_fixture")
        if ini:
            return str(ini)
    return os.getenv("AUTO_RETRY_DRIVER_FIXTURE", "set_driver")


def _frame_pattern(config=None) -> str:
    if config:
        ini = config.getini("auto_retry_frame_pattern")
        if ini:
            return str(ini)
    return os.getenv("AUTO_RETRY_FRAME_PATTERN", "")


def _utility_files(config=None) -> tuple:
    raw = ""
    if config:
        ini = config.getini("auto_retry_utility_files")
        if ini:
            raw = str(ini)
    if not raw:
        raw = os.getenv("AUTO_RETRY_UTILITY_FILES", "")
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def _project_root(config=None) -> Path:
    if config is not None:
        rootpath = getattr(config, "rootpath", None)
        if rootpath:
            return Path(rootpath)
        rootdir = getattr(config, "rootdir", None)
        if rootdir:
            return Path(str(rootdir))
    return Path.cwd()


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

def _excerpt(text: str, limit: int = 800) -> str:
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

def _notify_slack_final_failure(test_name, error_type, failure_text, analysis="", report_path="", jira_url=""):
    if not _slack_notify_enabled():
        return
    lines = [
        ":rotating_light: auto_retry final failure",
        f"- Test: `{test_name}`",
        f"- Error Type: `{error_type or 'Unknown'}`",
    ]
    if report_path:
        lines.append(f"- Report: `{report_path}`")
    if jira_url:
        lines.append(f"- Jira: {jira_url}")
    if analysis:
        lines.append(f"- AI Summary: {_excerpt(analysis)}")
    lines.append(f"- Failure Excerpt: `{_excerpt(failure_text)}`")
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
    text = _excerpt(analysis, limit=limit)
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


def _notify_teams_final_failure(test_name, error_type, failure_text, analysis="",
                                screenshot_path="", jira_key="", jira_url=""):
    if not _teams_notify_enabled():
        return

    facts = [
        {"title": "Test", "value": test_name},
        {"title": "Error Type", "value": error_type or "Unknown"},
        {"title": "Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    ]
    if jira_key:
        facts.append({"title": "Jira Issue", "value": f"[{jira_key}]({jira_url})" if jira_url else jira_key})
    if screenshot_path:
        # Adaptive Card는 value를 마크다운으로 렌더링해서 "\\"(UNC 접두사)가
        # 이스케이프로 먹혀 "\" 하나로 뭉개짐 → 백슬래시를 두 배로 escape해서 방지
        facts.append({"title": "Screenshot", "value": screenshot_path.replace("\\", "\\\\")})

    body = [
        {"type": "TextBlock", "text": "🚨 auto_retry Final Failure",
         "size": "Large", "weight": "Bolder", "color": "Attention"},
        {"type": "FactSet", "facts": facts},
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


# ── Jira ─────────────────────────────────────────────────────────────────────

def _jira_base_url() -> str:
    return os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")

def _jira_email() -> str:
    return os.getenv("JIRA_EMAIL", "").strip()

def _jira_api_token() -> str:
    return os.getenv("JIRA_API_TOKEN", "").strip()

def _jira_project_key() -> str:
    return os.getenv("JIRA_PROJECT_KEY", "QAT").strip()

def _jira_issue_type() -> str:
    return os.getenv("JIRA_ISSUE_TYPE", "Bug").strip()

def _jira_extra_fields() -> dict:
    raw = os.getenv("JIRA_EXTRA_FIELDS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logging.warning("[jira] JIRA_EXTRA_FIELDS is not valid JSON, ignoring")
        return {}
    return data if isinstance(data, dict) else {}

def _jira_env_name() -> str:
    return os.getenv("AUTO_RETRY_ENV_NAME", "").strip()

def _jira_notify_enabled() -> bool:
    raw = os.getenv("JIRA_NOTIFY_ON_FINAL_FAILURE", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if not _jira_env_name():
        # AUTO_RETRY_ENV_NAME이 없으면 로컬 실행으로 간주하고 Jira 이슈를 만들지 않는다.
        return False
    return bool(_jira_base_url() and _jira_email() and _jira_api_token())

def _jira_test_label(test_name: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9_-]+', '-', test_name).strip('-').lower()
    return f"autoretry-{slug}"[:180]

def _jira_summary_line(classification_text: str, failure_text: str, limit: int = 200) -> str:
    text = (classification_text or failure_text or "").strip()
    first_line = " ".join(text.splitlines()[0].split()) if text else ""
    if len(first_line) <= limit:
        return first_line
    return first_line[: limit - 3] + "..."

def _jira_issue_summary(test_name: str, classification_text: str, failure_text: str) -> str:
    env = _jira_env_name() or "로컬"
    line = _jira_summary_line(classification_text, failure_text) or test_name
    return f"[smartRetry][{env}] {line}"[:255]

def _jira_auth_header() -> str:
    token = base64.b64encode(f"{_jira_email()}:{_jira_api_token()}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"

def _jira_request(method: str, url: str, payload: dict = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url=url, data=body, method=method,
        headers={"Content-Type": "application/json", "Authorization": _jira_auth_header()},
    )
    try:
        with request.urlopen(req, timeout=_ai_timeout()) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Connection error: {exc.reason}") from exc

def _adf_paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}] if text else []}

def _adf_codeblock(code: str) -> dict:
    return {"type": "codeBlock", "attrs": {}, "content": [{"type": "text", "text": code}]}

def _text_to_adf(text: str) -> dict:
    content = []
    segments = re.split(r'```(?:\w+)?\n?([\s\S]*?)```', text)
    for i, segment in enumerate(segments):
        if i % 2 == 1:
            if segment.strip():
                content.append(_adf_codeblock(segment.strip()))
            continue
        for para in segment.strip("\n").split("\n\n"):
            para = para.strip()
            if para:
                content.append(_adf_paragraph(para))
    if not content:
        content = [_adf_paragraph(text or "")]
    return {"type": "doc", "version": 1, "content": content}

def _split_jira_recommendation(analysis: str) -> tuple:
    """AI 분석에서 '수정 권장 사항' 섹션만 분리 (댓글로 따로 달기 위함).

    모델마다 헤더 표기가 "3. 수정 권장 사항", "**수정 권장 사항**", "### 수정 권장 사항" 등으로
    제각각이라 번호/마크다운 기호를 모두 선택적으로 허용한다.
    """
    if not analysis:
        return "", ""
    pattern = r'\n\s*(?:#{1,6}\s*)?(?:\*\*)?(?:\d+[.\)]\s*)?수정\s*권장\s*사항(?:\*\*)?\s*:?\s*\n?'
    match = re.search(pattern, analysis)
    if not match:
        return analysis.strip(), ""
    return analysis[:match.start()].strip(), analysis[match.start():].strip()

def _build_jira_description(test_name: str, error_type: str, failure_text: str, analysis: str) -> dict:
    main_analysis, _ = _split_jira_recommendation(analysis)
    header = (
        f"Test: {test_name}\n"
        f"Error Type: {error_type or 'Unknown'}\n"
        f"Generated At: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    excerpt = _excerpt(failure_text, limit=3000)
    body_text = f"{header}\n\nAI 분석\n{main_analysis or '(분석 없음)'}\n\n실패 로그\n```\n{excerpt}\n```"
    return _text_to_adf(body_text)

def _jira_add_recommendation_comment(issue_key: str, fix_text: str) -> None:
    url = f"{_jira_base_url()}/rest/api/3/issue/{issue_key}/comment"
    body_text = f"💡 참고용 (AI 수정 권장 사항)\n\n{fix_text}"
    _jira_request("POST", url, {"body": _text_to_adf(body_text)})

def _jira_find_open_issue(label: str) -> str:
    url = f"{_jira_base_url()}/rest/api/3/search/jql"
    jql = f'project = "{_jira_project_key()}" AND labels = "{label}" AND statusCategory != Done ORDER BY created DESC'
    data = _jira_request("POST", url, {"jql": jql, "maxResults": 1, "fields": ["key"]})
    issues = data.get("issues") or []
    return issues[0]["key"] if issues else ""

def _jira_create_issue(test_name: str, error_type: str, failure_text: str, analysis: str,
                       label: str, classification_text: str = "") -> str:
    url = f"{_jira_base_url()}/rest/api/3/issue"
    payload = {
        "fields": {
            "project": {"key": _jira_project_key()},
            "summary": _jira_issue_summary(test_name, classification_text, failure_text),
            "issuetype": {"name": _jira_issue_type()},
            "description": _build_jira_description(test_name, error_type, failure_text, analysis),
            "labels": ["auto-retry", label],
            **_jira_extra_fields(),
        }
    }
    data = _jira_request("POST", url, payload)
    issue_key = data.get("key", "")
    if issue_key:
        _, fix_text = _split_jira_recommendation(analysis)
        if fix_text:
            try:
                _jira_add_recommendation_comment(issue_key, fix_text)
            except Exception as exc:
                logging.warning(f"[jira] failed to add recommendation comment for {issue_key}: {exc}")
    return issue_key

def _jira_add_comment(issue_key: str, error_type: str, failure_text: str, analysis: str) -> None:
    url = f"{_jira_base_url()}/rest/api/3/issue/{issue_key}/comment"
    body_text = (
        f"재발 감지 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n"
        f"Error Type: {error_type or 'Unknown'}\n\n"
        f"AI 분석\n{analysis or '(분석 없음)'}\n\n"
        f"실패 로그\n```\n{_excerpt(failure_text, limit=2000)}\n```"
    )
    _jira_request("POST", url, {"body": _text_to_adf(body_text)})

def _create_or_update_jira_issue(test_name: str, error_type: str, failure_text: str,
                                 analysis: str, classification_text: str = "") -> tuple:
    if not _jira_notify_enabled():
        return "", ""
    label = _jira_test_label(test_name)
    try:
        issue_key = _jira_find_open_issue(label)
        if issue_key:
            _jira_add_comment(issue_key, error_type, failure_text, analysis)
        else:
            issue_key = _jira_create_issue(test_name, error_type, failure_text, analysis, label, classification_text)
        if issue_key:
            return issue_key, f"{_jira_base_url()}/browse/{issue_key}"
    except Exception as exc:
        logging.error(f"[jira issue failed] {test_name}: {exc}")
    return "", ""


# ── NAS 백업 ─────────────────────────────────────────────────────────────────

def _nas_report_root() -> str:
    return os.getenv("NAS_REPORT_ROOT", "").strip()

def _nas_copy_enabled() -> bool:
    raw = os.getenv("NAS_REPORT_ENABLED", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(_nas_report_root())

def _copy_screenshot_to_nas(screenshot_path: str) -> str:
    if not screenshot_path or not os.path.exists(screenshot_path):
        return ""
    nas_root = _nas_report_root()
    if not nas_root:
        return ""
    try:
        import shutil
        dated_dir = Path(nas_root) / datetime.now().strftime("%Y%m%d")
        dated_dir.mkdir(parents=True, exist_ok=True)
        dest = dated_dir / Path(screenshot_path).name
        shutil.copy2(screenshot_path, dest)
        return str(dest)
    except Exception as exc:
        logging.warning(f"[nas copy failed] {screenshot_path}: {exc}")
        return ""


# ── AI 분석 ──────────────────────────────────────────────────────────────────

def _extract_failure_context(failure_text: str, frame_pattern: str = "",
                             utility_files: tuple = ()) -> dict:
    text   = failure_text or ""
    frames = re.findall(frame_pattern, text) if frame_pattern else []
    locator_match   = re.search(r"locator=(\([^\n]+\))", text)
    exception_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*Exception): Message: ([^\n]+)", text)

    caller_frame = ""
    for frame in reversed(frames):
        if not any(util in frame for util in utility_files):
            caller_frame = frame
            break

    return {
        "frames": frames[:8],
        "top_frame": frames[0] if frames else "",
        "last_project_frame": frames[-1] if frames else "",
        "caller_frame": caller_frame,
        "locator": locator_match.group(1) if locator_match else "",
        "exception": (
            f"{exception_match.group(1)}: {exception_match.group(2).strip()}"
            if exception_match else ""
        ),
    }


def _extract_caller_source(caller_frame: str, root_dir: Path, max_lines: int = 50) -> str:
    m = re.match(r"([\w/\\]+\.py):(\d+): in (\w+)", caller_frame)
    if not m:
        return ""
    rel_path, lineno, func_name = m.group(1), int(m.group(2)), m.group(3)
    abs_path = root_dir / rel_path
    if not abs_path.exists():
        return ""
    try:
        lines = abs_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    func_start = None
    for i in range(min(lineno - 1, len(lines) - 1), -1, -1):
        if re.match(rf"\s*def {re.escape(func_name)}\s*\(", lines[i]):
            func_start = i
            break
    if func_start is None:
        start = max(0, lineno - 10)
        return "\n".join(lines[start: min(len(lines), lineno + 10)])
    indent = len(lines[func_start]) - len(lines[func_start].lstrip())
    func_end = func_start + 1
    for i in range(func_start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            func_end = i
            continue
        if len(lines[i]) - len(lines[i].lstrip()) <= indent and stripped:
            break
        func_end = i
    snippet = lines[func_start: func_end + 1]
    if len(snippet) > max_lines:
        snippet = snippet[:max_lines] + [f"    # ... ({len(snippet) - max_lines}줄 생략)"]
    return "\n".join(snippet)


def _build_code_aware_guidance(failure_text: str, context: dict) -> str:
    hints = []
    lowered = (failure_text or "").lower()
    if "invalidsessionidexception" in lowered or "invalid session id" in lowered:
        hints.append(
            "- 드라이버 세션이 중간에 종료되었을 가능성을 점검하고, "
            "locator 문제와 세션 종료 문제를 구분해서 제안하세요."
        )
    if context.get("locator"):
        hints.append(f"- 가능하면 `{context['locator']}` 를 사용하는 호출부를 함께 언급하세요.")
    if context.get("caller_frame"):
        hints.append(f"- 수정 권장사항은 공통 유틸이 아닌 실제 호출부 `{context['caller_frame']}` 기준으로 작성하세요.")
    elif context.get("last_project_frame"):
        hints.append(f"- 최소 1개 이상 권장사항은 `{context['last_project_frame']}` 기준으로 구체적으로 작성하세요.")
    return "\n".join(hints)


def _build_ai_prompt(test_name: str, error_type: str, failure_text: str,
                     frame_pattern: str = "", utility_files: tuple = (),
                     root_dir: Path = None) -> str:
    context      = _extract_failure_context(failure_text, frame_pattern, utility_files)
    context_lines = []
    if context["exception"]:
        context_lines.append(f"- 추출 예외: {context['exception']}")
    if context["top_frame"]:
        context_lines.append(f"- 최초 프로젝트 스택: {context['top_frame']}")
    if context["last_project_frame"]:
        context_lines.append(f"- 마지막 프로젝트 스택: {context['last_project_frame']}")
    if context["caller_frame"] and context["caller_frame"] != context["last_project_frame"]:
        context_lines.append(f"- 실제 호출부 (공통 유틸 제외): {context['caller_frame']}")
        if root_dir is not None:
            source = _extract_caller_source(context["caller_frame"], root_dir)
            if source:
                context_lines.append(f"- 호출부 소스코드:\n```python\n{source}\n```")
    if context["locator"]:
        context_lines.append(f"- 추출 locator: {context['locator']}")

    guidance = _build_code_aware_guidance(failure_text, context)

    utility_instruction = ""
    if utility_files:
        utility_instruction = (
            f"{', '.join(utility_files)} 등 공통 유틸 함수는 수정 대상이 아닙니다. "
            "스택에서 공통 유틸을 호출한 상위 파일에서 수정 포인트를 찾으세요.\n"
        )

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
        f"{utility_instruction}"
        "파일 경로와 함수명을 직접 인용하고, 수정 방법을 실제 코드 스니펫으로 제시하세요.\n"
        "코드를 작성할 때는 반드시 기존코드와 수정코드를 아래 형식으로 작성하세요:\n"
        "기존코드:\n```\n(기존 코드)\n```\n수정코드:\n```\n(수정된 코드)\n```\n"
        "다음 수정은 절대 제안하지 마세요:\n"
        "- timeout 값만 늘리는 수정\n"
        "- AssertionError나 예외를 catch해서 logging.warning으로 대체하는 수정\n"
        "- 공통 함수 내부에 특정 테스트용 값을 하드코딩하는 수정\n"
        "각 항목 사이에는 반드시 빈 줄을 하나 추가하세요.\n\n"
        f"추출 컨텍스트:\n{chr(10).join(context_lines) if context_lines else '- 없음'}\n\n"
        f"추가 작성 가이드:\n{guidance or '- 없음'}\n\n"
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
                         frame_pattern: str = "", utility_files: tuple = (),
                         root_dir: Path = None) -> str:
    trimmed = (failure_text or "").strip()
    if not trimmed:
        return ""
    trimmed = trimmed[-_ai_max_chars():]
    prompt  = _build_ai_prompt(test_name, error_type, trimmed, frame_pattern, utility_files, root_dir)
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
    utility_files    = _utility_files(item.config)
    root_dir         = _project_root(item.config)
    final_error_type = error_type or _final_error_type(longrepr, classification_text)

    analysis = analyze_test_failure(
        test_name=item.nodeid,
        error_type=final_error_type,
        failure_text=longrepr,
        frame_pattern=frame_pattern,
        utility_files=utility_files,
        root_dir=root_dir,
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

    screenshot_path = ""
    screenshot_paths = getattr(call_report, "screenshot_paths", None) or []
    if screenshot_paths and _nas_copy_enabled():
        screenshot_path = _copy_screenshot_to_nas(screenshot_paths[-1])
        if screenshot_path:
            logging.info(f"[nas report] {item.nodeid} → {screenshot_path}")

    jira_key, jira_url = _create_or_update_jira_issue(
        item.nodeid, final_error_type, longrepr, analysis, classification_text
    )
    if jira_key:
        logging.info(f"[jira issue] {item.nodeid} → {jira_key}")

    _notify_slack_final_failure(item.nodeid, final_error_type, longrepr, analysis, report_path, jira_url)
    _notify_teams_final_failure(item.nodeid, final_error_type, longrepr, analysis, screenshot_path, jira_key, jira_url)


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

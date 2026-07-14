# pytest-smart-retry

Pytest plugin for automatic test retry with AI log analysis, Jira issue tracking, and Slack/Teams notifications.

`@pytest.mark.auto_retry` 마커가 붙은 테스트에서 Timeout/Network 계열 실패를 자동으로 재시도하고, 최종 실패 시 AI 분석 리포트를 생성하고 Jira 이슈를 등록한 뒤 Slack/Teams로 알림을 보냅니다.

## Features

- Timeout/Network 실패만 선택적으로 재시도 (Assertion 실패는 즉시 종료)
- Selenium 드라이버 자동 리셋 (`about:blank` 이동)
- 최종 실패 시 AI 로그 분석 (Ollama / OpenAI 지원), 실패 호출부 소스코드 자동 첨부, 오류 유형별 분석 가이드 힌트
- Slack / Microsoft Teams 웹훅 알림
- 최종 실패 시 Jira 이슈 자동 생성 (QA Kanban 보드, 동일 테스트 재발 시 코멘트로 누적)
- 실패 스크린샷 NAS 백업 (선택)
- `conftest.py` 없이 자동 등록 (pytest11 entry point)

## Installation

```bash
# GitHub에서 직접 설치
pip install git+https://github.com/soyeooooo/pytest-smart-retry.git

# AI 분석 기능 포함
pip install "git+https://github.com/soyeooooo/pytest-smart-retry.git#egg=pytest-smart-retry[ai]"

# Slack 알림 기능 포함
pip install "git+https://github.com/soyeooooo/pytest-smart-retry.git#egg=pytest-smart-retry[slack]"
```

로컬 클론 후 설치:

```bash
git clone https://github.com/soyeooooo/pytest-smart-retry.git
cd pytest-smart-retry

pip install .          # 일반 설치
pip install -e .       # 개발 모드 (코드 수정이 즉시 반영)
```

## Quick Start

```python
import pytest

@pytest.mark.auto_retry
def test_something():
    ...
```

재시도 횟수 설정 (`pytest.ini` 또는 `pyproject.toml`):

```ini
[pytest]
auto_retry_max = 3
auto_retry_driver_fixture = my_driver
```

## Configuration

### pytest.ini / pyproject.toml

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `auto_retry_max` | `2` | 재시도 횟수 |
| `auto_retry_driver_fixture` | `set_driver` | Selenium 드라이버 fixture 이름 |
| `auto_retry_frame_pattern` | `` | 스택 필터 정규식 |
| `auto_retry_utility_files` | `` | AI 분석 시 호출부에서 제외할 공통 유틸 파일명 (콤마 구분) |

CLI 옵션:

```bash
pytest --auto-retry-max=3
```

ini 옵션이 설정되지 않은 경우, 아래 환경변수가 fallback으로 사용됩니다:

| 변수 | 기본값 | 대응 ini 옵션 |
|------|--------|------|
| `AUTO_RETRY_MAX` | `2` | `auto_retry_max` |
| `AUTO_RETRY_DRIVER_FIXTURE` | `set_driver` | `auto_retry_driver_fixture` |
| `AUTO_RETRY_FRAME_PATTERN` | `` | `auto_retry_frame_pattern` |
| `AUTO_RETRY_UTILITY_FILES` | `` | `auto_retry_utility_files` |

### Environment Variables

#### AI 분석

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `AI_LOG_ANALYZER_PROVIDER` | `ollama` | `ollama` 또는 `openai` |
| `OLLAMA_MODEL` | `llama3` | Ollama 모델명 |
| `OLLAMA_ENDPOINT` | `http://localhost:11434/api/generate` | Ollama 엔드포인트 |
| `OPENAI_API_KEY` | | OpenAI API 키 |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI 모델명 |
| `AI_LOG_ANALYZER_OUTPUT_DIR` | `Report/ai_analysis` | 분석 리포트 저장 경로 |
| `AI_LOG_ANALYZER_TIMEOUT` | `120` | AI 요청 타임아웃 (초) |
| `AI_LOG_ANALYZER_MAX_CHARS` | `12000` | 로그 최대 문자 수 |

#### Slack

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `SLACK_WEBHOOK_URL` | | Incoming Webhook URL |
| `SLACK_NOTIFY_ON_FINAL_FAILURE` | `true` (URL 설정 시) | 최종 실패 알림 활성화 |

#### Microsoft Teams

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `TEAMS_WEBHOOK_URL` | | Power Automate Webhook URL |
| `TEAMS_NOTIFY_ON_FINAL_FAILURE` | `true` (URL 설정 시) | 최종 실패 알림 활성화 |

#### Jira

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `JIRA_BASE_URL` | | Jira Cloud URL (예: `https://your-domain.atlassian.net`) |
| `JIRA_EMAIL` | | 계정 이메일 (API 토큰 발급 계정) |
| `JIRA_API_TOKEN` | | 개인 API 토큰 ([발급 링크](https://id.atlassian.com/manage-profile/security/api-tokens)) |
| `JIRA_PROJECT_KEY` | `QAT` | 이슈를 생성할 프로젝트 키 |
| `JIRA_ISSUE_TYPE` | `Bug` | 이슈 타입 |
| `JIRA_NOTIFY_ON_FINAL_FAILURE` | `true` (인증 정보 설정 시) | 최종 실패 시 Jira 이슈 생성 활성화 |
| `JIRA_EXTRA_FIELDS` | | 프로젝트별 필수 커스텀 필드를 넘기는 JSON 객체 (예: `{"customfield_10771":["automation"]}`) |
| `AUTO_RETRY_ENV_NAME` | | CI/실행 환경 이름 (예: `QuickBuild`). **설정하지 않으면 로컬 실행으로 간주해 Jira 이슈를 생성하지 않습니다** — CI 파이프라인에서만 설정하세요. |

동일 테스트(nodeid 기반 라벨)로 열려 있는 이슈가 있으면 코멘트만 추가하고, 없으면 새 이슈를 생성합니다. 생성/코멘트된 이슈 링크는 Slack/Teams 알림에도 함께 첨부됩니다.

이슈 제목은 고정 포맷입니다: `[smartRetry][{AUTO_RETRY_ENV_NAME}] {실패 메시지 첫 줄}`
(예: `[smartRetry][QuickBuild] AssertionError: 로그인 버튼이 비활성화 상태입니다`)
테스트 이름/스택 등 상세 내용은 이슈 본문(description)에 포함됩니다.

프로젝트에 필수(required) 커스텀 필드가 있으면 이슈 생성이 `HTTP 400`으로 거부됩니다. 어떤 필드가 필수인지는 아래로 확인할 수 있습니다:
```
GET /rest/api/3/issue/createmeta?projectKeys=<프로젝트키>&issuetypeIds=<이슈타입ID>&expand=projects.issuetypes.fields
```
`required: true`이면서 `hasDefaultValue: false`인 필드를 `JIRA_EXTRA_FIELDS`에 채워주면 됩니다.

#### NAS 백업

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `NAS_REPORT_ROOT` | | 스크린샷을 복사할 NAS 경로 (예: `\\nas\reports`). 설정 시에만 동작 |
| `NAS_REPORT_ENABLED` | `true` (경로 설정 시) | NAS 백업 활성화 |

설정 시 최종 실패 스크린샷을 `NAS_REPORT_ROOT/YYYYMMDD/` 아래로 복사하고, Teams 알림의 Screenshot 필드에 경로를 첨부합니다.

## How It Works

1. `@pytest.mark.auto_retry` 마커가 있는 테스트만 대상
2. 테스트 실패 시 오류 메시지를 분류:
   - **Timeout/Network** → `auto_retry_max` 횟수만큼 재시도, Selenium 드라이버 리셋
   - **Assertion/기타** → 즉시 최종 실패 처리
3. 최종 실패 시:
   - AI 분석 리포트 생성 (`Report/ai_analysis/ai_analysis.txt`)
   - Jira 이슈 생성 또는 기존 이슈에 코멘트 추가 (QA Kanban 보드)
   - Slack/Teams 웹훅 알림 발송 (Jira 이슈 링크 포함)
4. 세션 시작 시 `Report/ai_analysis` 디렉토리를 미리 생성 (CI 파일 전송 단계 오류 방지)
5. 전체 pass 시 리포트 파일에 `전부 pass입니다 :)!` 메시지 기록

## Project Structure

```
pytest-smart-retry/
├── pyproject.toml
├── README.md
└── pytest_smart_retry/
    ├── __init__.py
    └── plugin.py
```

## Requirements

- Python >= 3.9
- pytest >= 7

Optional:
- `openai` — (예비용, 현재는 OpenAI 호출도 표준 라이브러리 urllib로 직접 구현되어 있어 미사용. 추후 공식 SDK로 전환할 경우를 대비해 남겨둠)
- `requests` — (예비용, 현재는 표준 라이브러리 urllib 사용)

## License

MIT

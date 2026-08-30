# AI Conversation Survey

Streamlit 기반의 사전 설문 → 무작위 조건 배정 → AI 대화 → 사후 설문 웹 앱입니다.

## Local run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

OpenAI를 연결하려면 `.streamlit/secrets.toml.example`을 `.streamlit/secrets.toml`로 복사하고 API 키를 설정합니다. 키가 없으면 앱은 데모 응답으로 작동합니다.

## Streamlit Community Cloud

1. 이 저장소를 GitHub에 push합니다.
2. Streamlit Community Cloud에서 **Create app**을 선택합니다.
3. 저장소와 `app.py`를 지정합니다.
4. Advanced settings의 Secrets에 다음을 입력합니다.

```toml
OPENAI_API_KEY = "your-key"
OPENAI_MODEL = "gpt-5-mini"
```

## Data note

기본 데이터베이스는 `survey.db` SQLite 파일입니다. Streamlit Community Cloud의 로컬 파일은 재배포·재시작 시 영구 보존이 보장되지 않으므로, 본 조사 전에는 외부 PostgreSQL 등 영구 데이터베이스로 교체해야 합니다. 현재 구성은 개발 및 파일럿용입니다.

OpenAI Responses API 호출에는 `store=False`를 사용하며, 설문 앱 자체 DB에는 대화 내용이 저장됩니다.

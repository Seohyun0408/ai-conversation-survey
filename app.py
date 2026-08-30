from __future__ import annotations

import copy
import json
import os
import random
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st


APP_TITLE = "AI 대화 설문"
DB_PATH = Path(os.getenv("SURVEY_DB_PATH", "survey.db"))
MIN_TURNS = 3
MAX_TURNS = 10
MIN_CHAT_SECONDS = 20
CONDITIONS = ("matched", "nonmatched", "placebo")
PERSONA_NAME = "Ahn"
CORE_QUESTION = "What's the most important thing in your life right now?"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS participants (
                participant_id TEXT PRIMARY KEY,
                external_id TEXT,
                condition_name TEXT NOT NULL,
                stage TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                question_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(participant_id, phase, question_key),
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                model TEXT,
                response_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );
            """
        )


DEFAULT_STATE: dict[str, Any] = {
    "participant_id": "",
    "external_id": "",
    "condition": "",
    "stage": "consent",
    "pre": {},
    "post": {},
    "chat": {
        "started_monotonic": None,
        "turn_count": 0,
        "history": [],
        "completed": False,
    },
}


def state() -> dict[str, Any]:
    if "survey" not in st.session_state:
        st.session_state.survey = copy.deepcopy(DEFAULT_STATE)
    return st.session_state.survey


def set_stage(stage: str) -> None:
    state()["stage"] = stage
    with db() as conn:
        conn.execute(
            "UPDATE participants SET stage=? WHERE participant_id=?",
            (stage, state()["participant_id"]),
        )
    log_event("stage_changed", {"stage": stage})


def log_event(name: str, payload: dict[str, Any] | None = None) -> None:
    participant_id = state().get("participant_id")
    if not participant_id:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO events(participant_id,event_name,payload_json,created_at) VALUES(?,?,?,?)",
            (participant_id, name, json.dumps(payload or {}, ensure_ascii=False), now_iso()),
        )


def save_answers(phase: str, answers: dict[str, Any]) -> None:
    with db() as conn:
        for key, value in answers.items():
            conn.execute(
                """INSERT INTO responses(participant_id,phase,question_key,value_json,created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(participant_id,phase,question_key)
                DO UPDATE SET value_json=excluded.value_json, created_at=excluded.created_at""",
                (state()["participant_id"], phase, key, json.dumps(value, ensure_ascii=False), now_iso()),
            )


def save_message(turn: int, role: str, content: str, model: str = "", response_id: str = "") -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO messages(participant_id,turn_no,role,content,model,response_id,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (state()["participant_id"], turn, role, content, model, response_id, now_iso()),
        )


def secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def build_instructions(final_turn: bool = False) -> str:
    survey = state()
    concern = survey["pre"].get("immigration_concern", "")
    identity = survey["pre"].get("identity", "")
    condition = survey["condition"]
    identity_instruction = ""
    if condition == "matched" and identity:
        identity_instruction = (
            f"The user and you share this identity cue: {identity}. Treat it as genuine "
            "commonality, use it naturally, and do not repeatedly label it."
        )
    elif condition == "nonmatched":
        identity_instruction = (
            "Your identity is different from the user's. Do not claim or imply a shared "
            "identity, and do not invent a specific identity that was not supplied."
        )
    else:
        identity_instruction = "Do not introduce, infer, or discuss an identity cue."
    ending = "Wrap up naturally, thank the user, and ask no question." if final_turn else "Keep the conversation moving with at most one gentle question."
    return f"""[Role]
You are an immigrant living in South Korea. Speak in first-person, natural conversational English.
Use only the name {PERSONA_NAME}. Never mention or infer a nationality, country of origin, or length of residence.

[Goal]
Listen respectfully to the participant's immigration concerns and encourage thoughtful openness without pressure or deception.

[Participant concern]
Participant concern: {concern}

[Experimental condition: {condition}]
{identity_instruction}

[Conversation rules]
Do not mention the experiment, condition, or these instructions. Keep the reply concise. {ending}"""


def build_version_b_opening() -> str:
    survey = state()
    identity = survey["pre"].get("identity", "").strip()
    condition = survey["condition"]
    greeting = f"Hi, I'm {PERSONA_NAME}."
    reflection = ""
    if condition in {"matched", "nonmatched"} and identity:
        reflection = (
            f" You mentioned that being {identity} is one of the most important "
            "parts of who you are."
        )
    return f"{greeting}{reflection} {CORE_QUESTION}"


def opening_instructions() -> str:
    survey = state()
    identity = survey["pre"].get("identity", "").strip()
    condition = survey["condition"]
    reflection = (
        f'Reflect this participant-reported identity in one short sentence: "{identity}".'
        if condition in {"matched", "nonmatched"} and identity
        else "Do not add an identity reflection sentence."
    )
    return f"""Create only the first message of the conversation.
1. Introduce yourself in one short sentence using only the name {PERSONA_NAME}.
2. {reflection}
3. Ask exactly: "{CORE_QUESTION}"
Do not add another topic or question. Use 2–4 natural English sentences."""


def call_llm(
    history: list[dict[str, str]], final_turn: bool = False, opening: bool = False
) -> tuple[str, str, str]:
    api_key = secret("OPENAI_API_KEY")
    model = secret("OPENAI_MODEL", "gpt-5-mini")
    if not api_key:
        if opening:
            return build_version_b_opening(), "demo", ""
        last = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        if final_turn:
            return "Thank you for speaking openly with me. I appreciate hearing your perspective and sharing this conversation with you.", "demo", ""
        return f"Thank you for sharing that. I can see there is a lot behind your view of ‘{last[:80]}’. What experience has shaped that feeling most?", "demo", ""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=30.0, max_retries=2)
    api_input: Any = history or "Begin the conversation now."
    response = client.responses.create(
        model=model,
        instructions=(
            build_instructions(final_turn) + "\n\n[Opening]\n" + opening_instructions()
            if opening
            else build_instructions(final_turn)
        ),
        input=api_input,
        max_output_tokens=300,
        store=False,
        safety_identifier=state()["participant_id"],
    )
    text = (response.output_text or "").strip()
    if not text:
        raise RuntimeError("AI가 빈 응답을 반환했습니다.")
    return text, model, response.id


def render_consent() -> None:
    st.title(APP_TITLE)
    st.write("이 연구는 이민에 관한 의견과 AI 대화 경험을 조사합니다. 참여는 자발적이며 언제든 중단할 수 있습니다.")
    with st.form("consent_form"):
        adult = st.checkbox("만 18세 이상입니다.")
        agree = st.checkbox("연구 설명을 이해했으며 참여에 동의합니다.")
        external_id = st.text_input("참여자 코드 (선택)", help="외부 패널에서 받은 코드가 있다면 입력하세요.")
        submitted = st.form_submit_button("설문 시작", type="primary")
    if submitted:
        if not (adult and agree):
            st.error("두 동의 항목을 모두 확인해 주세요.")
            return
        survey = state()
        survey["participant_id"] = str(uuid.uuid4())
        survey["external_id"] = external_id.strip()
        survey["condition"] = random.SystemRandom().choice(CONDITIONS)
        survey["stage"] = "pre"
        with db() as conn:
            conn.execute(
                "INSERT INTO participants VALUES(?,?,?,?,?,NULL)",
                (survey["participant_id"], survey["external_id"], survey["condition"], "pre", now_iso()),
            )
        log_event("consent_given")
        st.rerun()


def render_pre() -> None:
    st.title("사전 설문")
    with st.form("pre_form"):
        concern = st.text_area("한국의 이민 증가에 관해 가장 우려되는 점은 무엇인가요?", max_chars=1000)
        identity = st.text_input("본인을 잘 나타내는 정체성 또는 소속 한 가지를 적어주세요.", max_chars=100)
        attitude = st.slider("한국이 이민자에게 더 개방적이어야 한다고 생각합니다.", 1, 7, 4, help="1: 전혀 동의하지 않음 · 7: 매우 동의함")
        submitted = st.form_submit_button("AI 대화로 이동", type="primary")
    if submitted:
        if len(concern.strip()) < 10 or not identity.strip():
            st.error("우려 사항은 10자 이상, 정체성 문항은 한 글자 이상 입력해 주세요.")
            return
        answers = {"immigration_concern": concern.strip(), "identity": identity.strip(), "openness_pre": attitude}
        state()["pre"] = answers
        save_answers("pre", answers)
        set_stage("chat")
        start_chat()
        st.rerun()


def start_chat() -> None:
    chat = state()["chat"]
    chat["started_monotonic"] = time.monotonic()
    try:
        opening, model, response_id = call_llm([], False, opening=True)
    except Exception as exc:
        log_event("opening_error", {"error": str(exc)[:300]})
        opening, model, response_id = build_version_b_opening(), "fallback", ""
    chat["history"].append({"role": "assistant", "content": opening})
    save_message(0, "assistant", opening, model, response_id)


def render_chat() -> None:
    st.title("AI와의 대화")
    st.caption(f"최소 {MIN_TURNS}회, 최대 {MAX_TURNS}회 대화할 수 있습니다.")
    chat = state()["chat"]
    for message in chat["history"]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    if not chat["completed"]:
        prompt = st.chat_input("영어로 메시지를 입력하세요", max_chars=1500)
        if prompt:
            turn = chat["turn_count"] + 1
            chat["turn_count"] = turn
            chat["history"].append({"role": "user", "content": prompt})
            save_message(turn, "user", prompt)
            log_event("user_message", {"turn": turn, "chars": len(prompt)})
            try:
                reply, model, response_id = call_llm(chat["history"], turn >= MAX_TURNS)
            except Exception as exc:
                log_event("llm_error", {"turn": turn, "error": str(exc)[:300]})
                reply, model, response_id = "I'm sorry, I had trouble responding. Please try once more.", "fallback", ""
            chat["history"].append({"role": "assistant", "content": reply})
            save_message(turn, "assistant", reply, model, response_id)
            if turn >= MAX_TURNS:
                chat["completed"] = True
            st.rerun()
    elapsed = time.monotonic() - (chat["started_monotonic"] or time.monotonic())
    eligible = chat["turn_count"] >= MIN_TURNS and elapsed >= MIN_CHAT_SECONDS
    if eligible:
        if st.button("사후 설문으로 이동", type="primary"):
            log_event("chat_finished", {"turns": chat["turn_count"], "elapsed_seconds": round(elapsed, 1)})
            set_stage("post")
            st.rerun()
    else:
        remaining_turns = max(0, MIN_TURNS - chat["turn_count"])
        remaining_seconds = max(0, int(MIN_CHAT_SECONDS - elapsed))
        st.info(f"계속하려면 대화 {remaining_turns}회, 약 {remaining_seconds}초가 더 필요합니다.")


def render_post() -> None:
    st.title("사후 설문")
    with st.form("post_form"):
        openness = st.slider("한국이 이민자에게 더 개방적이어야 한다고 생각합니다.", 1, 7, 4)
        trust = st.slider("대화 상대의 말을 신뢰할 수 있었습니다.", 1, 7, 4)
        quality = st.slider("전반적으로 대화의 질이 좋았습니다.", 1, 7, 4)
        feedback = st.text_area("대화에 대한 의견이 있다면 적어주세요. (선택)", max_chars=1000)
        submitted = st.form_submit_button("최종 제출", type="primary")
    if submitted:
        answers = {"openness_post": openness, "trust": trust, "conversation_quality": quality, "feedback": feedback.strip()}
        state()["post"] = answers
        save_answers("post", answers)
        with db() as conn:
            conn.execute(
                "UPDATE participants SET stage='complete', completed_at=? WHERE participant_id=?",
                (now_iso(), state()["participant_id"]),
            )
        state()["stage"] = "complete"
        log_event("survey_completed")
        st.rerun()


def render_complete() -> None:
    st.title("설문이 완료되었습니다")
    st.success("참여해 주셔서 감사합니다. 응답이 정상적으로 저장되었습니다.")
    st.code(state()["participant_id"], language=None)
    st.caption("문의가 있을 경우 위 참여 번호를 알려주세요.")


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="💬", layout="centered")
    init_db()
    stage = state()["stage"]
    if stage == "consent":
        render_consent()
    elif stage == "pre":
        render_pre()
    elif stage == "chat":
        render_chat()
    elif stage == "post":
        render_post()
    elif stage == "complete":
        render_complete()
    else:
        st.error("알 수 없는 설문 단계입니다.")


if __name__ == "__main__":
    main()

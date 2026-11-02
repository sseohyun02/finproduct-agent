"""
HyperCLOVA X API 클라이언트 (2026 미래에셋 AI Festival)

두 가지 역할:
  1) 의도추출: 질문 -> 구조화된 의도 JSON (intent_prompt.md 사용)
  답변 표시는 agent_pipeline의 결정론적 포맷터가 담당한다.

OpenAI 호환 엔드포인트 사용(권장). .env에서 키 로드.
  CLOVA_API_KEY : CLOVA Studio API 키
  CLOVA_MODEL   : 모델명 (기본 HCX-005)
"""

import os
import json
import re


class ClovaClient:
    def __init__(self, prompt_path=None):
        # .env 로드
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        self.api_key = os.getenv("CLOVA_API_KEY", "")
        self.model = os.getenv("CLOVA_MODEL", "HCX-005")
        self.base_url = os.getenv(
            "CLOVA_BASE_URL",
            "https://clovastudio.stream.ntruss.com/v1/openai"
        )

        # OpenAI 호환 클라이언트
        from openai import OpenAI
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        # 의도추출 프롬프트 로드
        self.intent_prompt = ""
        if prompt_path and os.path.exists(prompt_path):
            self.intent_prompt = open(prompt_path, encoding="utf-8").read()

    def _chat(self, system, user, temperature=0.1, max_tokens=1024):
        """CLOVA chat completions 호출."""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content

    # =========================================================
    # 1. 의도추출
    # =========================================================
    def extract_intent(self, question, temporal_context=None, validation_feedback=None):
        """질문과 시간 정보를 함께 전달해 의도 JSON을 추출."""
        system = self.intent_prompt

        if temporal_context is not None:
            system += (
                "\n\n## 실행 환경의 시간 정보\n"
                + json.dumps(temporal_context, ensure_ascii=False)
            )

        if validation_feedback:
            system += (
                "\n\n이전 의도 검증 오류입니다. 원래 질문을 다시 해석해 JSON 전체를 한 번 수정하세요. "
                "오류 조건을 임의로 삭제하지 말고 원문의 요구를 보존하세요. "
                "지원하지 않는 요구는 unsupported_conditions에 기록하세요.\n"
                + json.dumps(validation_feedback, ensure_ascii=False)
            )
        raw = self._chat(system, question, temperature=0.1, max_tokens=2048)
        return self._parse_json(raw)
    
    def _parse_json(self, text):
        """LLM 출력에서 JSON 추출(마크다운 펜스 제거)."""
        if not text:
            return None
        # ```json ... ``` 제거
        text = re.sub(r"```json\s*|\s*```", "", text).strip()
        # 첫 { 부터 마지막 } 까지
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

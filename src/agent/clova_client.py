"""
HyperCLOVA X API 클라이언트 (2026 미래에셋 AI Festival)

두 가지 역할:
  1) 의도추출: 질문 -> 구조화된 의도 JSON (intent_prompt.md 사용)
  2) 답변생성: 쿼리 결과 -> 자연어 답변

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
    def extract_intent(self, question, temporal_context=None):
        """질문과 시간 정보를 함께 전달해 의도 JSON을 추출."""
        system = self.intent_prompt

        if temporal_context is not None:
            system += (
                "\n\n## 실행 환경의 시간 정보\n"
                + json.dumps(temporal_context, ensure_ascii=False)
            )

        raw = self._chat(system, question, temperature=0.1)
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

    # =========================================================
    # 2. 답변생성
    # =========================================================
    def generate_answer(self, question, result_summary, note=None):
        """
        질문 + 쿼리결과 요약 -> 자연어 답변.
        result_summary: 쿼리 결과를 텍스트로 정리한 것.
        note: concepts.json의 답변문구(정성표현 기준 안내 등).
        """
        system = (
            "당신은 금융상품 정보를 사실 기반으로 안내하는 도우미입니다. "
            "제공된 검색 결과에 있는 정보만 사용하고, 없는 정보는 지어내지 마세요. "
            "다음은 반드시 지키세요:\n"
            "- 검색 결과의 각 값(위험등급·보수·순자산 등)을 그대로 사용. 임의로 바꾸거나 추측 금지\n"
            "- 위험등급 숫자는 검색 결과에 나온 값 그대로 (예: 위험등급=5면 5로 표기, 1로 바꾸지 말 것)\n"
            "- 투자 권유·추천 금지 ('투자하는 것이 좋습니다', '장기 관점' 등 조언 문구 절대 금지)\n"
            "- 미래 가격·수익 예측 금지\n"
            "- 검색 결과에 없는 일반론·주의사항 덧붙이지 말 것\n"
            "- 상품명과 핵심 수치를 간결한 개조식(번호목록)으로 정리. 표 대신 목록 사용\n"
            "- 순자산은 검색 결과에 표시된 단위 그대로 사용\n"
            "- 답변 끝에 조언이나 권유 문장을 붙이지 말 것"
        )
        user = f"질문: {question}\n\n검색 결과:\n{result_summary}"
        if note:
            user += f"\n\n안내사항(답변에 포함): {note}"
        answer = self._chat(system, user, temperature=0.3, max_tokens=1024)
        return self._strip_advice(answer)

    def _strip_advice(self, text):
        """CLOVA가 습관적으로 붙이는 투자 조언·권유 문구를 후처리로 제거."""
        if not text:
            return text
        # 조언성 문장이 포함된 줄/문장 제거 (문장 단위)
        advice_markers = [
            "투자 성향", "투자성향", "위험 감수", "위험감수", "신중", "고려해야",
            "고려하시", "권장", "추천드립니다", "판단하시", "결정하시",
            "장기적인 관점", "장기 관점", "분산 투자", "분산투자", "전문가와 상담",
            "참고하시기", "참고 바랍니다", "유의하시", "주의하시",
        ]
        # 문장 분리(。. 개행 기준) 후 조언 문장 제거
        import re
        sentences = re.split(r'(?<=[.。\n])', text)
        kept = []
        for s in sentences:
            if any(m in s for m in advice_markers):
                continue  # 조언 문장은 버림
            kept.append(s)
        result = "".join(kept).strip()
        return result if result else text  # 다 지워지면 원본 유지

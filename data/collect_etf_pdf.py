# -*- coding: utf-8 -*-
"""
ETF 구성종목(PDF) 수집 스크립트

- 우리 원본 데이터(국내ETF 마스터)에 있는 ETF 종목코드로 구성종목을 수집
- KRX 로그인 정보는 .env 파일에서 읽음 (KRX_ID, KRX_PW)
- 결과는 하나의 엑셀 파일에 ETF별 시트로 저장
- 중간에 끊겨도 이어받기 가능 (이미 수집한 시트는 건너뜀)
- 실패 종목은 따로 기록

[2026-08 갱신] 대회측 새 데이터 반영:
  - 마스터 파일명이 소문자로 변경(pref01n001_data.xlsx) → 경로 패턴 수정.
  - ETF만 수집(pd_grp_no=='ETF', 1,235개). ETN(545개)은 구성종목 개념 없어 제외.
  - 전체 재수집을 원하면 기존 etf_portfolio.xlsx를 먼저 삭제하고 실행할 것.

사전 준비:
1. pip install pykrx pandas openpyxl python-dotenv
2. 프로젝트 루트에 .env 파일 생성 후 아래 내용 입력:
     KRX_ID=본인아이디
     KRX_PW=본인비밀번호
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import RAW_DIR, ROOT
import os
import time
import glob
import pandas as pd
from datetime import datetime

# ── 1. 환경변수(.env) 로드 ──────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    print("python-dotenv가 없습니다. pip install python-dotenv 후 다시 실행하세요.")
    raise

KRX_ID = os.environ.get("KRX_ID", "")
KRX_PW = os.environ.get("KRX_PW", "")

if not KRX_ID or not KRX_PW:
    print("경고: .env에 KRX_ID / KRX_PW가 설정되지 않았습니다.")
    print("일부 기능(구성종목 조회)은 로그인이 필요해 실패할 수 있습니다.")

os.environ["KRX_ID"] = KRX_ID
os.environ["KRX_PW"] = KRX_PW

from pykrx import stock

# ── 2. 설정 ────────────────────────────────────────────
BASE_DATE = "20260710"          # 구성종목 기준일 (확실한 영업일로 지정)
OUTPUT_FILE = str(RAW_DIR / "etf_portfolio.xlsx")   # 결과 저장 파일
FAIL_LOG = str(RAW_DIR / "etf_portfolio_fail.csv")  # 실패 종목 기록
SLEEP_SEC = 1.5                 # 요청 간 대기(초) - IP 차단 방지
# 새 파일명(소문자, datarows 없음) 대응: 코드 접두부만으로 매칭
MASTER_GLOB = str(RAW_DIR / "**/*ref01n001*.xlsx")


# ── 3. 우리 데이터에서 국내 ETF 목록 뽑기 ───────────────
def load_etf_list():
    """국내ETF 마스터에서 ETF(ETN 제외) 종목코드·이름 목록을 반환"""
    files = [f for f in glob.glob(MASTER_GLOB, recursive=True)
             if "schema" not in f.lower()
             and not os.path.basename(f).startswith("._")]
    if not files:
        raise FileNotFoundError(f"국내ETF 마스터 파일을 찾을 수 없습니다: {MASTER_GLOB}")
    df = pd.read_excel(files[0])
    print(f"마스터 파일: {files[0]} ({len(df)}행)")

    # ETF만 (ETN 제외) - 구성종목 개념은 ETF에 해당
    if "pd_grp_no" in df.columns:
        df = df[df["pd_grp_no"] == "ETF"].copy()

    # 단축코드 변환: pd_itm_no_ma(A305080) -> 305080
    def to_short(ma):
        s = str(ma).strip()
        return s[1:] if s and s[0].isalpha() else s

    df["short_code"] = df["pd_itm_no_ma"].apply(to_short)

    result = df[["short_code", "pd_nm"]].dropna().drop_duplicates("short_code")
    return list(result.itertuples(index=False, name=None))


# ── 4. 이미 수집한 종목 확인 (이어받기용) ───────────────
def already_done():
    if not os.path.exists(OUTPUT_FILE):
        return set()
    try:
        xl = pd.ExcelFile(OUTPUT_FILE)
        return {name.split("_")[0] for name in xl.sheet_names}
    except Exception:
        return set()


# ── 5. 메인 수집 로직 ───────────────────────────────────
def main():
    etf_list = load_etf_list()
    done = already_done()
    print(f"수집 대상 ETF: {len(etf_list)}개")
    print(f"이미 수집됨: {len(done)}개 (건너뜀)")
    print(f"기준일: {BASE_DATE}\n")

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    mode = "a" if os.path.exists(OUTPUT_FILE) else "w"
    writer_kwargs = {"engine": "openpyxl", "mode": mode}
    if mode == "a":
        writer_kwargs["if_sheet_exists"] = "overlay"

    fails = []
    success = 0

    with pd.ExcelWriter(OUTPUT_FILE, **writer_kwargs) as writer:
        for i, (code, name) in enumerate(etf_list, 1):
            if code in done:
                continue

            safe_name = "".join(c for c in str(name) if c.isalnum() or c in " _")[:20]
            sheet = f"{code}_{safe_name}"[:31]

            try:
                pdf = stock.get_etf_portfolio_deposit_file(code, BASE_DATE)
                if pdf is None or len(pdf) == 0:
                    raise ValueError("빈 결과")
                pdf = pdf.reset_index()
                pdf.insert(0, "etf_code", code)
                pdf.to_excel(writer, sheet_name=sheet, index=False)
                success += 1
                print(f"[{i}/{len(etf_list)}] {name[:25]} ({code}) → {len(pdf)}종목 OK")
            except Exception as e:
                fails.append({"code": code, "name": name, "error": str(e)})
                print(f"[{i}/{len(etf_list)}] {name[:25]} ({code}) → 실패: {e}")

            time.sleep(SLEEP_SEC)

    if fails:
        pd.DataFrame(fails).to_csv(FAIL_LOG, index=False, encoding="utf-8-sig")
        print(f"\n실패 {len(fails)}개 → {FAIL_LOG} 에 기록")

    print(f"\n완료! 성공 {success}개 / 전체 {len(etf_list)}개")
    print(f"결과 파일: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
"""운용사/발행사 정규화 (etf_domestic 전용)

etf_domestic의 cu_fund_mgmt_co에는 세 가지 문제가 섞여 있다.
  1) 정상 표기지만 약칭 ('삼성', 'KB' 등) → 정식 법인명으로 통일
  2) 상품명이 통째로 들어간 오염 → 브랜드로 운용사 복원 (종목코드로 직접 지정)
  3) ETF 운용사 vs ETN 발행사가 다른 회사 → 합치지 않고 각각 정식명으로

원본 cu_fund_mgmt_co는 보존하고, 표준 법인명은 fund_mgmt_co_std에 저장한다.
번호1(BNK, 종목코드까지 손상)은 운용사만 복원하고 상품정보 손상은 row_valid로 표시.

* ETF/ETN 구분은 pd_grp_no 컬럼에 이미 있으므로 여기서 만들지 않는다.

[2026-08 갱신] 대회측 새 데이터 반영:
  - 신규 오염 브랜드 발견: 'ACE'(한국투자신탁운용), '디에스'(DS자산운용)
    → CONTAM_BRAND에 추가.
"""
import duckdb
from src.config import DB_PATH

# ── 1) 정상 표기 → 정식 법인명 (ETF 운용사) ──
ETF_MGMT = {
    "삼성": "삼성자산운용",
    "삼성KODEX": "삼성자산운용",
    "삼성액티브": "삼성액티브자산운용",     # 별개 법인
    "미래에셋": "미래에셋자산운용",
    "미래에셋TIGER": "미래에셋자산운용",
    "KB": "KB자산운용",
    "한국투자": "한국투자신탁운용",
    "한화": "한화자산운용",
    "한화PLUS": "한화자산운용",
    "신한": "신한자산운용",
    "키움": "키움투자자산운용",
    "NH-Amundi": "NH아문디자산운용",
    "하나": "하나자산운용",
    "우리": "우리자산운용",
    "대신": "대신자산운용",
    "타임폴리오": "타임폴리오자산운용",
    "에셋플러스": "에셋플러스자산운용",
    "교보악사": "교보악사자산운용",
    "마이다스": "마이다스에셋자산운용",
    "케이씨지아이": "케이씨지아이자산운용",
    "트러스톤": "트러스톤자산운용",
    "유리": "유리자산운용",
    "한국밸류": "한국밸류자산운용",
    "브이아이": "브이아이자산운용",
    "iM에셋": "iM에셋자산운용",
    "더제이": "더제이자산운용",
    "BNK": "BNK자산운용",
    "DB": "DB자산운용",
    "IBK": "IBK자산운용",
    "흥국": "흥국자산운용",
    "현대": "현대자산운용",
    "디에스": "DS자산운용",
}

# ── 2) ETN 발행사 → 정식 법인명 (증권사, 운용사와 별개) ──
ETN_ISSUER = {
    "삼성증권(주)": "삼성증권",
    "미래에셋증권 주식회사": "미래에셋증권",
    "KB증권(주)": "KB증권",
    "한국투자증권(주)": "한국투자증권",
    "신한투자증권 주식회사": "신한투자증권",
    "키움증권(주)": "키움증권",
    "하나증권주식회사": "하나증권",
    "대신증권(주)": "대신증권",
    "엔에이치투자증권(주)": "NH투자증권",
    "메리츠증권 주식회사": "메리츠증권",
}

# ── 3) 오염 건: 종목코드(pd_itm_no) → 운용사 직접 지정 ──
# 상품명 안의 브랜드로 수동 확인 (TIGER=미래에셋, KODEX=삼성, SOL=신한, DB, BNK, ACE=한투)
# 대부분은 아래 브랜드 규칙으로 자동 처리되지만, 명시적으로 브랜드 규칙을 둔다.
CONTAM_BRAND = {
    "TIGER": "미래에셋자산운용",
    "KODEX": "삼성자산운용",
    "SOL":   "신한자산운용",
    "UNICORN": "현대자산운용",
    "마이티": "DB자산운용",   # DB마이티
    "ACE": "한국투자신탁운용",  # 신규(2026-08): ACE 삼성전자SK하이닉스... 등
    "디에스": "DS자산운용",     # 신규(2026-08): 디에스 DS 코스닥액티브...
}


def apply_fund_mgmt(con: duckdb.DuckDBPyConnection):
    """etf_domestic에 fund_mgmt_co_std 컬럼을 추가한다."""
    _add_column_if_absent(con, "etf_domestic", "fund_mgmt_co_std", "VARCHAR")
    _add_column_if_absent(con, "etf_domestic", "row_valid", "BOOLEAN")
    con.execute("UPDATE etf_domestic SET row_valid = COALESCE(row_valid, true)")

    # (a) 정상 표기 매핑 (ETF 운용사 + ETN 발행사)
    for src, std in {**ETF_MGMT, **ETN_ISSUER}.items():
        con.execute(
            "UPDATE etf_domestic SET fund_mgmt_co_std = ? WHERE cu_fund_mgmt_co = ?",
            [std, src],
        )

    # (b) 오염 행: 아직 표준값이 안 채워진 것들을 브랜드로 복원
    #     운용사자리 값 또는 약어명(pd_abrv_nm)에 브랜드가 있으면 매핑
    for brand, std in CONTAM_BRAND.items():
        con.execute(f"""
            UPDATE etf_domestic
            SET fund_mgmt_co_std = ?
            WHERE fund_mgmt_co_std IS NULL
              AND (cu_fund_mgmt_co LIKE '%{brand}%' OR pd_abrv_nm LIKE '%{brand}%')
        """, [std])

    # (c) 오염 ETN 1건: '미래에셋증권글로벌헬스케어...(ETN)'
    con.execute("""
        UPDATE etf_domestic
        SET fund_mgmt_co_std = '미래에셋증권'
        WHERE fund_mgmt_co_std IS NULL
          AND cu_fund_mgmt_co LIKE '미래에셋증권%'
    """)

    # (d) 상품명/종목코드까지 손상된 행 → 상품정보 손상 플래그
    con.execute("""
        UPDATE etf_domestic
        SET row_valid = false
        WHERE cu_fund_mgmt_co = '.' OR pd_itm_no = 'KR'
    """)

    # ── 검증 ──
    n_null = con.execute(
        "SELECT COUNT(*) FROM etf_domestic WHERE fund_mgmt_co_std IS NULL"
    ).fetchone()[0]
    n_bad = con.execute(
        "SELECT COUNT(*) FROM etf_domestic WHERE row_valid = false"
    ).fetchone()[0]
    print(f"[etf_domestic] 운용사 표준화 완료. 미매핑 {n_null}건, 손상행 {n_bad}건")
    if n_null > 0:
        left = con.execute("""
            SELECT DISTINCT cu_fund_mgmt_co FROM etf_domestic
            WHERE fund_mgmt_co_std IS NULL LIMIT 10
        """).fetchdf()
        print("  미매핑 표기:", left['cu_fund_mgmt_co'].tolist())


def _add_column_if_absent(con, table, column, dtype):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")

# ── 해외ETF: 표기 오류 통일 (원본 직접 수정) ──
# 해외ETF는 이미 정식 법인명이라 정제할 게 거의 없음.
# 유일한 표기 흔들림은 대소문자 차이뿐이므로(정보 손실 없음) 원본을 직접 통일한다.
#   "SSgA Funds Management Inc" → "SSGA Funds Management Inc"
# 그룹핑/별칭(BlackRock, Vanguard 등)은 정제가 아니라 개념 사전(매핑표)에서 처리.
OVERSEAS_FIX = {
    "SSgA Funds Management Inc": "SSGA Funds Management Inc",
}


def apply_fund_mgmt_overseas(con: duckdb.DuckDBPyConnection):
    """etf_overseas의 운용사 표기 오류(대소문자)를 원본에서 직접 통일한다."""
    for wrong, correct in OVERSEAS_FIX.items():
        con.execute(
            "UPDATE etf_overseas SET cu_fund_mgmt_co = ? WHERE cu_fund_mgmt_co = ?",
            [correct, wrong],
        )

    # 검증: 통일 후 SSGA 표기가 하나로 합쳐졌는지
    df = con.execute("""
        SELECT cu_fund_mgmt_co, COUNT(*) n FROM etf_overseas
        WHERE cu_fund_mgmt_co ILIKE '%ssga%'
        GROUP BY 1
    """).fetchall()
    print(f"[etf_overseas] SSGA 표기 통일 완료: {df}")
    
    
if __name__ == "__main__":
    con = duckdb.connect(str(DB_PATH))
    apply_fund_mgmt(con)             # 국내ETF (fund_mgmt_co_std 생성)
    apply_fund_mgmt_overseas(con)    # 해외ETF (SSGA 원본 통일)
    con.close()
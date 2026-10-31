from datetime import date, datetime


def parse_date(value):
    """YYYY-MM-DD 또는 YYYYMMDD를 date로 변환."""
    if value is None:
        return None

    text = str(value).strip()

    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


class TemporalPolicy:
    def __init__(self, con):
        # 확인되지 않은 기준일은 추측해서 채우지 않는다.
        self.snapshot_dates = {
            "bond": None,
            "etf_domestic": None,
            "etf_overseas": None,
            "fund": None,
            "holdings": None,
        }

        if con is not None:
            self.snapshot_dates["bond"] = self._read_bond_date(con)

    def _read_bond_date(self, con):
        """실제 서비스 DB의 채권 기준일을 확인한다."""
        columns = {
            str(row[1]).lower()
            for row in con.execute(
                "PRAGMA table_info('bond')"
            ).fetchall()
        }

        if "info_base_dt" not in columns:
            return None

        rows = con.execute(
            "SELECT DISTINCT info_base_dt FROM bond"
        ).fetchall()

        if not rows:
            return None

        parsed = [parse_date(row[0]) for row in rows]

        # 누락·해석 실패·여러 날짜가 섞인 경우 단일 기준일로 단정하지 않는다.
        if any(value is None for value in parsed):
            return None

        dates = set(parsed)
        return next(iter(dates)) if len(dates) == 1 else None

    def context(self):
        """LLM에 전달할 확인된 기준일."""
        return {
            source: value.isoformat() if value else None
            for source, value in self.snapshot_dates.items()
        }

    def _sources(self, intent):
        scope = intent.get("product_scope")

        # 현재 query_builder도 etf를 국내 ETF로 처리한다.
        if scope == "etf":
            scope = "etf_domestic"

        sources = {scope}

        # 이 관계들은 구성종목 데이터의 시점도 영향을 준다.
        for condition in intent.get("graph_conditions") or []:
            relation = condition.get("relation")

            if relation == "holds":
                sources.add("holdings")

            if relation == "hasTheme" and scope == "bond":
                sources.update({"etf_domestic", "holdings"})

            if relation in {"belongsToGroup", "sameGroupAs"}:
                if scope == "etf_domestic":
                    sources.add("holdings")

        return sources

    def check(self, intent):
        """
        통과: None
        중단: (status, 사용자 안내문)
        """
        temporal = intent.get("temporal")

        if not isinstance(temporal, dict):
            return (
                "clarify",
                "조회 시점을 파악하지 못했습니다. "
                "저장된 데이터 조회인지, 특정 날짜의 정보 조회인지 알려주세요.",
            )

        kind = temporal.get("kind")

        if kind == "prediction":
            return (
                "unanswerable",
                "보유 데이터로 미래 가격이나 수익률을 예측할 수 없습니다. "
                "저장된 상품 정보와 과거 수익률은 조회할 수 있습니다.",
            )

        if kind == "realtime":
            return (
                "unanswerable",
                "현재 서비스는 실시간 데이터에 연결되어 있지 않습니다. "
                "저장된 데이터 기준의 상품 정보는 조회할 수 있습니다.",
            )

        if kind in {"snapshot", "scheduled"}:
            return None

        if kind != "observed":
            return (
                "clarify",
                "저장된 상품 정보 조회인지, 특정 시점의 정보 조회인지 "
                "조금 더 구체적으로 알려주세요.",
            )

        start = parse_date(temporal.get("start_date"))
        end = parse_date(temporal.get("end_date"))

        if start is None or end is None or start > end:
            return (
                "clarify",
                "조회하려는 날짜 또는 기간을 구체적으로 알려주세요.",
            )

        sources = self._sources(intent)
        dates = [self.snapshot_dates.get(source) for source in sources]

        if any(value is None for value in dates):
            return (
                "unanswerable",
                "이 질문에 필요한 데이터의 기준일이 모두 확인되지 않아 "
                "요청하신 시점의 정보라고 확정해 답할 수 없습니다. "
                "시점을 지정하지 않은 저장 데이터 조회는 가능합니다.",
            )

        available = ", ".join(
            sorted({value.isoformat() for value in dates})
        )

        # 여러 데이터가 모두 요청 날짜의 스냅샷이어야 통과한다.
        if start == end and all(value == end for value in dates):
            return None

        return (
            "unanswerable",
            f"이 질문에 사용하는 데이터의 확인된 기준일은 {available}입니다. "
            "요청하신 날짜·기간의 관측값을 조회하는 기능은 제공하지 않습니다. "
            "저장된 기간별 수익률과 상품 정보는 조회할 수 있습니다.",
        )

    def answer_note(self, intent):
        """저장값을 오늘의 정보처럼 표현하지 않도록 답변에 전달."""
        sources = self._sources(intent)

        labels = {
            "bond": "채권",
            "etf_domestic": "국내 ETF",
            "etf_overseas": "해외 ETF",
            "fund": "펀드",
            "holdings": "구성종목",
        }

        parts = []

        for source in sorted(sources, key=str):
            snapshot = self.snapshot_dates.get(source)
            label = labels.get(source, "상품")

            if snapshot:
                parts.append(f"{label} 데이터 기준일: {snapshot.isoformat()}")
            else:
                parts.append(f"{label} 데이터 기준일: 미확인")

        return (
            "; ".join(parts)
            + ". 조회 결과는 저장된 데이터이며 오늘·현재의 실시간 정보로 표현하지 마세요."
        )
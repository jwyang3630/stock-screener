"""전체 시장 스캔용 계산 모듈 (Streamlit과 무관한 순수 pandas 코드).

SEC의 Financial Statement Data Sets(분기별 zip)에서 10-K 공시의 재무 숫자를 뽑아
기업별 품질 지표(ROIC 등)를 계산하고, 주가가 주어지면 가격 지표(이익수익률 등)를 계산합니다.
금액 단위는 모두 USD(달러)입니다.
"""
import os
import tempfile
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import requests

FSDS_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip"
TICKER_URL = "https://www.sec.gov/files/company_tickers.json"

# 손익·현금흐름(1년치, qtrs=4) 항목: 우선순위대로 첫 번째로 값이 있는 태그를 사용
FLOW_TAGS = {
    "rev": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "ebit": ["OperatingIncomeLoss"],
    "ni": ["NetIncomeLoss", "ProfitLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
}
# 재무상태표(시점, qtrs=0) 항목
INSTANT_TAGS = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash_a": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "debt_total": ["LongTermDebt"],
    "debt_lt": ["LongTermDebtNoncurrent"],
    "debt_cur": ["LongTermDebtCurrent", "DebtCurrent"],
    "stb": ["ShortTermBorrowings"],
    "cp": ["CommercialPaper"],
}
ALL_TAGS = {t for v in list(FLOW_TAGS.values()) + list(INSTANT_TAGS.values()) for t in v}


def default_quarters(today=None):
    """오늘 기준으로 이미 공개됐을 가능성이 큰 최근 4개 분기 이름을 돌려줍니다 (예: 2025q4..2026q3)."""
    today = today or date.today()
    y, q = today.year, (today.month - 1) // 3 + 1  # 현재 분기
    out = []
    for _ in range(4):
        q -= 1
        if q == 0:
            q, y = 4, y - 1
        out.append(f"{y}q{q}")
    return list(reversed(out))


def download_quarter(q, user_agent, dest_dir, timeout=120):
    """분기 zip을 dest_dir에 내려받고 경로를 돌려줍니다. 키가 필요 없지만 SEC는 연락처가 든 User-Agent를 요구합니다."""
    url = FSDS_URL.format(q=q)
    path = os.path.join(dest_dir, f"{q}.zip")
    try:
        with requests.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                raise RuntimeError(f"{q}: SEC 다운로드 실패 (HTTP {r.status_code})")
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
    except requests.RequestException as e:
        raise RuntimeError(f"{q}: 네트워크 오류 ({type(e).__name__})")
    return path


def fetch_ticker_map(user_agent, timeout=60):
    """CIK(int) -> 티커 사전. 같은 CIK에 티커가 여러 개면 목록에서 먼저 나온 것을 씁니다."""
    try:
        r = requests.get(TICKER_URL, headers={"User-Agent": user_agent}, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"티커 목록 네트워크 오류 ({type(e).__name__})")
    if r.status_code != 200:
        raise RuntimeError(f"티커 목록 다운로드 실패 (HTTP {r.status_code})")
    out = {}
    for item in r.json().values():
        cik = int(item["cik_str"])
        out.setdefault(cik, item["ticker"])
    return out


def _series_for(df, tags):
    """df(adsh, tag, value)에서 태그 우선순위대로 adsh별 값을 합칩니다."""
    out = None
    for t in tags:
        s = df.loc[df["tag"] == t].drop_duplicates("adsh").set_index("adsh")["value"]
        out = s if out is None else out.combine_first(s)
    return out if out is not None else pd.Series(dtype=float)


def extract_quarter(zip_path, forms=("10-K",)):
    """분기 zip 하나에서 10-K 공시별 핵심 재무 숫자를 뽑아 DataFrame(공시 1건=1행)으로 돌려줍니다."""
    with zipfile.ZipFile(zip_path) as z:
        names = {n.lower(): n for n in z.namelist()}
        if "sub.txt" not in names or "num.txt" not in names:
            raise RuntimeError("zip 안에 sub.txt 또는 num.txt가 없습니다 (파일 형식이 달라졌을 수 있음)")
        with z.open(names["sub.txt"]) as f:
            sub = pd.read_csv(
                f, sep="\t", dtype=str,
                usecols=lambda c: c in {"adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "filed"},
            )
        sub = sub[sub["form"].isin(forms)].copy()
        if sub.empty:
            return pd.DataFrame()
        adsh_set = set(sub["adsh"])

        parts = []
        with z.open(names["num.txt"]) as f:
            reader = pd.read_csv(
                f, sep="\t", dtype=str, chunksize=500_000,
                usecols=lambda c: c in {"adsh", "tag", "ddate", "qtrs", "uom", "segments", "coreg", "value"},
            )
            for chunk in reader:
                m = chunk["adsh"].isin(adsh_set) & chunk["tag"].isin(ALL_TAGS) & (chunk["uom"] == "USD")
                chunk = chunk[m]
                if "segments" in chunk.columns:
                    chunk = chunk[chunk["segments"].isna()]
                if "coreg" in chunk.columns:
                    chunk = chunk[chunk["coreg"].isna()]
                if len(chunk):
                    parts.append(chunk[["adsh", "tag", "ddate", "qtrs", "value"]])
    if not parts:
        return pd.DataFrame()

    num = pd.concat(parts, ignore_index=True)
    num["value"] = pd.to_numeric(num["value"], errors="coerce")
    num["qtrs"] = pd.to_numeric(num["qtrs"], errors="coerce")
    num["ddate"] = pd.to_datetime(num["ddate"], format="%Y%m%d", errors="coerce")
    num = num.dropna(subset=["value", "qtrs", "ddate"])
    sub["period"] = pd.to_datetime(sub["period"], format="%Y%m%d", errors="coerce")
    num = num.merge(sub[["adsh", "period"]], on="adsh", how="inner")

    cur_flow = num[(num["ddate"] == num["period"]) & (num["qtrs"] == 4)]
    days_back = (num["period"] - num["ddate"]).dt.days
    prior_flow = num[(num["qtrs"] == 4) & days_back.between(345, 385)]
    cur_inst = num[(num["ddate"] == num["period"]) & (num["qtrs"] == 0)]

    res = pd.DataFrame(index=sub["adsh"].values)
    for k, tags in FLOW_TAGS.items():
        res[k] = _series_for(cur_flow, tags)
    res["revp"] = _series_for(prior_flow, FLOW_TAGS["rev"])
    res["nip"] = _series_for(prior_flow, FLOW_TAGS["ni"])
    inst = {k: _series_for(cur_inst, tags) for k, tags in INSTANT_TAGS.items()}
    res["equity"] = inst["equity"]
    res["cash"] = inst["cash_a"].reindex(res.index).fillna(0) + inst["sti"].reindex(res.index).fillna(0)
    base = inst["debt_total"].reindex(res.index)
    base = base.where(base.notna(), inst["debt_lt"].reindex(res.index).fillna(0) + inst["debt_cur"].reindex(res.index).fillna(0))
    res["debt"] = base + inst["stb"].reindex(res.index).fillna(0) + inst["cp"].reindex(res.index).fillna(0)

    res = res.reset_index().rename(columns={"index": "adsh"})
    out = sub.merge(res, on="adsh", how="inner")
    out["cik"] = pd.to_numeric(out["cik"], errors="coerce")
    out["sic"] = pd.to_numeric(out["sic"], errors="coerce")
    out["filed"] = pd.to_datetime(out["filed"], format="%Y%m%d", errors="coerce")
    out["capex"] = out["capex"].abs()
    return out.drop(columns=["form"])


def build_universe(frames, tax=0.21, exclude_financials=True):
    """여러 분기의 결과를 합쳐 기업(CIK)별 최신 10-K 한 건만 남기고 품질 지표를 계산합니다."""
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("filed").drop_duplicates("cik", keep="last")
    need = ["rev", "revp", "ebit", "ni", "nip", "cfo", "capex", "equity"]
    df = df.dropna(subset=need).copy()
    df["debt"] = df["debt"].fillna(0.0)
    df["cash"] = df["cash"].fillna(0.0)
    if exclude_financials:
        # SIC 6000~6799: 은행, 보험, 증권, 부동산 투자 등 (ROIC·FCF 개념이 맞지 않음)
        df = df[~df["sic"].between(6000, 6799)]
    return compute_quality(df, tax)


def compute_quality(df, tax=0.21):
    df = df.copy()
    df["net_debt"] = df["debt"] - df["cash"]
    invested = df["equity"] + df["net_debt"]
    df["roic"] = np.where((df["ebit"] > 0) & (invested > 0), df["ebit"] * (1 - tax) / invested, np.nan)
    df["fcf"] = df["cfo"] - df["capex"]
    lev_ok = (df["net_debt"] <= 0) | ((df["ebit"] > 0) & (df["net_debt"] / df["ebit"].where(df["ebit"] > 0) <= 3))
    df["quality"] = (
        (df["roic"] >= 0.12).astype(int)
        + (df["cfo"] > df["ni"]).astype(int)
        + (df["rev"] > df["revp"]).astype(int)
        + (df["ni"] > df["nip"]).astype(int)
        + lev_ok.astype(int)
    )
    return df


def dcf_fair_price(fcf, net_debt, shares, g, r, tg):
    """5년 성장 후 영구 성장 DCF로 주당 적정가를 구합니다. 계산 불가면 None."""
    if fcf is None or fcf <= 0 or r <= tg or shares <= 0:
        return None
    pv, f = 0.0, fcf
    for t in range(1, 6):
        f *= 1 + g
        pv += f / (1 + r) ** t
    pv += f * (1 + tg) / (r - tg) / (1 + r) ** 5
    return (pv - net_debt) / shares


def add_valuation(df, prices, g=0.05, r=0.10, tg=0.03, mos_req=0.30):
    """prices: {티커: (price, marketCap)}. 가격이 있는 종목만 남겨 가격 지표와 순위를 계산합니다."""
    df = df.copy()
    df["price"] = df["ticker"].map(lambda t: prices.get(t, (np.nan, np.nan))[0])
    df["mcap"] = df["ticker"].map(lambda t: prices.get(t, (np.nan, np.nan))[1])
    df = df.dropna(subset=["price", "mcap"])
    df = df[(df["price"] > 0) & (df["mcap"] > 0)].copy()
    if df.empty:
        return df
    df["shares"] = df["mcap"] / df["price"]
    df["ev"] = df["mcap"] + df["net_debt"]
    df["ey"] = np.where(df["ev"] > 0, df["ebit"] / df["ev"], np.nan)
    df["fcfy"] = df["fcf"] / df["mcap"]
    fair = [dcf_fair_price(a, b, c, g, r, tg) for a, b, c in zip(df["fcf"], df["net_debt"], df["shares"])]
    df["fair"] = pd.Series(fair, index=df.index, dtype="float64")
    df["margin"] = np.where(df["fair"] > 0, (df["fair"] - df["price"]) / df["fair"], np.nan)
    df["rank_score"] = df["roic"].rank(ascending=False) + df["ey"].rank(ascending=False)
    cheap = df["margin"] >= mos_req
    good = df["quality"] >= 4
    df["verdict"] = np.select(
        [good & cheap, good, cheap],
        ["✅ 후보", "👀 품질 좋음·비쌈", "⚠️ 싸지만 품질 의심"],
        default="❌ 보류",
    )
    return df.sort_values("rank_score", na_position="last")

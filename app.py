"""저평가 기업 스크리너 (Streamlit)

종목 티커를 입력하면 Financial Modeling Prep(FMP)에서 재무 데이터를 가져와
품질(ROIC 등)과 가격(이익수익률, FCF 수익률, 간이 DCF)을 계산하고 순위를 매깁니다.
공부용 후보 선별 도구이며 투자 권유가 아닙니다.
"""
import requests
import pandas as pd
import streamlit as st

BASE = "https://financialmodelingprep.com/stable"

st.set_page_config(page_title="저평가 기업 스크리너", layout="wide")


class FMPError(Exception):
    pass


def pick(d, keys, default=None):
    """여러 후보 필드명 중 값이 있는 첫 번째를 반환."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fmp_get(path, symbol, key, extra=None):
    params = {"symbol": symbol, "apikey": key}
    if extra:
        params.update(extra)
    # 주의: requests 예외 메시지에는 URL(=API 키)이 들어가므로 절대 그대로 화면에 내보내지 않습니다.
    try:
        r = requests.get(f"{BASE}/{path}", params=params, timeout=20)
    except requests.RequestException:
        raise FMPError("네트워크 오류가 발생했습니다. 잠시 후 다시 시도하세요.")
    if r.status_code == 402:
        raise FMPError("이 종목은 무료 플랜에서 제공되지 않습니다 (유료 플랜 전용 종목)")
    if r.status_code in (401, 403):
        raise FMPError(f"API 키가 잘못됐거나 '{path}' 항목이 무료 플랜에서 제공되지 않습니다 (HTTP {r.status_code})")
    if r.status_code == 429:
        raise FMPError("호출 한도를 초과했습니다. 잠시 후 또는 내일 다시 시도하세요.")
    if r.status_code >= 400:
        raise FMPError(f"FMP 서버 오류 (HTTP {r.status_code})")
    try:
        data = r.json()
    except ValueError:
        raise FMPError("FMP 응답을 읽을 수 없습니다")
    if isinstance(data, dict) and ("Error Message" in data or "message" in data):
        msg = str(data.get("Error Message") or data.get("message")).replace(key, "***")
        raise FMPError(msg)
    return data


def fetch_company(symbol, key):
    """FMP에서 한 종목의 입력 숫자를 모읍니다. 금액은 백만 단위로 환산."""
    quote = fmp_get("quote", symbol, key)
    income = fmp_get("income-statement", symbol, key, {"period": "annual", "limit": 2})
    balance = fmp_get("balance-sheet-statement", symbol, key, {"period": "annual", "limit": 1})
    cash = fmp_get("cash-flow-statement", symbol, key, {"period": "annual", "limit": 1})

    if not quote or not income or not balance or not cash:
        raise FMPError("데이터가 비어 있습니다 (티커 확인 또는 무료 플랜 범위 밖)")

    q, i0 = quote[0], income[0]
    i1 = income[1] if len(income) > 1 else {}
    b, c = balance[0], cash[0]
    M = 1_000_000.0

    price = pick(q, ["price"])
    mcap = pick(q, ["marketCap"])
    if not price or not mcap:
        raise FMPError("주가 또는 시가총액을 가져오지 못했습니다")

    debt = pick(b, ["totalDebt"])
    if debt is None:
        debt = (pick(b, ["shortTermDebt"], 0) or 0) + (pick(b, ["longTermDebt"], 0) or 0)

    out = {
        "name": symbol,
        "price": float(price),
        "mcap": float(mcap) / M,
        "shares": float(mcap) / float(price) / M,
        "debt": float(debt) / M,
        "cash": float(pick(b, ["cashAndShortTermInvestments", "cashAndCashEquivalents"], 0) or 0) / M,
        "equity": float(pick(b, ["totalStockholdersEquity", "totalEquity"], 0) or 0) / M,
        "ebit": pick(i0, ["operatingIncome"]),
        "ni": pick(i0, ["netIncome"]),
        "nip": pick(i1, ["netIncome"]),
        "rev": pick(i0, ["revenue"]),
        "revp": pick(i1, ["revenue"]),
        "cfo": pick(c, ["netCashProvidedByOperatingActivities", "operatingCashFlow"]),
        "capex": pick(c, ["capitalExpenditure"]),
        "period": pick(i0, ["date"], ""),
    }
    for k in ["ebit", "ni", "nip", "rev", "revp", "cfo", "capex"]:
        if out[k] is None:
            raise FMPError(f"'{k}' 항목을 가져오지 못했습니다 (금융업 등 항목 구조가 다른 기업일 수 있음)")
        out[k] = float(out[k]) / M
    out["capex"] = abs(out["capex"])  # FMP는 설비투자를 음수로 줌
    return out


def analyze(v, tax, g, r, tg, mos_req):
    mcap = v["mcap"]
    net_debt = v["debt"] - v["cash"]
    ev = mcap + net_debt
    invested = v["equity"] + net_debt
    fcf = v["cfo"] - v["capex"]

    roic = v["ebit"] * (1 - tax) / invested if invested > 0 and v["ebit"] > 0 else None
    ey = v["ebit"] / ev if ev > 0 else None
    fcfy = fcf / mcap if mcap > 0 else None
    lev = net_debt / v["ebit"] if v["ebit"] > 0 else None

    iv = mos = None
    if fcf > 0 and r > tg:
        pv, f = 0.0, fcf
        for t in range(1, 6):
            f *= 1 + g
            pv += f / (1 + r) ** t
        tv = f * (1 + tg) / (r - tg)
        pv += tv / (1 + r) ** 5
        iv = (pv - net_debt) / v["shares"]
        if iv > 0:
            mos = (iv - v["price"]) / iv

    checks = [
        ("ROIC 12% 이상", roic is not None and roic >= 0.12),
        ("영업현금흐름 > 순이익", v["cfo"] > v["ni"]),
        ("매출 증가", v["rev"] > v["revp"]),
        ("순이익 증가", v["ni"] > v["nip"]),
        ("순부채 ≤ 영업이익의 3배", net_debt <= 0 or (lev is not None and lev <= 3)),
    ]
    score = sum(1 for _, ok in checks if ok)
    cheap = mos is not None and mos >= mos_req
    good = score >= 4
    if good and cheap:
        verdict = "✅ 후보"
    elif good:
        verdict = "👀 품질 좋음·비쌈"
    elif cheap:
        verdict = "⚠️ 싸지만 품질 의심"
    else:
        verdict = "❌ 보류"
    return {
        "roic": roic, "ey": ey, "fcfy": fcfy, "iv": iv, "mos": mos,
        "score": score, "checks": checks, "verdict": verdict, "fcf": fcf, "net_debt": net_debt, "ev": ev,
    }


# ---------------- UI ----------------
st.title("저평가 기업 스크리너")
st.caption("미국 상장 기업의 재무 숫자로 '괜찮은 기업(품질)'과 '싼 가격(가치)'을 계산해 순위를 매깁니다. 후보를 추리는 공부용 도구이며 투자 권유가 아닙니다.")

with st.sidebar:
    st.header("설정")
    secret_key = ""
    try:
        secret_key = st.secrets.get("FMP_API_KEY", "")
    except Exception:
        secret_key = ""
    if secret_key:
        api_key = secret_key
        st.success("저장된 API 키를 사용합니다")
    else:
        api_key = st.text_input("FMP API 키", type="password", help="financialmodelingprep.com에서 무료 가입 후 발급")

    st.subheader("가정값")
    tax = st.number_input("법인세율 (%)", 0.0, 50.0, 21.0, 1.0) / 100
    g = st.number_input("향후 5년 FCF 성장률 (%)", -10.0, 30.0, 5.0, 0.5) / 100
    r = st.number_input("할인율 (%)", 1.0, 30.0, 10.0, 0.5) / 100
    tg = st.number_input("영구 성장률 (%)", 0.0, 6.0, 3.0, 0.5) / 100
    mos_req = st.number_input("요구 안전마진 (%)", 0.0, 90.0, 30.0, 5.0) / 100
    if r <= tg:
        st.error("할인율은 영구 성장률보다 커야 합니다")

tickers_text = st.text_area(
    "분석할 티커 (쉼표 또는 줄바꿈으로 구분)",
    value="AAPL, MSFT, JNJ, KO, PG",
    height=90,
)
st.caption("종목당 FMP 호출이 4번 쓰입니다. 무료 플랜은 하루 호출 한도가 있으니 한 번에 10~20종목 정도로 시작하세요. 같은 종목은 6시간 동안 저장돼 다시 호출하지 않습니다.")

if st.button("분석 시작", type="primary"):
    if not api_key:
        st.error("왼쪽에 FMP API 키를 입력해 주세요.")
        st.stop()
    if r <= tg:
        st.stop()

    symbols = []
    for s in tickers_text.replace("\n", ",").split(","):
        s = s.strip().upper()
        if s and s not in symbols:
            symbols.append(s)
    if not symbols:
        st.error("티커를 입력해 주세요.")
        st.stop()

    rows, details, errors = [], {}, {}
    bar = st.progress(0.0)
    for idx, sym in enumerate(symbols):
        try:
            v = fetch_company(sym, api_key)
            a = analyze(v, tax, g, r, tg, mos_req)
            details[sym] = (v, a)
            rows.append({
                "종목": sym, "판정": a["verdict"], "품질(5)": a["score"],
                "ROIC(%)": None if a["roic"] is None else a["roic"] * 100,
                "이익수익률(%)": None if a["ey"] is None else a["ey"] * 100,
                "FCF수익률(%)": None if a["fcfy"] is None else a["fcfy"] * 100,
                "안전마진(%)": None if a["mos"] is None else a["mos"] * 100,
                "현재가": v["price"], "적정주가": a["iv"],
            })
        except FMPError as e:
            errors[sym] = str(e)
            if "한도" in str(e):
                break
        except Exception as e:
            errors[sym] = f"오류: {e}".replace(api_key, "***")
        bar.progress((idx + 1) / len(symbols))
    bar.empty()

    if rows:
        df = pd.DataFrame(rows)
        df["순위점수"] = df["ROIC(%)"].rank(ascending=False) + df["이익수익률(%)"].rank(ascending=False)
        df = df.sort_values("순위점수", na_position="last").reset_index(drop=True)
        df.index = df.index + 1
        st.subheader("결과 (마법공식 순위: ROIC 순위 + 이익수익률 순위, 낮을수록 위)")
        st.dataframe(
            df,
            use_container_width=True,
            column_config={
                "ROIC(%)": st.column_config.NumberColumn(format="%.1f"),
                "이익수익률(%)": st.column_config.NumberColumn(format="%.1f"),
                "FCF수익률(%)": st.column_config.NumberColumn(format="%.1f"),
                "안전마진(%)": st.column_config.NumberColumn(format="%.1f"),
                "현재가": st.column_config.NumberColumn(format="%.2f"),
                "적정주가": st.column_config.NumberColumn(format="%.2f"),
                "순위점수": st.column_config.NumberColumn(format="%.0f"),
            },
        )
        st.download_button("CSV로 저장", df.to_csv(index=False).encode("utf-8-sig"), "screener_result.csv", "text/csv")

        st.subheader("종목별 상세 (가져온 숫자를 꼭 한 번 확인하세요)")
        for sym in df["종목"]:
            v, a = details[sym]
            with st.expander(f"{sym} — {a['verdict']}  (기준 회계연도 {v['period']})"):
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**가져온 숫자 (백만 달러)**")
                    st.write({
                        "주가": round(v["price"], 2), "시가총액": round(v["mcap"]), "총부채": round(v["debt"]),
                        "현금": round(v["cash"]), "자기자본": round(v["equity"]), "영업이익": round(v["ebit"]),
                        "순이익": round(v["ni"]), "전년 순이익": round(v["nip"]), "매출": round(v["rev"]),
                        "전년 매출": round(v["revp"]), "영업현금흐름": round(v["cfo"]), "설비투자": round(v["capex"]),
                        "FCF": round(a["fcf"]),
                    })
                with c2:
                    st.markdown("**품질 체크**")
                    for text, ok in a["checks"]:
                        st.write(("✔ " if ok else "✘ ") + text)

    if errors:
        st.subheader("가져오지 못한 종목")
        for sym, msg in errors.items():
            st.warning(f"{sym}: {msg}")

st.divider()
st.caption(
    "※ 은행·보험 같은 금융업은 ROIC와 FCF 개념이 맞지 않아 결과가 왜곡됩니다. "
    "싸 보이는 기업은 싼 이유가 있는 경우가 많으니 사업 내용, 일회성 손익, 경쟁 구도를 직접 확인하세요. "
    "품질 점수는 간이 5항목입니다."
)

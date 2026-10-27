"""전체 시장 스캔 페이지

1단계: SEC 재무 데이터(분기별 zip)에서 전체 상장사의 품질(ROIC 등)을 계산해 후보를 거릅니다. (키 불필요)
2단계: 후보 종목만 FMP로 주가를 가져와(종목당 1회 호출) 가격 지표와 순위를 계산합니다.
공부용 후보 선별 도구이며 투자 권유가 아닙니다.
"""
import os
import tempfile
import time

import pandas as pd
import requests
import streamlit as st

import scan_core as sc

BASE = "https://financialmodelingprep.com/stable"

st.set_page_config(page_title="전체 시장 스캔", layout="wide")


class FMPError(Exception):
    pass


# ---------------- 캐시 ----------------
@st.cache_data(ttl=24 * 3600, show_spinner=False)
def load_quarter(q, ua, ver=2):
    """분기 zip을 내려받아 10-K 숫자만 추출해 돌려줍니다 (zip은 바로 삭제)."""
    with tempfile.TemporaryDirectory() as d:
        path = sc.download_quarter(q, ua, d)
        return sc.extract_quarter(path)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def load_ticker_map(ua):
    return sc.fetch_ticker_map(ua)


@st.cache_resource
def quote_store():
    """티커 -> (조회시각, 결과). 앱이 재시작될 때까지 유지되며, 호출 횟수를 정확히 세기 위해 직접 관리합니다."""
    return {}


def fmp_quote(symbol, key):
    """FMP에서 주가와 시가총액을 가져옵니다. 결과: dict(status, price, mcap). 키·한도 문제는 예외."""
    try:
        r = requests.get(f"{BASE}/quote", params={"symbol": symbol, "apikey": key}, timeout=20)
    except requests.RequestException:
        return {"status": "error"}  # 예외 메시지에는 키가 든 URL이 있을 수 있어 절대 노출하지 않음
    if r.status_code == 402:
        return {"status": "paid"}
    if r.status_code in (401, 403):
        raise FMPError("API 키가 잘못됐거나 권한이 없습니다")
    if r.status_code == 429:
        raise FMPError("호출 한도를 초과했습니다")
    if r.status_code >= 400:
        return {"status": "error"}
    try:
        data = r.json()
    except ValueError:
        return {"status": "error"}
    if isinstance(data, dict) or not data:
        return {"status": "empty"}
    q = data[0]
    price, mcap = q.get("price"), q.get("marketCap")
    if not price or not mcap:
        return {"status": "empty"}
    return {"status": "ok", "price": float(price), "mcap": float(mcap)}


YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockScreenerStudy)"}


def yahoo_price(symbol):
    """Yahoo 공개 차트 주소에서 현재가만 가져옵니다(키 불필요). 시가총액은 주지 않으므로 SEC 표지의 주식수로 계산합니다."""
    sym = symbol.replace(".", "-")
    try:
        r = requests.get(YAHOO.format(sym=sym), params={"range": "5d", "interval": "1d"},
                         headers=YAHOO_HEADERS, timeout=20)
    except requests.RequestException as e:
        return {"status": "error", "why": f"네트워크 오류({type(e).__name__})"}
    if r.status_code == 404:
        return {"status": "empty", "why": "Yahoo에 없는 티커(404)"}
    if r.status_code in (401, 403, 429):
        return {"status": "blocked", "why": f"Yahoo가 접속 거절(HTTP {r.status_code})"}
    if r.status_code >= 400:
        return {"status": "error", "why": f"Yahoo 오류(HTTP {r.status_code})"}
    try:
        meta = r.json()["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
    except (ValueError, KeyError, IndexError, TypeError):
        return {"status": "empty", "why": "Yahoo 응답에 가격 없음"}
    if price <= 0:
        return {"status": "empty", "why": "Yahoo 가격이 0 이하"}
    return {"status": "ok", "price": price}


# ---------------- 화면 ----------------
st.title("전체 시장 스캔")
st.caption("SEC 공시 데이터로 미국 상장사 전체의 품질을 계산해 후보를 거른 뒤, 후보만 주가를 가져와 순위를 매깁니다. 공부용 도구이며 투자 권유가 아닙니다.")

with st.sidebar:
    st.header("설정")
    secret_key = ""
    try:
        secret_key = st.secrets.get("FMP_API_KEY", "")
    except Exception:
        secret_key = ""
    if secret_key:
        api_key = secret_key
        st.success("저장된 FMP 키를 사용합니다")
    else:
        api_key = st.text_input("FMP API 키", type="password")

    email = st.text_input(
        "SEC 요청용 연락 이메일",
        help="SEC는 자동 접속 시 연락처를 요구합니다. 본인 이메일을 입력하세요. SEC에만 전달되고 저장되지 않습니다.",
    )

    st.subheader("가정값")
    tax = st.number_input("법인세율 (%)", 0.0, 50.0, 21.0, 1.0) / 100
    g = st.number_input("향후 5년 FCF 성장률 (%)", -10.0, 30.0, 5.0, 0.5) / 100
    r = st.number_input("할인율 (%)", 1.0, 30.0, 10.0, 0.5) / 100
    tg = st.number_input("영구 성장률 (%)", 0.0, 6.0, 3.0, 0.5) / 100
    mos_req = st.number_input("요구 안전마진 (%)", 0.0, 90.0, 30.0, 5.0) / 100
    if r <= tg:
        st.error("할인율은 영구 성장률보다 커야 합니다")

ua = f"StockScreenerStudy {email.strip()}" if email.strip() else ""

# ---- 연결 테스트 ----
with st.expander("연결 테스트 (오류가 날 때 먼저 눌러 보세요)"):
    if st.button("SEC 접속 확인"):
        if not ua:
            st.error("왼쪽에 SEC 요청용 이메일을 먼저 입력하세요.")
        else:
            try:
                url = sc.FSDS_URL.format(q=sc.default_quarters()[-1])
                resp = requests.get(url, headers={"User-Agent": ua, "Range": "bytes=0-1023"}, timeout=30)
                st.write(f"분기 재무 데이터 파일: HTTP {resp.status_code}")
                resp2 = requests.get(sc.TICKER_URL, headers={"User-Agent": ua}, timeout=30)
                st.write(f"티커 목록: HTTP {resp2.status_code}")
                if resp.status_code in (200, 206) and resp2.status_code == 200:
                    st.success("SEC 접속이 정상입니다.")
                else:
                    st.warning("SEC가 접속을 거절했거나 주소가 바뀌었을 수 있습니다. 위 숫자를 알려주세요.")
            except requests.RequestException as e:
                st.error(f"네트워크 오류: {type(e).__name__}")

# ---- 1단계 ----
st.header("1단계. SEC 재무 데이터로 품질 계산 (키 불필요)")
quarters_text = st.text_input(
    "불러올 분기 (쉼표로 구분, 연간 보고서 10-K가 제출되는 최근 1년치를 권장)",
    value=", ".join(sc.default_quarters()),
)
st.caption("분기 파일 하나는 약 60MB이고 처음 불러올 때 몇 분 걸립니다. 한 번 불러오면 24시간 동안 저장됩니다.")

if st.button("SEC 데이터 불러오기", type="primary"):
    if not ua:
        st.error("왼쪽에 SEC 요청용 이메일을 먼저 입력하세요.")
        st.stop()
    qs = [q.strip().lower() for q in quarters_text.split(",") if q.strip()]
    frames, problems = [], []
    bar = st.progress(0.0, text="시작")
    for i, q in enumerate(qs):
        bar.progress(i / max(len(qs), 1), text=f"{q} 불러오는 중...")
        try:
            frames.append(load_quarter(q, ua))
        except Exception as e:
            problems.append(f"{q}: {e}")
    bar.empty()
    for p in problems:
        st.warning(p)
    uni = sc.build_universe(frames, tax=tax)
    if uni.empty:
        st.error("사용할 수 있는 데이터가 없습니다. 위의 경고 문구를 알려주세요.")
    else:
        st.session_state["universe"] = uni
        st.session_state["quarters_used"] = qs

uni = st.session_state.get("universe")
if uni is not None and not uni.empty:
    st.success(
        f"{len(uni):,}개 기업의 최신 10-K 숫자를 불러왔습니다 "
        f"(금융업 제외, 기준 분기: {', '.join(st.session_state.get('quarters_used', []))})."
    )

    # ---- 2단계 ----
    st.header("2단계. 후보 거르기")
    c1, c2, c3, c4 = st.columns(4)
    min_rev = c1.number_input("최소 매출 (백만 달러)", 0, 1_000_000, 1000, 100)
    min_roic = c2.number_input("최소 ROIC (%)", 0.0, 200.0, 15.0, 1.0) / 100
    min_q = c3.number_input("최소 품질 점수 (5점)", 0, 5, 4, 1)
    max_n = c4.number_input("주가를 조회할 최대 종목 수", 1, 240, 100, 10)

    short = uni[
        (uni["rev"] >= min_rev * 1e6)
        & (uni["roic"] >= min_roic)
        & (uni["quality"] >= min_q)
        & (uni["fcf"] > 0)
    ].sort_values(["quality", "roic"], ascending=[False, False])
    st.write(f"조건에 맞는 기업: **{len(short):,}개** (이 중 상위 {min(len(short), int(max_n))}개만 주가를 조회합니다)")
    short = short.head(int(max_n)).copy()
    if "shares_sec" in short.columns:
        st.caption(f"SEC에서 발행주식수를 찾은 후보: {int(short['shares_sec'].notna().sum())}/{len(short)}개")
    else:
        st.warning("이 데이터에는 발행주식수 정보가 없습니다. 1단계에서 'SEC 데이터 불러오기'를 다시 눌러 주세요.")

    view = pd.DataFrame({
        "회사": short["name"], "기준일": short["period"].dt.date, "매출(백만$)": (short["rev"] / 1e6).round(0),
        "ROIC(%)": (short["roic"] * 100).round(1), "품질(5)": short["quality"],
        "FCF(백만$)": (short["fcf"] / 1e6).round(0), "순부채(백만$)": (short["net_debt"] / 1e6).round(0),
    })
    st.dataframe(view, use_container_width=True, hide_index=True)

    # ---- 3단계 ----
    st.header("3단계. 주가를 가져와 가격 지표와 순위 계산")
    daily_cap = st.number_input("오늘 FMP에서 쓸 최대 호출 수 (무료 한도 250회)", 10, 250, 150, 10)
    yahoo_only = st.checkbox("FMP를 건너뛰고 Yahoo만 사용 (FMP 호출 0회)", value=False)
    use_yahoo = st.checkbox("FMP로 안 되는 종목은 Yahoo로 보충", value=True, disabled=yahoo_only)
    if yahoo_only:
        use_yahoo = True
    if st.button("주가 조회 시작"):
        if not api_key and not yahoo_only:
            st.error("FMP API 키가 필요합니다(또는 'Yahoo만 사용'을 체크하세요).")
            st.stop()
        if not ua:
            st.error("왼쪽에 SEC 요청용 이메일을 먼저 입력하세요.")
            st.stop()
        try:
            tmap = load_ticker_map(ua)
        except Exception as e:
            st.error(f"티커 목록을 가져오지 못했습니다: {e}")
            st.stop()
        short["ticker"] = short["cik"].astype("Int64").map(lambda c: tmap.get(int(c)) if pd.notna(c) else None)
        no_ticker = int(short["ticker"].isna().sum())
        short = short.dropna(subset=["ticker"])

        store = quote_store()
        sec_shares = dict(zip(short["ticker"], short["shares_sec"] if "shares_sec" in short.columns else [float("nan")] * len(short)))
        now = time.time()
        used, paid, failed, stopped = 0, [], [], None
        y_ok, y_fail, y_streak = 0, [], 0
        y_why = {}
        n_sh = int(pd.Series(list(sec_shares.values()), dtype="float64").notna().sum())
        bar = st.progress(0.0, text="주가 조회 중...")
        tickers = list(short["ticker"])
        for i, t in enumerate(tickers):
            res = {"status": "skipped"}
            if not yahoo_only:
                hit = store.get(t)
                if hit and now - hit[0] < 24 * 3600:
                    res = hit[1]
                else:
                    if used >= daily_cap:
                        stopped = "오늘 쓸 FMP 호출 수 한도에 도달해 멈췄습니다. 내일 이어서 하거나 한도를 늘리세요."
                        break
                    try:
                        res = fmp_quote(t, api_key)
                    except FMPError as e:
                        stopped = str(e)
                        break
                    used += 1
                    store[t] = (now, res)
                if res["status"] == "paid":
                    paid.append(t)
            if res["status"] != "ok":
                if use_yahoo:
                    yk = "Y:" + t
                    yhit = store.get(yk)
                    if yhit and now - yhit[0] < 6 * 3600:
                        yres = yhit[1]
                    else:
                        yres = yahoo_price(t)
                        time.sleep(0.25)
                        if yres["status"] != "blocked":
                            store[yk] = (now, yres)
                    if yres["status"] == "blocked":
                        y_streak += 1
                        if y_streak >= 5:
                            stopped = "Yahoo가 접속을 막고 있습니다(차단 또는 요청 과다). 잠시 뒤 다시 시도하세요."
                            break
                    else:
                        y_streak = 0
                    if yres["status"] == "ok" and sec_shares.get(t) and sec_shares[t] == sec_shares[t]:
                        y_ok += 1
                    else:
                        y_fail.append(t)
                        why = yres.get("why") if yres["status"] != "ok" else "주가는 받았으나 SEC 주식수 없음"
                        y_why[why] = y_why.get(why, 0) + 1
                elif res["status"] != "paid":
                    failed.append(t)
            bar.progress((i + 1) / max(len(tickers), 1), text=f"{t} ({i + 1}/{len(tickers)})")
        bar.empty()

        prices = {t: (v[1]["price"], v[1]["mcap"]) for t, v in store.items()
                  if not t.startswith("Y:") and v[1].get("status") == "ok"}
        for t, v in store.items():
            if t.startswith("Y:") and v[1].get("status") == "ok":
                tk = t[2:]
                sh = sec_shares.get(tk)
                if tk not in prices and sh and sh == sh:
                    prices[tk] = (v[1]["price"], v[1]["price"] * sh)
        prices = {t: p for t, p in prices.items() if t in set(tickers)}
        out = sc.add_valuation(short, prices, g=g, r=r, tg=tg, mos_req=mos_req)

        msg = f"FMP 호출 {used}회 사용 · 주가를 가져온 종목 {len(out)}개"
        if y_ok:
            msg += f" · 그중 Yahoo 보충 {y_ok}개(시가총액은 SEC 표지 주식수×주가)"
        if paid and not y_ok:
            msg += f" · 무료 플랜에서 제외된 종목 {len(paid)}개"
        if y_fail:
            msg += f" · Yahoo로도 못 가져온 종목 {len(y_fail)}개"
        if no_ticker:
            msg += f" · 티커를 찾지 못한 기업 {no_ticker}개"
        st.info(msg)
        if stopped:
            st.warning(stopped)
        if out.empty:
            st.error("주가를 가져온 종목이 없습니다.")
        else:
            table = pd.DataFrame({
                "티커": out["ticker"], "회사": out["name"], "판정": out["verdict"], "품질(5)": out["quality"],
                "ROIC(%)": out["roic"] * 100, "이익수익률(%)": out["ey"] * 100, "FCF수익률(%)": out["fcfy"] * 100,
                "안전마진(%)": out["margin"] * 100, "현재가": out["price"], "적정주가": out["fair"],
                "순위점수": out["rank_score"], "기준일": out["period"].dt.date,
            }).reset_index(drop=True)
            table.index = table.index + 1
            st.subheader("결과 (마법공식 순위: ROIC 순위 + 이익수익률 순위, 낮을수록 위)")
            st.dataframe(
                table, use_container_width=True,
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
            st.download_button("CSV로 저장", table.to_csv(index=False).encode("utf-8-sig"), "market_scan_result.csv", "text/csv")
        if y_fail:
            st.warning("Yahoo 실패 원인 집계: " + " / ".join(f"{k} {v}개" for k, v in y_why.items())
                       + f" · SEC 주식수가 있는 후보: {n_sh}/{len(tickers)}개")
        if y_fail:
            with st.expander(f"Yahoo로도 가져오지 못한 종목 {len(y_fail)}개 (주가 없음 또는 SEC 주식수 없음)"):
                st.write(", ".join(y_fail))
        if paid:
            with st.expander(f"FMP 무료 플랜에서 제외된 종목 {len(paid)}개"):
                st.write(", ".join(paid))

st.divider()
st.caption(
    "※ SEC 숫자는 각 기업의 가장 최근 연간 보고서(10-K) 기준이며 태그 이름이 회사마다 달라 일부 항목은 비어 있거나 부정확할 수 있습니다. "
    "리스·우선주·소수지분은 반영하지 않았습니다. 자동차·유통 등 금융 자회사가 있는 기업은 부채와 현금흐름이 왜곡될 수 있으니 "
    "후보로 나온 종목은 반드시 공시와 사업 내용을 직접 확인하세요."
)

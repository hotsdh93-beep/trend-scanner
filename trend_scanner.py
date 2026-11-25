#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
트렌드 템플릿 스캐너 (마크 미너비니 Trend Template + VCP)

사용법
  python trend_scanner.py kr          한국장(코스피·코스닥) 스캔
  python trend_scanner.py us          미국장 스캔
  python trend_scanner.py all         둘 다
  python trend_scanner.py all --open  스캔 후 결과 화면 열기
  python trend_scanner.py report      스캔 없이 결과 화면만 다시 만들기

결과
  트렌드스캐너.html         결과 화면 (더블클릭해서 브라우저로 열기)
  data/                     스캔 결과·기록 (CSV, JSON)
  logs/scan.log             실행 기록

시세는 Yahoo Finance 무료 데이터(yfinance)를 씁니다. 매매 추천이 아니라 차트를 직접 확인할 후보를 좁히는 도구입니다.
"""
import argparse
import datetime as dt
import html
import json
import logging
import math
import sys
import time
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LOGS = BASE / "logs"
REPORT = BASE / "트렌드스캐너.html"

# ---------------------------------------------------------------- 설정
SETTINGS = {
    "kr": {"name": "한국", "min_cap": 3e11},   # 시가총액 3,000억 원 이상
    "us": {"name": "미국", "min_cap": 2e9,     # 시가총액 20억 달러 이상
           "min_price": 10, "min_avg_volume": 200_000,
           "exchanges": {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS"}},
}
CONFIG = BASE / "config.json"   # 앱(설정 화면)에서 바꾸는 값. 없으면 위 기본값 사용


def load_config():
    """config.json의 시총 기준을 SETTINGS에 반영한다."""
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return
    if c.get("kr_min_cap_eok"):
        SETTINGS["kr"]["min_cap"] = float(c["kr_min_cap_eok"]) * 1e8      # 억 원
    global PATTERN_RS_MIN, RS_MIN, MAX_FROM_HIGH, MAX_ABOVE_50, EPS_GROWTH_MIN, REV_GROWTH_MIN
    if c.get("pattern_rs_min"):
        PATTERN_RS_MIN = int(c["pattern_rs_min"])
    RS_MIN = int(c.get("rs_min", RS_MIN))
    MAX_FROM_HIGH = float(c.get("max_from_high", MAX_FROM_HIGH))
    MAX_ABOVE_50 = float(c.get("max_above_50", MAX_ABOVE_50))
    EPS_GROWTH_MIN = float(c.get("eps_growth_min", EPS_GROWTH_MIN))
    REV_GROWTH_MIN = float(c.get("rev_growth_min", REV_GROWTH_MIN))
    if c.get("us_min_cap_eok_usd"):
        SETTINGS["us"]["min_cap"] = float(c["us_min_cap_eok_usd"]) * 1e8  # 억 달러


RS_MIN = 80            # 8번째 조건: RS 80 이상 (config.json의 rs_min)
MAX_FROM_HIGH = 15     # 가격 위치: 52주 고점에서 15% 이내 (max_from_high)
MAX_ABOVE_50 = 25      # 가격 위치: 50일선보다 25% 넘게 오르면 과열로 제외 (max_above_50)
EPS_GROWTH_MIN = 25    # 실적: 최근 분기 순이익 전년 동기 대비 +25% 이상 (eps_growth_min)
REV_GROWTH_MIN = 20    # 실적: 최근 분기 매출 전년 동기 대비 +20% 이상 (rev_growth_min)
KEEP_NEAR_RS = 80      # 7개 충족 종목은 RS 80 이상만 결과표에 남김
PATTERN_RS_MIN = 85    # VCP·베이스 패턴 후보는 RS 85 이상만 (config.json의 pattern_rs_min으로 변경)
BATCH = 80             # 한 번에 내려받을 종목 수

log = logging.getLogger("scanner")


# ---------------------------------------------------------------- 스캔 대상
def _clean_name(market, name, sym):
    import re
    n = (name or sym).strip()
    if market == "kr":
        n = re.sub(r"\(주\)|㈜|주식회사|\(유\)", "", n)
        n = re.sub(r"\s+", " ", n).strip()
    else:
        for _ in range(3):
            n = re.sub(r",?\s+(Inc\.?|Incorporated|Corporation|Corp\.?|Holdings?|Ltd\.?|Limited|plc|PLC|N\.V\.|S\.A\.|SE|AG|Co\.|Company|L\.P\.|LP|Class [A-C])$", "", n, flags=re.I)
            n = re.sub(r"\s+(&|and)$", "", n, flags=re.I).strip()
    return n or sym


def _screen_page(market, offset, min_cap):
    """Yahoo 스크리너에서 시가총액 순으로 250개씩 받아온다. 한국은 한글 종목명을 받으려고 ko-KR로 요청."""
    body = {
        "size": 250, "offset": offset, "sortField": "intradaymarketcap", "sortType": "DESC",
        "quoteType": "EQUITY", "userId": "", "userIdType": "guid",
        "query": {"operator": "AND", "operands": [
            {"operator": "EQ", "operands": ["region", market]},
            {"operator": "GT", "operands": ["intradaymarketcap", min_cap]}]},
    }
    try:
        from yfinance.data import YfData
        params = {"corsDomain": "finance.yahoo.com", "formatted": "false",
                  "lang": "ko-KR" if market == "kr" else "en-US",
                  "region": "KR" if market == "kr" else "US"}
        resp = YfData().post("https://query1.finance.yahoo.com/v1/finance/screener",
                             data=json.dumps(body, separators=(",", ":")), params=params)
        resp.raise_for_status()
        return resp.json()["finance"]["result"][0]
    except Exception as e:  # yfinance 내부 구조가 바뀐 경우 공개 함수로 대체 (종목명은 영문)
        log.warning("스크리너 직접 호출 실패(%s), yf.screen으로 대체", e)
        from yfinance import EquityQuery as Q
        q = Q("and", [Q("eq", ["region", market]), Q("gt", ["intradaymarketcap", min_cap])])
        return yf.screen(q, offset=offset, size=250, sortField="intradaymarketcap", sortAsc=False)


KRX_CACHE = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/listing/krx/{}.csv"


def krx_listing():
    """KRX 공식 상장 종목 목록(종목명·시장·시가총액·당일 시세). FinanceDataReader가 쓰는 공개 캐시에서 가장 최근 날짜를 받는다."""
    import io
    import urllib.request
    today = dt.date.today()
    for back in range(10):
        d = (today - dt.timedelta(days=back)).isoformat()
        try:
            req = urllib.request.Request(KRX_CACHE.format(d), headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            df = pd.read_csv(io.BytesIO(raw), dtype={"Code": str}, index_col=0)
        except Exception:
            continue
        # 장 시작 전에 만들어진 그날 파일은 시가총액·종가가 비어 있을 수 있음 → 값이 채워진 가장 최근 파일을 쓴다
        filled = (pd.to_numeric(df.get("Marcap"), errors="coerce").fillna(0) > 0) & (pd.to_numeric(df.get("Close"), errors="coerce").fillna(0) > 0)
        if len(df) > 1000 and int(filled.sum()) > 1000:
            df["date"] = d
            return df
    return None


def get_universe(market):
    cfg = SETTINGS[market]
    if market == "kr":
        # 종목명·시장 구분은 KRX 공식 목록을 쓴다 (Yahoo의 한글 종목명은 사명 변경이 반영 안 된 경우가 있음)
        lst = krx_listing()
        if lst is not None:
            out = []
            for r in lst.itertuples():
                code = str(r.Code).zfill(6)
                if r.Market not in ("KOSPI", "KOSDAQ", "KOSDAQ GLOBAL") or code[5] != "0" or "스팩" in str(r.Name):
                    continue
                if not (r.Marcap >= cfg["min_cap"]):
                    continue
                kospi = r.Market == "KOSPI"
                out.append({"s": code + (".KS" if kospi else ".KQ"), "n": str(r.Name), "ex": "KSC" if kospi else "KOE",
                            "cap": float(r.Marcap), "krx": (r.date, r.Open, r.High, r.Low, r.Close, r.Volume)})
            log.info("[kr] KRX 목록(%s) 사용", lst["date"].iloc[0])
            return out
        log.warning("[kr] KRX 목록을 받지 못해 Yahoo 목록을 사용합니다 (종목명이 옛 이름일 수 있음)")
    out, offset = [], 0
    while offset < 8000:
        res = None
        for attempt in range(3):
            try:
                res = _screen_page(market, offset, cfg["min_cap"])
                break
            except Exception as e:
                log.warning("스크리너 %s offset %d 실패: %s", market, offset, e)
                time.sleep(3 * (attempt + 1))
        if not res or not res.get("quotes"):
            break
        for q in res["quotes"]:
            sym = q.get("symbol", "")
            if market == "kr":
                if not (len(sym) == 9 and sym[:6].isdigit() and sym[6:] in (".KS", ".KQ")):
                    continue
                if sym[5] != "0":            # 우선주 제외 (보통주 코드는 0으로 끝남)
                    continue
                nm = (q.get("shortName") or "") + " " + (q.get("longName") or "")
                if "스팩" in nm or "SPAC" in nm.upper() or "ACQUISITION" in nm.upper():
                    continue
            else:
                if q.get("exchange") not in cfg["exchanges"]:
                    continue
                if any(c in sym for c in ".-^") and not (len(sym.split(".")[0]) <= 5 and sym[-2:] in (".A", ".B", "-A", "-B")):
                    continue
                if (q.get("regularMarketPrice") or 0) < cfg["min_price"]:
                    continue
                if (q.get("averageDailyVolume3Month") or 0) < cfg["min_avg_volume"]:
                    continue
            out.append({"s": sym, "n": _clean_name(market, q.get("longName") or q.get("shortName"), sym),
                        "ex": q.get("exchange", ""), "cap": q.get("marketCap") or 0})
        offset += 250
        if offset >= res.get("total", 0):
            break
    return out


# ---------------------------------------------------------------- 시세
def _split(df, symbols):
    """yf.download 결과를 종목별 OHLCV DataFrame으로 나눈다."""
    res = {}
    if df is None or df.empty:
        return res
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = set(df.columns.get_level_values(0))
        for s in symbols:
            if s in lv0:
                d = df[s].dropna(subset=["Close", "High", "Low"])
                if len(d):
                    res[s] = d
    elif len(symbols) == 1:
        d = df.dropna(subset=["Close", "High", "Low"])
        if len(d):
            res[symbols[0]] = d
    return res


def apply_krx_close(df, k):
    """마지막 일봉을 KRX 공식 시세로 맞춘다. Yahoo에 그날 봉이 아직 없으면 추가한다."""
    if df is None or not len(df) or not k:
        return df
    d, o, h, l, c, v = k
    if not (c and c > 0 and h and l):                      # 거래정지 등
        return df
    last = df.index[-1]
    ld = last.strftime("%Y-%m-%d")
    df = df.copy()
    if ld == d:
        df.loc[last, "Close"] = float(c)
        df.loc[last, "High"] = max(float(h), float(df.loc[last, "High"]))
        df.loc[last, "Low"] = min(float(l), float(df.loc[last, "Low"]))
        df.loc[last, "Volume"] = float(v)
    elif ld < d:
        ts = pd.Timestamp(d)
        if df.index.tz is not None:
            ts = ts.tz_localize(df.index.tz)
        row = pd.DataFrame({"Open": [float(o)], "High": [float(h)], "Low": [float(l)], "Close": [float(c)], "Volume": [float(v)]}, index=[ts])
        df = pd.concat([df, row])
    return df


def market_open_now(market, as_of):
    """스캔 시점에 기준일 장이 아직 열려 있었는지(=마지막 값이 종가가 아닌 장중 시세인지)."""
    tz, close = ("Asia/Seoul", (15, 40)) if market == "kr" else ("America/New_York", (16, 15))
    now = pd.Timestamp.now(tz=tz)
    return now.strftime("%Y-%m-%d") == as_of and (now.hour, now.minute) < close


def download_prices(symbols):
    prices = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period="2y", interval="1d", group_by="ticker", auto_adjust=False,
                                 threads=True, progress=False, timeout=30)
                prices.update(_split(df, chunk))
                break
            except Exception as e:
                log.warning("시세 내려받기 실패(%d~): %s", i, e)
                time.sleep(5 * (attempt + 1))
        log.info("시세 %d/%d", min(i + BATCH, len(symbols)), len(symbols))
        time.sleep(0.5)
    # 빠진 종목은 천천히 한 번 더 (최대 5분)
    missing = [s for s in symbols if s not in prices]
    until = time.time() + 300
    for s in missing:
        if time.time() > until:
            break
        try:
            prices.update(_split(yf.download(s, period="2y", interval="1d", group_by="ticker",
                                             auto_adjust=False, progress=False, timeout=20), [s]))
        except Exception:
            pass
        time.sleep(0.4)
    return prices


# ---------------------------------------------------------------- 지표
def _sma(a, n, end):
    if end + 1 < n:
        return float("nan")
    return float(np.mean(a[end - n + 1:end + 1]))


def vcp(H, L, C, V):
    """최근 6개월 고점 이후의 조정(3% 이상 되돌림 기준 지그재그)이 차례로 작아지는지 본다."""
    e = len(C) - 1
    s = max(0, e - 129)
    hi_idx = s + int(np.argmax(H[s:e + 1]))
    dry = _sma(V, 10, e) / _sma(V, 50, e) if _sma(V, 50, e) else float("nan")
    if e - hi_idx < 15:
        return {"ok": False, "t": 0, "depths": [], "toPivot": (H[hi_idx] / C[e] - 1) * 100, "dryUp": dry}
    TH = 0.03
    piv = [("H", hi_idx, H[hi_idx])]
    mode, ext_i, ext_v = "down", hi_idx, float("inf")
    for i in range(hi_idx + 1, e + 1):
        if mode == "down":
            if L[i] < ext_v:
                ext_i, ext_v = i, L[i]
            if H[i] >= ext_v * (1 + TH):
                piv.append(("L", ext_i, ext_v)); mode, ext_i, ext_v = "up", i, H[i]
        else:
            if H[i] > ext_v:
                ext_i, ext_v = i, H[i]
            if L[i] <= ext_v * (1 - TH):
                piv.append(("H", ext_i, ext_v)); mode, ext_i, ext_v = "down", i, L[i]
    contr = [1 - piv[k + 1][2] / piv[k][2] for k in range(len(piv) - 1) if piv[k][0] == "H" and piv[k + 1][0] == "L"]
    if mode == "down" and piv[-1][0] == "H":
        contr.append(1 - ext_v / piv[-1][2])
    highs = [p for p in piv if p[0] == "H"]
    pivot = max(ext_v, highs[-1][2]) if mode == "up" else highs[-1][2]
    t = 1 if contr else 0
    while t < len(contr) and t < 6 and contr[-1 - t] > contr[-t] * 1.1:
        t += 1
    seq = contr[len(contr) - t:] if t else []
    last = seq[-1] if seq else 1
    to_pivot = pivot / C[e] - 1
    ok = t >= 2 and seq[0] <= 0.35 and last <= 0.12 and dry < 1 and -0.01 <= to_pivot <= 0.08
    return {"ok": bool(ok), "t": int(t), "depths": [round(d * 100, 1) for d in seq], "toPivot": to_pivot * 100, "dryUp": dry}


def analyze(df):
    if df is None or len(df) < 230:
        return None
    C = df["Close"].to_numpy(float); H = df["High"].to_numpy(float)
    L = df["Low"].to_numpy(float); V = df["Volume"].fillna(0).to_numpy(float)
    e = len(C) - 1; p = C[e]
    m50, m150, m200 = _sma(C, 50, e), _sma(C, 150, e), _sma(C, 200, e)
    m200_1m = _sma(C, 200, e - 22)
    w = min(252, len(C))
    hi, lo = float(np.max(H[e - w + 1:])), float(np.min(L[e - w + 1:]))
    roc = lambda n: C[e] / C[e - n] - 1 if e - n >= 0 else float("nan")
    rs_raw = 0.4 * roc(63) + 0.2 * roc(126) + 0.2 * roc(189) + 0.2 * roc(min(252, e))
    pts = C[-120:][::-1][::6][::-1]
    mn, mx = pts.min(), pts.max()
    spark = [round(float((x - mn) / ((mx - mn) or 1)) * 100) for x in pts]
    return {
        "p": p, "chg": (C[e] / C[e - 1] - 1) * 100, "date": df.index[e].strftime("%Y-%m-%d"),
        "rsRaw": rs_raw, "hi": hi, "lo": lo, "m50": m50, "m200": m200,
        "vol": _sma(V, 10, e) / _sma(V, 50, e) if _sma(V, 50, e) else float("nan"),
        "c": [p > m150 and p > m200, m150 > m200, m200 > m200_1m, m50 > m150 and m50 > m200,
              p > m50, p >= lo * 1.3, p >= hi * 0.75],
        "vcp": vcp(H, L, C, V), "spark": spark,
    }


# ---------------------------------------------------------------- 베이스 패턴
# 베이스 모양(컵, 플랫 베이스, 이중 바닥)은 주봉으로, 손잡이·피벗·하이 타이트 플래그는 일봉으로 판별한다.
# 기준은 윌리엄 오닐(CAN SLIM)·마크 미너비니가 제시한 수치를 따른다. 규칙 기반 근사이므로 최종 판단은 차트로.

def _weekly(df):
    w = df.resample("W-FRI").agg({"High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    return w["High"].to_numpy(float), w["Low"].to_numpy(float), w["Close"].to_numpy(float), w["Volume"].to_numpy(float), w.index


def cup_with_handle(df):
    """손잡이 달린 컵: 선행 상승 30%+, 컵 깊이 12~35%(최대 40%), 컵 6~65주, U자 바닥, 오른쪽이 왼쪽 고점 90% 이상 회복,
    손잡이 1~4주(5~20거래일)·깊이 12% 이내·컵 위쪽 절반, 피벗 = 손잡이 고점."""
    WH, WL, WC, WV, widx = _weekly(df)
    H = df["High"].to_numpy(float); L = df["Low"].to_numpy(float); C = df["Close"].to_numpy(float); V = df["Volume"].fillna(0).to_numpy(float)
    n, e = len(WH), len(C) - 1
    for a in range(n - 7, max(26, n - 66) - 1, -1):           # 가장 최근의 왼쪽 고점부터
        if WH[a] < WH[max(0, a - 4):a + 5].max():             # 앞뒤 4주 중 최고점이어야 함
            continue
        if WH[a] / WL[max(0, a - 26):a + 1].min() - 1 < 0.30:  # 선행 상승 30% 이상
            continue
        seg = slice(a + 1, n)
        b = a + 1 + int(np.argmin(WL[seg]))
        depth = 1 - WL[b] / WH[a]
        if not (0.12 <= depth <= 0.40) or b >= n - 1:
            continue
        c = b + 1 + int(np.argmax(WH[b + 1:]))
        if not (WH[a] * 0.90 <= WH[c] <= WH[a] * 1.05) or not (6 <= c - a <= 65):
            continue
        third = WL[b] + (WH[a] - WL[b]) / 3                    # U자: 아래 1/3 구간에 3주 이상 머묾
        if int(np.sum(WC[a:c + 1] <= third)) < 3 and depth > 0.15:
            continue
        # 손잡이 (일봉): 오른쪽 고점 이후
        start = int(np.searchsorted(df.index, widx[c] - pd.Timedelta(days=6)))
        p = start + int(np.argmax(H[start:e + 1]))
        hd = e - p                                            # 손잡이 길이(거래일)
        if hd < 5:
            return {"name": "컵 형성", "pivot": H[p], "note": f"컵 깊이 {depth*100:.0f}% · {c - a}주, 손잡이 형성 전"}
        if hd > 20:
            continue
        hlow = L[p + 1:e + 1].min()
        hdepth = 1 - hlow / H[p]
        if hdepth > 0.12 or hlow < WL[b] + (WH[a] - WL[b]) / 2:  # 깊이 12% 이내, 컵 위쪽 절반
            continue
        if C[e] > H[p] * 1.05:                                  # 이미 5% 넘게 돌파 → 늦음
            continue
        dry = np.mean(V[p + 1:e + 1]) / (_sma(V, 50, p) or np.nan)
        return {"name": "컵앤핸들", "pivot": H[p],
                "note": f"컵 {depth*100:.0f}% · {c - a}주, 손잡이 {hdepth*100:.0f}% · {hd}일" + (" · 손잡이 거래량 감소" if dry < 0.9 else "")}
    return None


def flat_base(df):
    """플랫 베이스: 20% 이상 오른 뒤 5주 이상, 고저 폭 15% 이내로 옆으로 다짐. 피벗 = 베이스 고점."""
    WH, WL, WC, WV, _ = _weekly(df)
    n = len(WH)
    best = None
    for k in range(5, min(26, n - 13)):                      # 최근 k주를 베이스로 볼 때 가장 긴 것
        hi, lo = WH[n - k:].max(), WL[n - k:].min()
        if hi / lo - 1 > 0.15:
            break
        if WC[n - k - 1] / WL[max(0, n - k - 26):n - k].min() - 1 >= 0.20:
            best = (k, hi, lo)
    if not best:
        return None
    k, hi, lo = best
    # 신고가 근처에서 다지는 베이스여야 하고(52주 고점 95% 이상), 주가가 베이스 위쪽 절반에 있어야 함
    if hi < WH[max(0, n - 52):].max() * 0.95 or WC[-1] < lo + (hi - lo) * 0.5:
        return None
    # 주봉 종가가 촘촘해야 함(최근 k주 종가 변동이 작음)
    if np.std(np.diff(np.log(WC[n - k:]))) > 0.04:
        return None
    return {"name": "플랫 베이스", "pivot": hi, "note": f"{k}주 · 폭 {(hi/lo-1)*100:.0f}%"}


def double_bottom(df):
    """이중 바닥(W): 선행 상승 30%+, 깊이 12~40%, 7주 이상, 두 번째 바닥이 첫 바닥을 살짝(8% 이내) 깨고 반등.
    피벗 = W 가운데 고점."""
    WH, WL, WC, WV, _ = _weekly(df)
    n = len(WH)
    lo_a = max(26, n - 66)
    if n - 7 <= lo_a:
        return None
    a = lo_a + int(np.argmax(WH[lo_a:n - 6]))
    if WH[a] / WL[max(0, a - 26):a + 1].min() - 1 < 0.30:
        return None
    b2 = a + 1 + int(np.argmin(WL[a + 1:]))
    if b2 - a < 4 or b2 >= n - 1:
        return None
    # 첫 바닥 b1: 두 번째 바닥 앞의 주봉 저점(앞뒤 2주 중 최저) 중, 그 뒤 8% 이상 반등(가운데 고점 m)한 가장 낮은 곳
    lows = [i for i in range(a + 2, b2 - 2) if WL[i] == WL[max(a + 1, i - 2):i + 3].min()]
    b1 = m = None
    for i in sorted(lows, key=lambda i: WL[i]):
        j = i + 1 + int(np.argmax(WH[i + 1:b2]))
        if WH[j] / WL[i] - 1 >= 0.08 and j - i >= 2 and b2 - j >= 2:
            b1, m = i, j
            break
    if b1 is None:
        return None
    depth = 1 - WL[b2] / WH[a]
    if not (0.12 <= depth <= 0.40) or not (WL[b1] * 0.92 <= WL[b2] < WL[b1]):
        return None
    if WH[m] > WH[a] or WH[m] < WL[b1] + 0.4 * (WH[a] - WL[b1]) or n - 1 - a < 7:  # 가운데 고점은 베이스 중간 이상
        return None
    if WC[-1] < WL[b2] * 1.03 or WC[-1] > WH[m] * 1.05:
        return None
    return {"name": "이중 바닥", "pivot": WH[m], "note": f"깊이 {depth*100:.0f}% · {n - 1 - a}주, 두 번째 바닥 {(1-WL[b2]/WL[b1])*100:.0f}% 언더컷"}


def high_tight_flag(df):
    """하이 타이트 플래그: 8주(40거래일) 안에 90% 이상 급등 → 3~25거래일 동안 25% 이내 조정. 피벗 = 깃대 고점."""
    H = df["High"].to_numpy(float); L = df["Low"].to_numpy(float); C = df["Close"].to_numpy(float)
    e = len(C) - 1
    p = e - 30 + int(np.argmax(H[e - 30:e + 1]))
    if not (3 <= e - p <= 25):
        return None
    gain = H[p] / L[max(0, p - 40):p + 1].min() - 1
    depth = 1 - L[p + 1:e + 1].min() / H[p]
    if gain < 0.90 or depth > 0.25:
        return None
    return {"name": "하이 타이트 플래그", "pivot": H[p], "note": f"깃대 +{gain*100:.0f}%, 조정 {depth*100:.0f}% · {e - p}일"}


def find_patterns(df):
    out = []
    if df is None or len(df) < 230:
        return out
    c = float(df["Close"].iloc[-1])
    for fn in (cup_with_handle, double_bottom, flat_base, high_tight_flag):
        try:
            r = fn(df)
        except Exception:
            r = None
        if r and r["pivot"] and math.isfinite(r["pivot"]):
            r["toPivot"] = (r["pivot"] / c - 1) * 100
            if -5 <= r["toPivot"] <= 15:                     # 피벗 근처(아래 15% ~ 돌파 후 5%)만
                out.append(r)
    names = {r["name"] for r in out}
    if names & {"컵앤핸들", "이중 바닥"}:                     # 큰 베이스가 잡히면 겹치는 작은 판정은 뺀다
        out = [r for r in out if r["name"] not in ("플랫 베이스", "컵 형성")]
    return out


def rs_rank(values):
    """같은 시장 스캔 대상 안에서 백분위(1~99)."""
    arr = np.sort(np.array(values))
    return lambda v: int(max(1, min(99, round(np.searchsorted(arr, v, side="left") / len(arr) * 100))))


def _r(x, n=1):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), n)


def fetch_fundamentals(symbols, budget=420):
    """8개 조건 통과 종목의 최근 분기 실적 성장률(전년 동기 대비)을 Yahoo에서 받는다. 7일간 캐시."""
    from concurrent.futures import ThreadPoolExecutor
    path = DATA / "fundamentals.json"
    cache = _load(path, {})
    fresh = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    need = [s for s in symbols if not (s in cache and cache[s].get("at", "") >= fresh)]
    until = time.time() + budget

    def one(sym):
        for attempt in range(2):
            if time.time() > until:
                return sym, None
            try:
                info = yf.Ticker(sym).info or {}
                g = lambda k: (round(float(info[k]) * 100, 1) if isinstance(info.get(k), (int, float)) and math.isfinite(info[k]) else None)
                return sym, {"eps": g("earningsQuarterlyGrowth"), "rev": g("revenueGrowth"), "at": dt.date.today().isoformat()}
            except Exception:
                time.sleep(2 * (attempt + 1))
        return sym, None

    if need:
        log.info("실적 조회 %d종목 (캐시 %d종목)", len(need), len(symbols) - len(need))
        with ThreadPoolExecutor(max_workers=4) as ex:
            for sym, v in ex.map(one, need):
                if v:
                    cache[sym] = v
        DATA.mkdir(exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


INDEX = {"kr": ("^KS11", "코스피"), "us": ("^GSPC", "S&P 500")}


def market_condition(market):
    """지수의 50일·200일선 위치로 시장 추세를 본다. 미너비니는 지수가 하락 추세일 때 신규 매수를 하지 않는다."""
    sym, name = INDEX[market]
    try:
        df = yf.download(sym, period="2y", interval="1d", auto_adjust=False, progress=False, timeout=30)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.xs(sym, axis=1, level=1) if sym in df.columns.get_level_values(1) else df.droplevel(1, axis=1)
        C = df["Close"].dropna().to_numpy(float)
        if len(C) < 220:
            return None
        e = len(C) - 1
        m50, m200, m200_1m = _sma(C, 50, e), _sma(C, 200, e), _sma(C, 200, e - 22)
        if C[e] > m50 and m50 > m200 and m200 >= m200_1m:
            state, label = "up", "상승 추세"
        elif C[e] > m200:
            state, label = "correction", "조정 중"
        else:
            state, label = "down", "하락 추세"
        return {"name": name, "close": round(float(C[e]), 2), "chg": round((C[e] / C[e - 1] - 1) * 100, 2),
                "vs50": round((C[e] / m50 - 1) * 100, 1), "vs200": round((C[e] / m200 - 1) * 100, 1),
                "state": state, "label": label}
    except Exception as ex:
        log.warning("[%s] 지수 추세 확인 실패: %s", market, ex)
        return None


def minervini_view(r, mkt):
    """A등급 종목을 미너비니 매수 원칙(시장 추세·베이스·피벗·확장·손절)으로 판정한 참고 의견. 규칙 기반."""
    p = r["p"]
    pats = [x for x in (r.get("pattern") or "").split(" · ") if x and x != "컵 형성"]
    base = ("VCP" if r.get("vcp") else "") or (pats[0] if pats else "")
    to_piv = r.get("patPivot") if pats and r.get("patPivot") is not None else r.get("toPivot")
    bn = base if base.endswith("베이스") else f"{base} 베이스"
    # 베이스가 없으면 52주 고점을 넘어서는 지점을 피벗 후보로 본다
    if base and to_piv is not None:
        pivot, pl = p * (1 + to_piv / 100), "피벗"
    elif r.get("fromHi") is not None:
        pivot, pl = p / (1 + r["fromHi"] / 100), "52주 고점"
        to_piv = (pivot / p - 1) * 100
    else:
        pivot, pl = None, "피벗"
    # 1) 차트 자리 판단 (시장과 무관)
    if (r.get("vs50") or 0) > 20 or (base and to_piv is not None and to_piv < -5):
        sv = "관망"
        ext = f"50일선보다 {r['vs50']:.0f}% 위" if (r.get("vs50") or 0) > 20 else f"피벗보다 {-to_piv:.0f}% 위"
        line = f"이미 {ext}로 많이 올랐습니다. 추격 매수 대신 다음 베이스나 10·21일선 눌림을 기다립니다."
    elif base and to_piv is not None and -5 <= to_piv <= 3:
        sv = "매수 검토"
        if to_piv >= 0:
            line = f"{bn}의 피벗까지 {to_piv:.1f}% 남았습니다. 피벗을 평소보다 40~50% 많은 거래량으로 돌파할 때가 매수 시점입니다."
        else:
            line = f"{bn}의 피벗을 {-to_piv:.1f}% 넘어선 상태로, 피벗 위 5% 이내의 매수 가능 구간입니다. 돌파 때 거래량이 충분했는지 확인하세요."
    elif base and to_piv is not None:
        sv = "관망"
        line = f"{bn}는 있지만 피벗까지 {to_piv:.0f}% 남았습니다. 피벗 근처로 다가올 때까지 기다립니다."
    else:
        sv = "관망"
        line = "추세와 실적은 좋지만 뚜렷한 베이스(VCP·컵·플랫)가 아직 없습니다. 베이스가 만들어질 때까지 기다리고, 그 전에는 52주 고점 돌파를 참고합니다."
    why = []
    # 2) 시장 추세
    if mkt and mkt["state"] == "down":
        v = "보류"
        why.append(f"{mkt['name']} 지수가 200일선 아래 하락 추세입니다. 미너비니는 이때 신규 매수를 하지 않고 현금을 지킵니다.")
        why.append(("시장이 돌아서면: " if sv == "매수 검토" else "차트 자리: ") + line)
    else:
        v = sv
        why.append(line)
        if mkt and mkt["state"] == "correction":
            why.append(f"{mkt['name']} 지수가 50일선 아래로 조정 중이라 매수해도 비중을 줄이는 게 원칙입니다.")
    vr = r.get("volRatio")
    if vr is not None and vr < 0.8:
        why.append("최근 거래량이 줄었습니다(매도 압력 감소, 좋은 신호).")
    elif vr is not None and vr > 1.5 and (r.get("chg") or 0) < 0:
        why.append("하락하는 날 거래량이 늘었습니다. 기관 매도 가능성을 확인하세요.")
    eps, rev = r.get("eps"), r.get("rev")
    if eps is not None and rev is not None:
        why.append(f"최근 분기 이익 {eps:+.0f}%, 매출 {rev:+.0f}% 성장(전년 동기 대비)으로 실적 조건은 충족합니다.")
    entry = max(p, pivot) if (pivot and sv == "매수 검토") else None
    return {"v": v, "why": why, "pivot": _r(pivot, 4), "pl": pl, "toPiv": _r(to_piv) if to_piv is not None else None,
            "stop": _r(entry * 0.93, 4) if entry else None, "base": base, "setup": sv}

def grade_of(r):
    """A: 8개 조건 + 가격 위치 + 실적 / B: 8개 조건 + 가격 위치 / C: 8개 조건만"""
    if r["score"] != 8:
        return ""
    pos_ok = r["fromHi"] is not None and r["fromHi"] >= -MAX_FROM_HIGH and r["vs50"] is not None and r["vs50"] <= MAX_ABOVE_50
    earn_ok = r.get("eps") is not None and r.get("rev") is not None and r["eps"] >= EPS_GROWTH_MIN and r["rev"] >= REV_GROWTH_MIN
    return "A" if pos_ok and earn_ok else "B" if pos_ok else "C"


def scan(market):
    t0 = time.time()
    log.info("[%s] 스캔 시작", market)
    uni = get_universe(market)
    log.info("[%s] 스캔 대상 %d종목", market, len(uni))
    if not uni:
        raise RuntimeError("스캔 대상 목록을 받지 못했습니다 (Yahoo 접속 문제일 수 있음)")
    prices = download_prices([u["s"] for u in uni])
    for u in uni:
        if u.get("krx") and u["s"] in prices:
            prices[u["s"]] = apply_krx_close(prices[u["s"]], u["krx"])
    valid = []
    for u in uni:
        a = analyze(prices.get(u["s"]))
        if a and math.isfinite(a["rsRaw"]):
            valid.append((u, a))
    rank = rs_rank([a["rsRaw"] for _, a in valid])
    rows, near_all, as_of = [], 0, ""
    for u, a in valid:
        rs = rank(a["rsRaw"])
        crit = [bool(c) for c in a["c"]] + [rs >= RS_MIN]
        score = int(sum(crit))
        as_of = max(as_of, a["date"])
        if score == 7:
            near_all += 1
        if score < 7 or (score == 7 and rs < KEEP_NEAR_RS):
            continue
        v = a["vcp"]
        pats = find_patterns(prices.get(u["s"]))
        rows.append({
            "pattern": " · ".join(p["name"] for p in pats),
            "patNote": " / ".join(p["note"] for p in pats),
            "patPivot": _r(min((p["toPivot"] for p in pats), key=abs)) if pats else None,
            "s": u["s"], "n": u["n"], "ex": u["ex"], "p": _r(a["p"], 4), "chg": _r(a["chg"], 2), "rs": rs, "score": score,
            "crit": "".join("1" if c else "0" for c in crit),
            "fromHi": _r((a["p"] / a["hi"] - 1) * 100), "vs50": _r((a["p"] / a["m50"] - 1) * 100),
            "volRatio": _r(a["vol"], 2), "vcp": 1 if (v["ok"] and score == 8 and rs >= PATTERN_RS_MIN) else 0,
            "vcpDepths": "/".join(f"{d:g}" for d in v["depths"]), "toPivot": _r(v["toPivot"]), "dryUp": _r(v["dryUp"], 2),
            "spark": a["spark"],
        })
    fund = fetch_fundamentals([r["s"] for r in rows if r["score"] == 8])
    for r in rows:
        f = fund.get(r["s"]) or {}
        r["eps"], r["rev"] = f.get("eps"), f.get("rev")
        r["grade"] = grade_of(r)
    mkt = market_condition(market)
    for r in rows:
        if r["grade"] == "A":
            r["view"] = minervini_view(r, mkt)
    order = {"A": 0, "B": 1, "C": 2, "": 3}
    rows.sort(key=lambda r: (order[r["grade"]], -r["score"], -r["rs"]))
    summary = {
        "market": market, "asOf": as_of, "scannedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "universe": len(uni), "analyzed": len(valid), "missing": len([u for u in uni if u["s"] not in prices]),
        "pass": int(sum(r["score"] == 8 for r in rows)), "near": int(near_all), "vcp": int(sum(r["vcp"] for r in rows)),
        "patterns": int(sum(1 for r in rows if r["score"] == 8 and r["rs"] >= PATTERN_RS_MIN and r["pattern"] and r["pattern"] != "컵 형성")),
        "patRs": PATTERN_RS_MIN,
        "gradeA": int(sum(r["grade"] == "A" for r in rows)), "gradeB": int(sum(r["grade"] == "B" for r in rows)),
        "rsMin": RS_MIN, "maxFromHigh": MAX_FROM_HIGH, "maxAbove50": MAX_ABOVE_50,
        "epsMin": EPS_GROWTH_MIN, "revMin": REV_GROWTH_MIN,
        "seconds": round(time.time() - t0),
        "intraday": bool(as_of and market_open_now(market, as_of)),
        "mkt": mkt,
        "buy": int(sum(1 for r in rows if r.get("view", {}).get("v") == "매수 검토")),
    }
    log.info("[%s] 완료: %s", market, summary)
    return summary, rows


# ---------------------------------------------------------------- 저장
def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(market, summary, rows):
    DATA.mkdir(exist_ok=True)
    latest, prev = DATA / f"{market}_latest.json", DATA / f"{market}_prev.json"
    old = _load(latest, None)
    # 새 거래일이면 직전 결과를 비교 기준으로 넘기고, 같은 날 다시 돌린 경우엔 덮어쓰기만 한다
    if old and old["summary"]["asOf"] < summary["asOf"]:
        prev.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    latest.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    hist_path = DATA / "history.json"
    hist = [h for h in _load(hist_path, []) if not (h["market"] == market and h["asOf"] == summary["asOf"])]
    hist.append(summary)
    hist.sort(key=lambda h: (h["asOf"], h["market"]))
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=0), encoding="utf-8")
    pd.DataFrame(rows).drop(columns=["spark"], errors="ignore").to_csv(
        DATA / f"{market}_{summary['asOf'].replace('-', '')}.csv", index=False, encoding="utf-8-sig")


# ---------------------------------------------------------------- 결과 화면
def build_report(site_dir=None):
    payload = {"generatedAt": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
               "history": _load(DATA / "history.json", [])}
    for m in SETTINGS:
        cur, prev = _load(DATA / f"{m}_latest.json", None), _load(DATA / f"{m}_prev.json", None)
        payload[m] = {"summary": cur["summary"] if cur else None, "rows": cur["rows"] if cur else [],
                      "prev": [{"s": r["s"], "n": r["n"], "score": r["score"]} for r in prev["rows"]] if prev else None}
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.replace("/*__DATA__*/null", data)
    REPORT.write_text(page, encoding="utf-8")
    if site_dir:  # 휴대폰용 웹앱(GitHub Pages) 폴더: index.html + 앱 아이콘·매니페스트
        import shutil
        site = Path(site_dir)
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text(page, encoding="utf-8")
        for f in ("icon-192.png", "icon-512.png", "manifest.webmanifest"):
            if (BASE / "assets" / f).exists():
                shutil.copy(BASE / "assets" / f, site / f)
    return REPORT


TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>트렌드 템플릿 스캐너</title>
<link rel="manifest" href="manifest.webmanifest"><link rel="icon" href="icon-192.png"><link rel="apple-touch-icon" href="icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="트렌드스캐너"><meta name="theme-color" content="#2f6b4f">
<style>
/* 휴대폰 우선: 위에서부터 시장 상태 → 오늘 요약 → A등급 카드 → 나머지는 접힌 메뉴 */
:root{--bg:#f4f5f2;--panel:#fff;--fg:#1b211e;--muted:#67716b;--line:#e0e4df;--accent:#2f6b4f;--up:#1d7a4b;--down:#c23b2c;--warn:#a86a00;--chip:#edf1ec;
--buy-bg:#e3f3ea;--watch-bg:#fbf1dc;--hold-bg:#f8e3e0;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111412;--panel:#191d1b;--fg:#e7ece8;--muted:#9aa49e;--line:#2b312e;--accent:#73c49a;--up:#5cc58e;--down:#f07364;--warn:#f0b04a;--chip:#222825;
--buy-bg:#183527;--watch-bg:#382c14;--hold-bg:#3a1d19;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 "Pretendard","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;-webkit-text-size-adjust:100%}
.wrap{max-width:1100px;margin:0 auto;padding:0 max(14px,env(safe-area-inset-left)) 40px max(14px,env(safe-area-inset-right))}
.top{position:sticky;top:0;z-index:5;background:var(--bg);padding:calc(env(safe-area-inset-top,0px) + 12px) 0 0;border-bottom:1px solid var(--line)}
.top h1{font-size:18px;margin:0;font-weight:700;letter-spacing:-.01em;display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.top h1 small{font-size:12px;font-weight:400;color:var(--muted)}
.tabs{display:flex;gap:2px;margin-top:8px}
.tab{flex:1;background:none;border:0;border-bottom:2.5px solid transparent;padding:9px 0;font:inherit;font-size:15px;color:var(--muted);cursor:pointer}
.tab[aria-selected=true]{color:var(--fg);border-bottom-color:var(--accent);font-weight:700}
.sec{margin-top:16px}
.sec>h2{font-size:16px;margin:0 0 8px;font-weight:700;display:flex;align-items:baseline;gap:8px}
.sec>h2 .cnt{font-size:13px;font-weight:500;color:var(--muted)}
.note{font-size:12.5px;color:var(--muted);margin:0 0 8px}
.mkt{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-top:14px}
.mkt .dot{width:10px;height:10px;border-radius:50%;flex:none}
.mkt.up .dot{background:var(--up)}.mkt.correction .dot{background:var(--warn)}.mkt.down .dot{background:var(--down)}
.mkt b{font-size:15px}.mkt .sub{font-size:12.5px;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
.sec,details.fold{scroll-margin-top:110px}
.stat{cursor:pointer;-webkit-tap-highlight-color:transparent;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 8px;text-align:center}
.stat .n{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.2}
.stat .l{font-size:11.5px;color:var(--muted)}
.stat.hl{border-color:var(--up)}.stat.hl .n{color:var(--up)}
.legend{margin-top:10px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:9px 12px;font-size:12.5px;color:var(--muted);display:grid;gap:4px}
.legend .g{margin:0 6px 0 0}
.meta{font-size:12px;color:var(--muted);margin:8px 2px 0}
.cards{display:grid;gap:8px;grid-template-columns:1fr;align-items:start}
@media (min-width:720px){.cards{max-width:780px}.stats{grid-template-columns:repeat(4,minmax(0,160px))}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;min-width:0}
.card>summary{list-style:none;cursor:pointer;padding:12px 14px;-webkit-tap-highlight-color:transparent}
.card>summary::-webkit-details-marker{display:none}
.card .t{min-width:0}.card .rt{flex:none;text-align:right}
.card .rt .q{font-size:13px;font-variant-numeric:tabular-nums;margin-top:5px;white-space:nowrap}
.card .body{padding:4px 14px 14px;border-top:1px solid var(--line)}
.card .nm::after{content:" ▾";color:var(--muted);font-size:12px;font-weight:400}.card[open] .nm::after{content:" ▴"}
.card .go{display:inline-block;margin-top:10px;font-size:13.5px;color:var(--accent);text-decoration:none;font-weight:600}
.card .hd{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.card .nm{font-size:16.5px;font-weight:700;line-height:1.3;color:var(--fg);text-decoration:none}
.card .cd{font-size:12px;color:var(--muted)}
.vd{flex:none;font-size:12.5px;font-weight:700;padding:4px 10px;border-radius:99px;white-space:nowrap}
.vd.buy{background:var(--buy-bg);color:var(--up)}.vd.watch{background:var(--watch-bg);color:var(--warn)}.vd.hold{background:var(--hold-bg);color:var(--down)}
.px{display:flex;align-items:baseline;gap:8px;margin-top:6px;font-variant-numeric:tabular-nums}
.px .p{font-size:20px;font-weight:700}.px .c{font-size:13.5px}
.up{color:var(--up)}.down{color:var(--down)}
.sp{display:block;width:100%;height:54px;margin:6px 0 2px}
.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:6px 10px;margin-top:6px}
.kv div{min-width:0}.kv .k{font-size:11.5px;color:var(--muted)}.kv .v{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
.why{margin:10px 0 0;padding:10px 12px 10px 26px;background:var(--chip);border-radius:10px;font-size:13.5px}
.why li{margin:2px 0}
.lv{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:13px;font-variant-numeric:tabular-nums}
.lv b{font-weight:600}
.tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.tag{font-size:11.5px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.tag.pat{color:var(--warn);border-color:currentColor}.tag.vcp{color:var(--accent);border-color:currentColor}
details.fold{background:var(--panel);border:1px solid var(--line);border-radius:14px;margin-top:10px}
details.fold>summary{list-style:none;cursor:pointer;padding:13px 14px;font-weight:700;font-size:15px;display:flex;justify-content:space-between;align-items:center}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::after{content:"＋";color:var(--muted);font-weight:400}
details.fold[open]>summary::after{content:"－"}
details.fold>summary .cnt{font-weight:500;color:var(--muted);font-size:13px;margin-left:6px;margin-right:auto}
.fold .in{padding:0 14px 14px}
.row{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row .t{flex:1;min-width:0}.row .t a{color:var(--fg);text-decoration:none;font-weight:600}
.row .t .s{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .r{text-align:right;font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}
.g{display:inline-block;font-size:11px;font-weight:700;padding:0 6px;border-radius:5px;margin-left:5px;vertical-align:1px}
.g.A{background:var(--up);color:#fff}.g.B{background:var(--chip);color:var(--fg)}.g.C{color:var(--muted);box-shadow:inset 0 0 0 1px var(--line)}
.pill{font-size:12px;padding:1px 8px;border-radius:99px}.pill.in{background:var(--buy-bg);color:var(--up)}.pill.out{background:var(--hold-bg);color:var(--down)}
.controls{display:flex;gap:6px;margin-bottom:6px}
.controls select,.controls input{font:inherit;font-size:14px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);min-width:0}
.controls input{flex:1}
.empty{color:var(--muted);font-size:13px;margin:4px 0}
.disc{font-size:12px;color:var(--muted);margin-top:18px;text-align:center}
.crit li{margin:3px 0}.crit{padding-left:20px;font-size:13px}.fold p{font-size:13px}
svg text{fill:var(--muted);font-size:11px}
</style></head><body><div class="wrap">
<div class="top"><h1>트렌드 템플릿 스캐너 <small id="gen"></small></h1>
<nav class="tabs"><button class="tab" data-m="kr">한국</button><button class="tab" data-m="us">미국</button></nav></div>
<div id="view"></div>
<details class="fold" id="breadthBox"><summary>통과 종목 수 추이</summary><div class="in"><p class="note">스캔마다 8개 조건을 모두 충족한 종목 수. 늘면 주도주가 넓어지고, 줄면 시장이 약해지는 신호입니다.</p><div id="breadth"></div></div></details>
<details class="fold"><summary>판정·스캔 기준</summary><div class="in">
<p><b>미너비니 판정</b> (A등급에만, 규칙으로 자동 판정)</p>
<ul class="crit">
<li><b>매수 검토</b>: VCP나 베이스(컵앤핸들·플랫·이중 바닥·플래그)가 있고 피벗 아래 3% ~ 위 5% 안. 피벗을 평소보다 40~50% 많은 거래량으로 돌파할 때 매수, 손절은 매수가 −7%.</li>
<li><b>관망</b>: 베이스가 없거나 피벗까지 멀거나, 50일선 +20%·피벗 +5%를 넘게 올라 추격 위험.</li>
<li><b>보류</b>: 지수가 200일선 아래 하락 추세. 미너비니는 이때 신규 매수를 하지 않습니다.</li>
<li>시장 상태: 지수가 50일선·200일선 위이고 200일선이 오르면 상승 추세, 200일선 위·50일선 아래면 조정 중, 200일선 아래면 하락 추세.</li></ul>
<p><b>등급</b>: A = 8개 조건 + 가격 위치(52주 고점 15% 이내, 50일선 +25% 이하) + 실적(최근 분기 순이익 +25%, 매출 +20% 이상, 전년 같은 분기 대비) · B = 8개 조건 + 가격 위치 · C = 8개 조건만.</p>
<p><b>트렌드 템플릿 8개 조건</b></p>
<ol class="crit"><li>주가가 150일·200일선 위</li><li>150일선이 200일선 위</li><li>200일선이 1개월 전보다 높음</li><li>50일선이 150일·200일선 위</li><li>주가가 50일선 위</li><li>52주 최저가보다 30% 이상 높음</li><li>52주 최고가에서 25% 이내</li><li>RS 80 이상(3·6·9·12개월 수익률을 같은 시장 안에서 백분위로 매긴 값)</li></ol>
<p>VCP: 고점 이후 조정폭이 차례로 작아지고(첫 조정 35% 이하, 마지막 12% 이하) 거래량이 줄며 피벗 8% 이내. 베이스 모양은 주봉, 손잡이·피벗은 일봉으로 판별합니다.</p>
<p>스캔 대상: 한국 시가총액 3,000억 원 이상 보통주, 미국 시가총액 20억 달러·주가 10달러·거래량 20만 주 이상. 시세·실적: KRX, Yahoo Finance. 수치는 config.json에서 바꿀 수 있습니다.</p>
</div></details>
<p class="disc">규칙으로 자동 판정한 참고 의견이며 투자 권유가 아닙니다. 매수 전 차트와 실적을 직접 확인하세요.</p>
</div>
<script>
const D=/*__DATA__*/null;
const NAMES={kr:'한국',us:'미국'};
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n==null?'—':Number(n).toLocaleString('ko-KR');
const pct=(v,d=1)=>v==null?'—':(v>0?'+':'')+Number(v).toFixed(d)+'%';
const price=(m,v)=>v==null?'—':m==='kr'?Math.round(v).toLocaleString('ko-KR')+'원':'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const code=s=>s.replace(/\.(KS|KQ)$/,'');
const link=(m,s)=>m==='kr'?'https://m.stock.naver.com/domestic/stock/'+code(s)+'/total':'https://finance.yahoo.com/quote/'+encodeURIComponent(s)+'/chart';
const VC={'매수 검토':'buy','관망':'watch','보류':'hold'};
const st={m:'kr',kr:{f:'B',q:''},us:{f:'B',q:''}};
function spark(v,big){if(!v||v.length<2)return'';const w=big?300:80,h=big?54:24,p=3,x=i=>p+i*(w-2*p)/(v.length-1),y=t=>h-p-t/100*(h-2*p);
 const c=v[v.length-1]>=v[0]?'var(--up)':'var(--down)';const d=v.map((t,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(t).toFixed(1)).join('');
 return `<svg class="${big?'sp':''}" width="${big?'100%':w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${big?`<path d="${d}L${x(v.length-1)} ${h}L${x(0)} ${h}Z" fill="${c}" opacity=".08"/>`:''}<path d="${d}" fill="none" stroke="${c}" stroke-width="${big?2:1.5}" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;}
function tags(r){const t=[];if(r.vcp)t.push('<span class="tag vcp">VCP</span>');(r.pattern||'').split(' · ').filter(Boolean).forEach(x=>t.push(`<span class="tag ${x==='컵 형성'?'':'pat'}">${esc(x)}</span>`));return t.join('');}
function exName(m,r){return m==='kr'?(r.ex==='KSC'?'코스피':'코스닥'):(r.ex||'');}
function changes(m){const M=D[m];if(!M.prev)return null;const was=new Map(M.prev.filter(r=>r.score===8).map(r=>[r.s,r]));const now=new Map(M.rows.map(r=>[r.s,r]));const out=[];
 for(const r of M.rows)if(r.score===8&&!was.has(r.s))out.push({...r,change:'진입'});
 for(const [s,r] of was){const c=now.get(s);if(!c||c.score<8)out.push({s,n:r.n,ex:c?.ex,grade:c?.grade,change:'이탈',score:c?c.score:null,rs:c?c.rs:null});}
 return out.sort((a,b)=>a.change===b.change?(b.rs??0)-(a.rs??0):a.change==='진입'?-1:1);}
function hist(m){return D.history.filter(h=>h.market===m);}
function card(m,r){const v=r.view||{v:'관망',why:[]};
 const tp=r.patPivot??r.toPivot;
 return `<details class="card"><summary class="hd"><div class="t"><span class="nm">${esc(r.n)}</span><div class="cd">${esc(code(r.s))} · RS ${r.rs}${v.base?' · '+esc(v.base):''}${v.pivot&&(v.toPiv??tp)!=null?' · '+esc(v.pl||'피벗')+'까지 '+pct(v.toPiv??tp):''}${v.v==='보류'&&v.setup==='매수 검토'?' · <b class="up">자리 좋음</b>':''}</div></div><div class="rt"><span class="vd ${VC[v.v]||'watch'}">${esc(v.v)}</span><div class="q">${price(m,r.p)} <span class="${r.chg>0?'up':r.chg<0?'down':''}">${pct(r.chg,2)}</span></div></div></summary>
 <div class="body">${spark(r.spark,true)}
 <div class="kv"><div><div class="k">RS</div><div class="v">${r.rs}</div></div><div><div class="k">이익 성장</div><div class="v up">${pct(r.eps,0)}</div></div><div><div class="k">매출 성장</div><div class="v up">${pct(r.rev,0)}</div></div>
 <div><div class="k">고점 대비</div><div class="v">${pct(r.fromHi)}</div></div><div><div class="k">50일선 대비</div><div class="v">${pct(r.vs50)}</div></div><div><div class="k">거래량 10/50일</div><div class="v">${r.volRatio?.toFixed(2)??'—'}</div></div></div>
 <div class="tags">${tags(r)}</div>
 <ul class="why">${(v.why||[]).map(w=>`<li>${esc(w)}</li>`).join('')}</ul>
 ${v.pivot?`<div class="lv"><span>${esc(v.pl||'피벗')} <b>${price(m,v.pivot)}</b></span>${v.stop?`<span>손절 기준 <b class="down">${price(m,v.stop)}</b> (−7%${v.v==='보류'?', 시장 회복 시':''})</span>`:''}</div>`:''}
 <a class="go" href="${link(m,r.s)}" target="_blank" rel="noopener">차트 보기 ↗</a></div></details>`;}
function row(m,r,right){return `<div class="row"><div class="t"><a href="${link(m,r.s)}" target="_blank" rel="noopener">${esc(r.n)}</a>${r.grade?`<span class="g ${r.grade}">${r.grade}</span>`:''}<div class="s">${esc(code(r.s))} · ${tags(r)||esc(exName(m,r))}</div></div>${spark(r.spark,false)}<div class="r">${right}</div></div>`;}
function render(){
 document.querySelectorAll('.tab').forEach(t=>t.setAttribute('aria-selected',t.dataset.m===st.m));
 const m=st.m,M=D[m],S=M.summary,v=$('#view');
 if(!S){v.innerHTML=`<p class="empty" style="margin-top:20px">${NAMES[m]} 스캔 결과가 아직 없습니다.</p>`;return;}
 const mk=S.mkt,A=M.rows.filter(r=>r.grade==='A');
 const ord={'매수 검토':0,'관망':1,'보류':2};
 A.sort((a,b)=>(ord[a.view?.v]??1)-(ord[b.view?.v]??1)||(ord[a.view?.setup]??1)-(ord[b.view?.setup]??1)||Math.abs(a.patPivot??a.toPivot??99)-Math.abs(b.patPivot??b.toPivot??99)||b.rs-a.rs);
 const buy=A.filter(r=>r.view?.v==='매수 검토').length;
 const vp=M.rows.filter(r=>r.grade!=='A'&&(r.vcp||(r.pattern&&r.pattern!=='컵 형성'&&r.score===8&&r.rs>=(S.patRs??85))));
 const ch=changes(m),h=hist(m),prevH=h.length>1?h[h.length-2]:null,dPass=prevH?S.pass-prevH.pass:null;
 v.innerHTML=`
 ${mk?`<div class="mkt ${mk.state}"><span class="dot"></span><div><b>${esc(mk.name)} ${esc(mk.label)}</b><div class="sub">${fmt(mk.close)} (${pct(mk.chg,2)}) · 50일선 ${pct(mk.vs50)} · 200일선 ${pct(mk.vs200)}${mk.state==='down'?' · 신규 매수 쉬는 구간':mk.state==='correction'?' · 매수 비중 줄이기':''}</div></div></div>`:''}
 <div class="stats"><div class="stat hl" data-go="A"><div class="n">${buy}</div><div class="l">매수 검토</div></div><div class="stat" data-go="A"><div class="n">${A.length}</div><div class="l">A등급</div></div><div class="stat" data-go="Bo"><div class="n">${fmt(S.gradeB??0)}</div><div class="l">B등급</div></div><div class="stat" data-go="8"><div class="n">${fmt(S.pass)}</div><div class="l">8개 조건${!dPass?'':` <span class="${dPass>0?'up':dPass<0?'down':''}">${dPass>0?'+':''}${dPass}</span>`}</div></div></div>
 <div class="legend"><div><span class="g A">A</span> 8개 조건 + 가격 위치(고점 ${S.maxFromHigh??15}% 이내, 50일선 +${S.maxAbove50??25}% 이하) + 실적(최근 분기 이익 +${S.epsMin??25}%, 매출 +${S.revMin??20}% 이상)</div><div><span class="g B">B</span> 8개 조건 + 가격 위치 (실적 미달이거나 실적 데이터 없음)</div><div><span class="g C">C</span> 8개 조건만 (고점에서 멀거나 50일선 위로 과열)</div></div>
 <p class="meta">기준일 ${esc(S.asOf)} ${S.intraday?'<b class="down">장중 시세</b>':'종가'} · 스캔 ${fmt(S.universe)}종목${S.missing>S.universe*0.05?` · <span class="down">시세 누락 ${fmt(S.missing)}</span>`:''}</p>
 <section class="sec" id="secA"><h2>A등급 · 미너비니 판정 <span class="cnt">${A.length}종목</span></h2>
 <p class="note">차트·가격 위치·실적을 모두 충족한 핵심 후보입니다. 매수 검토 → 관망 순, 피벗에 가까운 순. 종목을 누르면 상세가 펼쳐집니다.</p>
 ${A.length?`<div class="cards">${A.map(r=>card(m,r)).join('')}</div>`:'<p class="empty">오늘은 A등급 종목이 없습니다. 아래 B등급과 패턴 후보를 참고하세요.</p>'}</section>
 <details class="fold" ${vp.length&&!A.length?'open':''}><summary>VCP·베이스 패턴 후보<span class="cnt">${vp.length}</span></summary><div class="in"><p class="note">A등급 외에 RS ${S.patRs??85} 이상이면서 피벗 근처에 베이스가 있는 종목(실적이나 가격 위치 조건은 미달).</p>
 ${vp.length?vp.sort((a,b)=>Math.abs(a.patPivot??a.toPivot)-Math.abs(b.patPivot??b.toPivot)).map(r=>row(m,r,`RS ${r.rs}<br><span class="cd">피벗 ${pct(r.patPivot??r.toPivot)}</span>`)).join(''):'<p class="empty">없음</p>'}</div></details>
 <details class="fold"><summary>신규 진입 · 이탈<span class="cnt">${ch?ch.length:'—'}</span></summary><div class="in"><p class="note">직전 거래일과 비교해 8개 조건을 새로 충족했거나 깨진 종목.</p>
 ${!ch?'<p class="empty">비교할 직전 결과가 아직 없습니다.</p>':ch.length?ch.map(r=>row(m,r,`<span class="pill ${r.change==='진입'?'in':'out'}">${r.change}</span><br><span class="cd">${r.score==null?'6개 이하':r.score+'/8'}</span>`)).join(''):'<p class="empty">변동 없음</p>'}</div></details>
 <details class="fold" id="allBox"><summary>전체 목록<span class="cnt">${M.rows.length}</span></summary><div class="in">
 <div class="controls"><select id="f"><option value="B">A·B등급</option><option value="Bo">B등급만</option><option value="8">8개 모두 충족</option><option value="7">7개 이상</option></select><input id="q" type="search" placeholder="종목명·코드"></div><div id="list"></div></div></details>`;
 const f=$('#f'),q=$('#q');f.value=st[m].f;q.value=st[m].q;f.onchange=()=>{st[m].f=f.value;list();};q.oninput=()=>{st[m].q=q.value.trim().toLowerCase();list();};list();
 document.querySelectorAll('.stat[data-go]').forEach(e=>e.onclick=()=>go(e.dataset.go));}
function go(k){if(k==='A'){$('#secA').scrollIntoView({behavior:'smooth'});return;}st[st.m].f=k;$('#f').value=k;list();const b=$('#allBox');b.open=true;b.scrollIntoView({behavior:'smooth'});}
function list(){const m=st.m,o=st[m];let rows=D[m].rows.slice();
 if(o.f==='Bo')rows=rows.filter(r=>r.grade==='B');if(o.f==='B')rows=rows.filter(r=>r.grade==='A'||r.grade==='B');if(o.f==='8')rows=rows.filter(r=>r.score===8);
 if(o.q)rows=rows.filter(r=>(r.n+' '+r.s).toLowerCase().includes(o.q));
 const GO={A:0,B:1,C:2};rows.sort((a,b)=>(GO[a.grade]??3)-(GO[b.grade]??3)||b.rs-a.rs);
 $('#list').innerHTML=rows.length?rows.slice(0,300).map(r=>row(m,r,`RS ${r.rs}<br><span class="cd">${pct(r.eps,0)} / ${pct(r.rev,0)}</span>`)).join('')+(rows.length>300?`<p class="empty">상위 300개만 표시</p>`:''):'<p class="empty">조건에 맞는 종목이 없습니다.</p>';}
function breadth(){const el=$('#breadth');const ser=['kr','us'].map((m,i)=>({m,c:i?'#7a6fd1':'var(--accent)',pts:hist(m).map(h=>({d:new Date(h.asOf+'T00:00:00Z'),v:h.pass,a:h.asOf}))})).filter(s=>s.pts.length);
 if(!ser.length){el.innerHTML='<p class="empty">기록이 쌓이면 추이가 그려집니다.</p>';return;}
 const W=Math.max(300,el.clientWidth||600),H=180,ml=36,mr=64,mt=10,mb=22,all=ser.flatMap(s=>s.pts);
 let d0=Math.min(...all.map(p=>+p.d)),d1=Math.max(...all.map(p=>+p.d));if(d0===d1){d0-=3*864e5;d1+=3*864e5;}
 const vmax=Math.max(1,...all.map(p=>p.v))*1.15,x=d=>ml+(d-d0)/(d1-d0)*(W-ml-mr),y=v=>H-mb-v/vmax*(H-mt-mb);
 let s=`<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}">`+[0,.5,1].map(t=>Math.round(vmax*t)).map(t=>`<line x1="${ml}" x2="${W-mr}" y1="${y(t)}" y2="${y(t)}" stroke="var(--line)"/><text x="${ml-6}" y="${y(t)+4}" text-anchor="end">${t}</text>`).join('');
 const nx=Math.min(5,Math.max(2,Math.floor(W/110)));for(let i=0;i<nx;i++){const d=d0+(d1-d0)*i/(nx-1),dd=new Date(d);s+=`<text x="${x(d)}" y="${H-5}" text-anchor="middle">${dd.getUTCMonth()+1}/${dd.getUTCDate()}</text>`;}
 for(const se of ser){s+=`<path d="${se.pts.map((p,i)=>(i?'L':'M')+x(+p.d)+' '+y(p.v)).join('')}" fill="none" stroke="${se.c}" stroke-width="2" stroke-linejoin="round"/>`;
  const l=se.pts[se.pts.length-1];s+=`<circle cx="${x(+l.d)}" cy="${y(l.v)}" r="3" fill="${se.c}"/><text x="${x(+l.d)+7}" y="${y(l.v)+4}" style="fill:${se.c}">${NAMES[se.m]} ${l.v}</text>`;}
 el.innerHTML=s+'</svg>';}
(function(){$('#gen').textContent=D.generatedAt.replace('T',' ').slice(5,16)+' 갱신';
 if(!D.kr.summary&&D.us.summary)st.m='us';
 document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{st.m=t.dataset.m;render();window.scrollTo({top:0});});
 render();$('#breadthBox').addEventListener('toggle',breadth);})();
</script></body></html>"""


# ---------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser(description="트렌드 템플릿 스캐너")
    ap.add_argument("market", choices=["kr", "us", "all", "report"], nargs="?", default="all")
    ap.add_argument("--open", action="store_true", help="끝나면 결과 화면 열기")
    ap.add_argument("--site", help="휴대폰용 웹앱 파일을 만들 폴더 (GitHub Pages용)")
    args = ap.parse_args()
    load_config()
    LOGS.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOGS / "scan.log", encoding="utf-8")] + ([logging.StreamHandler(sys.stdout)] if sys.stdout else []))
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    failed = False
    if args.market != "report":
        for m in (["kr", "us"] if args.market == "all" else [args.market]):
            try:
                summary, rows = scan(m)
                save(m, summary, rows)
            except Exception:
                failed = True
                log.exception("[%s] 스캔 실패", m)
    path = build_report(args.site)
    log.info("결과 화면: %s", path)
    if args.open:
        webbrowser.open(path.as_uri())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

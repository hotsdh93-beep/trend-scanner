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
    if c.get("us_min_cap_eok_usd"):
        SETTINGS["us"]["min_cap"] = float(c["us_min_cap_eok_usd"]) * 1e8  # 억 달러


RS_MIN = 70            # 8번째 조건: RS 70 이상
KEEP_NEAR_RS = 80      # 7개 충족 종목은 RS 80 이상만 결과표에 남김
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
        if len(df) > 1000:
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
            "volRatio": _r(a["vol"], 2), "vcp": 1 if (v["ok"] and score == 8) else 0,
            "vcpDepths": "/".join(f"{d:g}" for d in v["depths"]), "toPivot": _r(v["toPivot"]), "dryUp": _r(v["dryUp"], 2),
            "spark": a["spark"],
        })
    rows.sort(key=lambda r: (-r["score"], -r["rs"]))
    summary = {
        "market": market, "asOf": as_of, "scannedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "universe": len(uni), "analyzed": len(valid), "missing": len([u for u in uni if u["s"] not in prices]),
        "pass": int(sum(r["score"] == 8 for r in rows)), "near": int(near_all), "vcp": int(sum(r["vcp"] for r in rows)),
        "patterns": int(sum(1 for r in rows if r["score"] == 8 and r["pattern"] and r["pattern"] != "컵 형성")),
        "seconds": round(time.time() - t0),
        "intraday": bool(as_of and market_open_now(market, as_of)),
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
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>트렌드 템플릿 스캐너</title>
<link rel="manifest" href="manifest.webmanifest"><link rel="icon" href="icon-192.png"><link rel="apple-touch-icon" href="icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="트렌드스캐너"><meta name="theme-color" content="#2f6b4f">
<style>
:root{--bg:#f6f7f5;--panel:#fff;--fg:#1d2420;--muted:#66706a;--line:#dfe3df;--accent:#2f6b4f;--up:#1f7a4d;--down:#c0392b;--chip:#eef2ee;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121513;--panel:#1a1e1c;--fg:#e6ebe7;--muted:#98a29c;--line:#2c322f;--accent:#6fbf94;--up:#57c08a;--down:#ef6f61;--chip:#232926;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Pretendard","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-left))}
h1{font-size:24px;margin:0;font-weight:700;letter-spacing:-.01em}h2{font-size:15px;margin:0;font-weight:600}
.take{color:var(--muted);margin:6px 0 0}.meta{color:var(--muted);font-size:12px;margin:0}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:20px 0 16px}
.tab{background:none;border:0;border-bottom:2px solid transparent;margin-bottom:-1px;padding:8px 14px;font:inherit;color:var(--muted);cursor:pointer}
.tab[aria-selected=true]{color:var(--fg);border-bottom-color:var(--accent);font-weight:600}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px;margin-top:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0;display:flex;flex-direction:column;gap:8px}
.lbl{font-size:12px;color:var(--muted)}.big{font-size:30px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.1}
.sub{font-size:12px;color:var(--muted)}.up{color:var(--up)}.down{color:var(--down)}
.note{font-size:12px;color:var(--muted);margin:0}
.scroll{overflow:auto;max-height:560px}.scroll.short{max-height:330px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{position:sticky;top:0;background:var(--panel);text-align:left;font-size:12px;font-weight:600;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;z-index:1}
td{padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:middle}
.num{text-align:right}th.sortable{cursor:pointer}th.sortable:hover{color:var(--fg)}
td.name{white-space:normal;min-width:120px}td.name a{color:var(--fg);text-decoration:none}td.name a:hover{text-decoration:underline}
.code{font-size:11px;color:var(--muted)}
.dots{display:inline-flex;gap:2px}.dot{width:7px;height:7px;border-radius:2px;background:var(--up)}.dot.off{background:transparent;box-shadow:inset 0 0 0 1.5px var(--down)}
.pill{display:inline-block;padding:0 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.pill.in{color:var(--up)}.pill.pat{color:#b06a00}@media (prefers-color-scheme:dark){.pill.pat{color:#f0a64a}}.pill{margin:1px 0}.pill.out{color:var(--down)}.pill.vcp{color:var(--accent)}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
select,input{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.empty{color:var(--muted);font-size:12px;padding:12px 0;margin:0}
details{margin-top:20px;color:var(--muted);font-size:13px}summary{cursor:pointer;color:var(--fg)}
details li{margin:3px 0}svg text{fill:var(--muted);font-size:11px}
@media (max-width:640px){.hide-sm{display:none}}
</style></head><body><div class="wrap">
<h1>트렌드 템플릿 스캐너</h1>
<p class="take" id="take"></p>
<p class="meta" id="gen"></p>
<nav class="tabs"><button class="tab" data-m="kr">한국</button><button class="tab" data-m="us">미국</button></nav>
<div id="view"></div>
<div class="card" style="margin-top:12px"><h2>통과 종목 수 추이</h2><p class="note">스캔마다 8개 조건을 모두 충족한 종목 수. 늘면 주도주가 넓어지고, 줄면 시장이 약해지는 신호입니다.</p><div id="breadth"></div></div>
<details><summary>스캔 기준</summary><ol>
<li>주가가 150일·200일 이동평균선 위</li><li>150일선이 200일선 위</li><li>200일선이 1개월(22거래일) 전보다 높음</li>
<li>50일선이 150일·200일선 위</li><li>주가가 50일선 위</li><li>주가가 52주 최저가보다 30% 이상 높음</li>
<li>주가가 52주 최고가에서 25% 이내</li><li>RS 70 이상: 3·6·9·12개월 수익률(3개월 2배 가중)을 같은 시장 안에서 백분위로 매긴 값</li></ol>
<p>VCP 후보: 8개 조건 충족 + 최근 6개월 고점 이후 조정폭이 2번 이상 차례로 작아짐(첫 조정 35% 이하, 마지막 12% 이하) + 최근 10일 거래량이 50일 평균보다 적음 + 피벗(직전 고점)까지 8% 이내.</p>
<p>베이스 패턴 (모양은 주봉, 손잡이·피벗은 일봉): <b>컵앤핸들</b> 30% 이상 오른 뒤 12~35% 깊이·6~65주의 U자 컵, 오른쪽이 왼쪽 고점 90% 이상 회복, 1~4주·12% 이내 손잡이가 컵 위쪽 절반에 형성(손잡이 전이면 '컵 형성'). <b>플랫 베이스</b> 20% 이상 오른 뒤 5주 이상 고저 폭 15% 이내. <b>이중 바닥</b> 30% 이상 오른 뒤 12~40% 깊이·7주 이상, 두 번째 바닥이 첫 바닥을 8% 이내로 살짝 깨고 반등, 피벗은 W 가운데 고점. <b>하이 타이트 플래그</b> 8주 안에 90% 이상 급등 후 3~25일간 25% 이내 조정. 피벗 근처(아래 15% ~ 돌파 후 5%)인 경우만 표시합니다. 규칙으로 찾은 근사치이니 최종 판단은 차트로 하세요.</p>
<p>가격: 한국은 KRX 공식 종가, 미국은 정규장 종가입니다(시간외 거래 제외). 장중에 스캔하면 15~20분 지연된 장중 시세로 계산하고 화면에 '장중 시세'로 표시합니다. 한국 종목명·시장 구분은 KRX 공식 상장 목록을 씁니다.</p>
<p>스캔 대상: 한국은 시가총액 3,000억 원 이상 보통주(우선주·스팩 제외), 미국은 시가총액 20억 달러·주가 10달러·3개월 평균 거래량 20만 주 이상인 NYSE·NASDAQ 상장 주식. 표에는 8개 모두 충족한 종목과, 7개 충족하면서 RS 80 이상인 종목을 담습니다. 시세 출처: Yahoo Finance.</p>
<p>매매 추천이 아니라 차트를 직접 확인할 후보를 좁히는 도구입니다.</p></details>
</div>
<script>
const D=/*__DATA__*/null;
const NAMES={kr:'한국',us:'미국'};
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n==null?'—':Number(n).toLocaleString('ko-KR');
const pct=(v,d=1)=>v==null?'—':(v>0?'+':'')+Number(v).toFixed(d)+'%';
const price=(m,v)=>v==null?'—':m==='kr'?Math.round(v).toLocaleString('ko-KR'):'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const code=s=>s.replace(/\.(KS|KQ)$/,'');
const link=(m,s)=>m==='kr'?'https://finance.naver.com/item/fchart.naver?code='+code(s):'https://finance.yahoo.com/quote/'+encodeURIComponent(s)+'/chart';
const st={m:'kr',kr:{sort:'rs',dir:-1,f:'8',q:''},us:{sort:'rs',dir:-1,f:'8',q:''}};
function spark(v){if(!v||v.length<2)return'';const w=96,h=26,p=3,x=i=>p+i*(w-2*p)/(v.length-1),y=t=>h-p-t/100*(h-2*p);
 const c=v[v.length-1]>=v[0]?'var(--up)':'var(--down)';return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path d="${v.map((t,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(t).toFixed(1)).join('')}" fill="none" stroke="${c}" stroke-width="1.5" stroke-linejoin="round"/><circle cx="${x(v.length-1)}" cy="${y(v[v.length-1])}" r="2.2" fill="${c}"/></svg>`;}
function pills(t){return t?t.split(' · ').map(x=>`<span class="pill ${x==='컵 형성'?'':'pat'}">${esc(x)}</span>`).join(' '):'';}
function nameCell(m,r){const ex=m==='kr'?(r.ex==='KSC'?' · 코스피':' · 코스닥'):'';return `<td class="name"><a href="${link(m,r.s)}" target="_blank" rel="noopener">${esc(r.n)}</a><div class="code">${esc(code(r.s))}${ex}</div></td>`;}
function changes(m){const M=D[m];if(!M.prev)return null;const was=new Map(M.prev.filter(r=>r.score===8).map(r=>[r.s,r]));const now=new Map(M.rows.map(r=>[r.s,r]));const out=[];
 for(const r of M.rows)if(r.score===8&&!was.has(r.s))out.push({...r,change:'진입'});
 for(const [s,r] of was){const c=now.get(s);if(!c||c.score<8)out.push({s,n:r.n,ex:c?.ex,change:'이탈',score:c?c.score:null,rs:c?c.rs:null});}
 return out.sort((a,b)=>a.change===b.change?(b.rs??0)-(a.rs??0):a.change==='진입'?-1:1);}
function hist(m){return D.history.filter(h=>h.market===m);}
function render(){
 document.querySelectorAll('.tab').forEach(t=>t.setAttribute('aria-selected',t.dataset.m===st.m));
 const m=st.m,M=D[m],S=M.summary,v=$('#view');
 if(!S){v.innerHTML=`<p class="empty">${NAMES[m]} 스캔 결과가 아직 없습니다. "지금 스캔.bat"을 실행하세요.</p>`;return;}
 const h=hist(m),prevH=h.length>1?h[h.length-2]:null,dPass=prevH?S.pass-prevH.pass:null,ch=changes(m);
 const nIn=ch?ch.filter(c=>c.change==='진입').length:null,nOut=ch?ch.filter(c=>c.change==='이탈').length:null;
 const vc=M.rows.filter(r=>r.vcp).sort((a,b)=>a.toPivot-b.toPivot);
 const pt=M.rows.filter(r=>r.score===8&&r.pattern&&r.pattern!=='컵 형성').sort((a,b)=>Math.abs(a.patPivot)-Math.abs(b.patPivot));
 v.innerHTML=`<div class="grid4">
 <div class="card"><div class="lbl">8개 조건 모두 충족</div><div class="big">${fmt(S.pass)}</div><div class="sub ${dPass>0?'up':dPass<0?'down':''}">${dPass==null?'직전 스캔 없음':'직전 대비 '+(dPass>0?'+':'')+dPass}</div></div>
 <div class="card"><div class="lbl">VCP 후보</div><div class="big">${fmt(S.vcp)}</div><div class="sub">돌파 지점 근처의 수축 패턴</div></div>
 <div class="card"><div class="lbl">베이스 패턴</div><div class="big">${fmt(S.patterns??pt.length)}</div><div class="sub">피벗 근처의 컵·플랫·W·플래그</div></div>
 <div class="card"><div class="lbl">신규 진입</div><div class="big">${nIn==null?'—':fmt(nIn)}</div><div class="sub ${nOut>0?'down':''}">${nOut==null?'비교할 직전 스캔 없음':'이탈 '+nOut}</div></div>
 <div class="card"><div class="lbl">1개 조건만 부족</div><div class="big">${fmt(S.near)}</div><div class="sub">관찰 대상</div></div></div>
 <p class="meta" style="margin-top:10px">기준일 ${esc(S.asOf)} ${S.intraday?'<b class="down">장중 시세</b>(15~20분 지연, 장 마감 후 스캔에서 종가로 바뀜)':m==='kr'?'종가(KRX 공식 종가)':'정규장 종가(시간외 거래 제외)'} · 스캔 대상 ${fmt(S.universe)}종목 중 ${fmt(S.analyzed)}종목 분석${S.missing>S.universe*0.05?` · <span class="down">시세를 못 받은 종목 ${fmt(S.missing)}개</span>`:''}</p>
 <div class="grid2">
 <div class="card"><h2>VCP 후보</h2><p class="note">조정폭이 점점 줄고 거래량이 마르며 피벗에 다가선 종목. 피벗 돌파와 거래량 급증을 확인하세요.</p>
  <div class="scroll short">${vc.length?`<table><thead><tr><th>종목</th><th>수축 단계(%)</th><th class="num">피벗까지</th><th class="num hide-sm">거래량 10/50일</th><th>최근 6개월</th></tr></thead><tbody>${vc.map(r=>`<tr>${nameCell(m,r)}<td>${esc(r.vcpDepths.split('/').join(' → '))}</td><td class="num">${pct(r.toPivot)}</td><td class="num hide-sm">${r.dryUp?.toFixed(2)??'—'}</td><td>${spark(r.spark)}</td></tr>`).join('')}</tbody></table>`:'<p class="empty">오늘은 조건에 맞는 VCP 후보가 없습니다.</p>'}</div></div>
 <div class="card"><h2>신규 진입 · 이탈</h2><p class="note">직전 거래일 스캔과 비교해 8개 조건을 새로 모두 충족했거나 더는 충족하지 않는 종목.</p>
  <div class="scroll short">${!ch?'<p class="empty">비교할 직전 거래일 결과가 아직 없습니다. 다음 거래일 스캔부터 표시됩니다.</p>':ch.length?`<table><thead><tr><th>구분</th><th>종목</th><th class="num">충족</th><th class="num">RS</th></tr></thead><tbody>${ch.map(r=>`<tr><td><span class="pill ${r.change==='진입'?'in':'out'}">${r.change}</span></td>${nameCell(m,r)}<td class="num">${r.score==null?'6개 이하':r.score+'/8'}</td><td class="num">${r.rs??'—'}</td></tr>`).join('')}</tbody></table>`:'<p class="empty">직전 스캔과 달라진 종목이 없습니다.</p>'}</div></div></div>
 <div class="card" style="margin-top:12px"><h2>베이스 패턴 후보</h2><p class="note">8개 조건을 충족하면서 피벗 근처(아래 15% ~ 돌파 후 5%)에 있는 베이스. 모양은 주봉, 손잡이·피벗은 일봉으로 판별했습니다. 피벗에 가까운 순.</p>
  <div class="scroll short">${pt.length?`<table><thead><tr><th>종목</th><th>패턴</th><th class="hide-sm">내용</th><th class="num">피벗까지</th><th>최근 6개월</th></tr></thead><tbody>${pt.map(r=>`<tr>${nameCell(m,r)}<td>${pills(r.pattern)}</td><td class="hide-sm" style="white-space:normal">${esc(r.patNote)}</td><td class="num">${pct(r.patPivot)}</td><td>${spark(r.spark)}</td></tr>`).join('')}</tbody></table>`:'<p class="empty">오늘은 피벗 근처의 베이스 패턴이 없습니다.</p>'}</div></div>
 <div class="card" style="margin-top:12px"><div class="controls"><h2>전체 후보</h2><div class="controls">
  <select id="f"><option value="8">8개 모두 충족</option><option value="7">7개 이상 충족</option><option value="vcp">VCP 후보만</option><option value="pat">베이스 패턴만</option></select>
  <input id="q" type="search" placeholder="종목명·코드 검색"></div></div>
  <p class="note">조건 칸의 네모 8개는 아래 "스캔 기준" 순서입니다. 빈 네모가 미충족 조건. 종목명을 누르면 차트가 열립니다.</p>
  <div class="scroll"><table id="main"><thead><tr>
  <th class="sortable" data-k="n">종목</th><th class="sortable num" data-k="p">종가</th><th class="sortable num" data-k="chg">등락</th><th class="sortable num" data-k="rs">RS</th>
  <th class="sortable" data-k="score">조건</th><th class="sortable num" data-k="fromHi">고점 대비</th><th class="sortable num hide-sm" data-k="vs50">50일선 대비</th>
  <th class="sortable num hide-sm" data-k="volRatio">거래량 10/50일</th><th class="sortable" data-k="vcp">VCP·패턴</th><th class="hide-sm">최근 6개월</th></tr></thead><tbody></tbody></table>
  <p class="empty" id="none" hidden>조건에 맞는 종목이 없습니다.</p></div></div>`;
 const f=$('#f'),q=$('#q');f.value=st[m].f;q.value=st[m].q;
 f.onchange=()=>{st[m].f=f.value;table();};q.oninput=()=>{st[m].q=q.value.trim().toLowerCase();table();};
 document.querySelectorAll('#main th.sortable').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(st[m].sort===k)st[m].dir*=-1;else{st[m].sort=k;st[m].dir=k==='n'?1:-1;}table();});
 table();}
function table(){const m=st.m,o=st[m];let rows=D[m].rows.slice();
 if(o.f==='8')rows=rows.filter(r=>r.score===8);if(o.f==='vcp')rows=rows.filter(r=>r.vcp);if(o.f==='pat')rows=rows.filter(r=>r.pattern);
 if(o.q)rows=rows.filter(r=>(r.n+' '+r.s).toLowerCase().includes(o.q));
 rows.sort((a,b)=>o.sort==='n'?o.dir*a.n.localeCompare(b.n,'ko'):(o.dir*((a[o.sort]??-1e9)-(b[o.sort]??-1e9))||b.rs-a.rs));
 document.querySelectorAll('#main th.sortable').forEach(th=>th.setAttribute('aria-sort',th.dataset.k===o.sort?(o.dir>0?'ascending':'descending'):'none'));
 $('#none').hidden=rows.length>0;
 $('#main tbody').innerHTML=rows.map(r=>`<tr>${nameCell(m,r)}<td class="num">${price(m,r.p)}</td><td class="num ${r.chg>0?'up':r.chg<0?'down':''}">${pct(r.chg,2)}</td><td class="num">${r.rs}</td>
 <td><span class="dots" title="8개 중 ${r.score}개 충족">${[...r.crit].map((b,i)=>`<span class="dot${b==='1'?'':' off'}" title="조건 ${i+1}"></span>`).join('')}</span></td>
 <td class="num">${pct(r.fromHi)}</td><td class="num hide-sm">${pct(r.vs50)}</td><td class="num hide-sm">${r.volRatio?.toFixed(2)??'—'}</td>
 <td style="white-space:normal">${r.vcp?'<span class="pill vcp">VCP</span> ':''}${pills(r.pattern)}</td><td class="hide-sm">${spark(r.spark)}</td></tr>`).join('');}
function breadth(){const el=$('#breadth');const ser=['kr','us'].map((m,i)=>({m,c:i?'#7a6fd1':'var(--accent)',pts:hist(m).map(h=>({d:new Date(h.asOf+'T00:00:00Z'),v:h.pass,a:h.asOf}))})).filter(s=>s.pts.length);
 if(!ser.length){el.innerHTML='<p class="empty">기록이 쌓이면 여기에 추이가 그려집니다.</p>';return;}
 const W=Math.max(300,el.clientWidth||800),H=200,ml=40,mr=70,mt=10,mb=24,all=ser.flatMap(s=>s.pts);
 let d0=Math.min(...all.map(p=>+p.d)),d1=Math.max(...all.map(p=>+p.d));if(d0===d1){d0-=3*864e5;d1+=3*864e5;}
 const vmax=Math.max(1,...all.map(p=>p.v))*1.15,x=d=>ml+(d-d0)/(d1-d0)*(W-ml-mr),y=v=>H-mb-v/vmax*(H-mt-mb);
 const ticks=[0,.25,.5,.75,1].map(t=>Math.round(vmax*t));
 let s=`<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}">`+ticks.map(t=>`<line x1="${ml}" x2="${W-mr}" y1="${y(t)}" y2="${y(t)}" stroke="var(--line)"/><text x="${ml-6}" y="${y(t)+4}" text-anchor="end">${t}</text>`).join('');
 const nx=Math.min(6,Math.max(2,Math.floor(W/120)));for(let i=0;i<nx;i++){const d=d0+(d1-d0)*i/(nx-1),dd=new Date(d);s+=`<text x="${x(d)}" y="${H-6}" text-anchor="middle">${dd.getUTCMonth()+1}/${dd.getUTCDate()}</text>`;}
 for(const se of ser){s+=`<path d="${se.pts.map((p,i)=>(i?'L':'M')+x(+p.d)+' '+y(p.v)).join('')}" fill="none" stroke="${se.c}" stroke-width="2" stroke-linejoin="round"/>`;
  if(se.pts.length<40)s+=se.pts.map(p=>`<circle cx="${x(+p.d)}" cy="${y(p.v)}" r="3" fill="${se.c}"><title>${p.a} ${NAMES[se.m]} ${p.v}종목</title></circle>`).join('');
  const l=se.pts[se.pts.length-1];s+=`<text x="${x(+l.d)+8}" y="${y(l.v)+4}" style="fill:${se.c}">${NAMES[se.m]} ${l.v}</text>`;}
 el.innerHTML=s+'</svg>';}
(function(){const parts=['kr','us'].filter(m=>D[m].summary).map(m=>`${NAMES[m]} ${fmt(D[m].summary.pass)}종목(VCP ${D[m].summary.vcp})`);
 $('#take').textContent=parts.length?parts.join(', ')+'이 트렌드 템플릿 8개 조건을 모두 충족합니다. VCP 후보부터 차트를 확인하세요.':'아직 스캔 결과가 없습니다.';
 $('#gen').textContent='화면 생성 '+D.generatedAt.replace('T',' ').slice(0,16);
 if(!D.kr.summary&&D.us.summary)st.m='us';
 document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{st.m=t.dataset.m;render();});
 render();breadth();addEventListener('resize',breadth);})();
</script></body></html>
"""


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

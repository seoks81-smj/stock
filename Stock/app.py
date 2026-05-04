"""
눌림목 스크리너 - Flask 백엔드
================================
한국 주식 눌림목 패턴을 분석하는 웹 API 서버

엔드포인트:
    GET /                   - 메인 페이지
    GET /api/analyze/<code> - 단일 종목 분석
    GET /api/watchlist      - 관심종목 일괄 분석
    GET /api/health         - 헬스체크
"""

import os
import time
from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from pykrx import stock


app = Flask(__name__)
CORS(app)

# ============================================================
# 설정
# ============================================================

LOOKBACK_DAYS = 120

CONFIG = {
    "ma20_uptrend_days": 5,
    "min_pullback_pct": 5.0,
    "max_pullback_pct": 25.0,
    "fib_min": 0.236,
    "fib_max": 0.618,
    "ma_support_tolerance": 3.0,
    "volume_dry_ratio": 0.7,
    "min_rise_pct": 15.0,
}


# ============================================================
# 데이터 가져오기 (캐시 적용)
# ============================================================

def get_date_range(days=LOOKBACK_DAYS):
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


# 5분 캐시 (같은 종목 반복 요청 방지)
_cache = {}
CACHE_TTL = 300  # 5분


def fetch_ohlcv_cached(ticker):
    """캐시 적용된 OHLCV 데이터 조회"""
    now = time.time()

    if ticker in _cache:
        cached_data, cached_time = _cache[ticker]
        if now - cached_time < CACHE_TTL:
            return cached_data

    start_date, end_date = get_date_range()
    try:
        df = stock.get_market_ohlcv(start_date, end_date, ticker)
        if df is None or len(df) < 60:
            return None
        df.columns = ["open", "high", "low", "close", "volume", "change"]
        _cache[ticker] = (df, now)
        return df
    except Exception:
        return None


@lru_cache(maxsize=500)
def get_ticker_name_cached(ticker):
    try:
        return stock.get_market_ticker_name(ticker)
    except Exception:
        return ticker


# ============================================================
# 기술적 분석
# ============================================================

def calculate_indicators(df):
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma120"] = df["close"].rolling(120).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def find_swing(df, window=60):
    recent = df.tail(window)
    high_idx = recent["close"].idxmax()
    high_price = recent["close"].max()

    before_high = recent.loc[:high_idx]
    if len(before_high) < 5:
        return None
    low_idx = before_high["close"].idxmin()
    low_price = before_high["close"].min()

    return {
        "swing_low_date": low_idx,
        "swing_low_price": float(low_price),
        "swing_high_date": high_idx,
        "swing_high_price": float(high_price),
        "rise_pct": float((high_price - low_price) / low_price * 100),
    }


def analyze_pullback(df, ticker_name="", ticker=""):
    """눌림목 분석 - 점수와 상세 정보 반환"""
    if df is None or len(df) < 120:
        return {"error": "데이터 부족 (최소 120일)"}

    df = calculate_indicators(df)
    latest = df.iloc[-1]

    score = 0
    reasons = []
    warnings = []

    # 1. 추세 확인 (30점)
    ma20_recent = df["ma20"].tail(CONFIG["ma20_uptrend_days"])
    if ma20_recent.is_monotonic_increasing:
        score += 10
        reasons.append("20일선 우상향")
    elif ma20_recent.iloc[-1] > ma20_recent.iloc[0]:
        score += 5
        reasons.append("20일선 약한 상승")
    else:
        warnings.append("20일선 하락")

    if (latest["ma5"] > latest["ma20"] > latest["ma60"] > latest["ma120"]):
        score += 10
        reasons.append("이평선 완전 정배열")
    elif (latest["ma5"] > latest["ma20"] > latest["ma60"]):
        score += 7
        reasons.append("이평선 정배열")
    elif (latest["ma20"] > latest["ma60"]):
        score += 4
        reasons.append("중기 상승 추세")
    else:
        warnings.append("이평선 정배열 아님")

    swing = find_swing(df, window=60)
    if swing is None:
        return {"error": "스윙 분석 불가"}

    if swing["rise_pct"] >= CONFIG["min_rise_pct"]:
        score += 10
        reasons.append(f"직전 상승 +{swing['rise_pct']:.1f}%")
    elif swing["rise_pct"] >= 10:
        score += 5

    # 2. 조정 깊이 (25점)
    high_price = swing["swing_high_price"]
    low_price = swing["swing_low_price"]
    current_price = float(latest["close"])

    pullback_pct = (high_price - current_price) / high_price * 100
    fib_ratio = (high_price - current_price) / (high_price - low_price) if high_price > low_price else 0

    if CONFIG["min_pullback_pct"] <= pullback_pct <= CONFIG["max_pullback_pct"]:
        score += 15
        reasons.append(f"적정 조정폭 -{pullback_pct:.1f}%")
    elif pullback_pct < CONFIG["min_pullback_pct"]:
        score += 5
        warnings.append(f"조정 부족 -{pullback_pct:.1f}%")
    else:
        warnings.append(f"조정 과다 -{pullback_pct:.1f}%")

    if CONFIG["fib_min"] <= fib_ratio <= CONFIG["fib_max"]:
        score += 10
        reasons.append(f"피보나치 {fib_ratio*100:.0f}% 되돌림")
    elif fib_ratio < CONFIG["fib_min"]:
        score += 3

    # 3. 이평선 지지 (20점)
    tolerance = CONFIG["ma_support_tolerance"] / 100
    ma5_dist = abs(current_price - latest["ma5"]) / latest["ma5"]
    ma20_dist = abs(current_price - latest["ma20"]) / latest["ma20"]
    ma60_dist = abs(current_price - latest["ma60"]) / latest["ma60"]

    if ma5_dist <= tolerance and current_price >= latest["ma5"] * 0.99:
        score += 20
        reasons.append("5일선 지지 (강한 종목)")
    elif ma20_dist <= tolerance and current_price >= latest["ma20"] * 0.97:
        score += 18
        reasons.append("20일선 지지 (생명선)")
    elif ma60_dist <= tolerance and current_price >= latest["ma60"] * 0.97:
        score += 12
        reasons.append("60일선 지지")
    elif latest["ma5"] >= current_price >= latest["ma20"] * 0.97:
        score += 10
        reasons.append("5-20일선 사이")

    # 4. 거래량 패턴 (15점)
    pullback_start_idx = df.index.get_loc(swing["swing_high_date"])
    pullback_volumes = df.iloc[pullback_start_idx:]["volume"]

    if len(pullback_volumes) >= 3:
        avg_volume = df["vol_ma20"].iloc[pullback_start_idx]
        recent_avg_volume = pullback_volumes.tail(5).mean()

        if avg_volume > 0:
            vol_ratio = recent_avg_volume / avg_volume
            if vol_ratio < CONFIG["volume_dry_ratio"]:
                score += 15
                reasons.append(f"거래량 감소 ({vol_ratio*100:.0f}%)")
            elif vol_ratio < 1.0:
                score += 8
                reasons.append(f"거래량 보통 ({vol_ratio*100:.0f}%)")
            else:
                warnings.append(f"거래량 증가 ({vol_ratio*100:.0f}%)")

    # 5. 반등 신호 (10점)
    recent_3 = df.tail(3)
    bullish_days = recent_3[recent_3["close"] > recent_3["open"]]
    if len(bullish_days) > 0:
        last_bullish = bullish_days.iloc[-1]
        avg_vol = df["vol_ma20"].iloc[-1]
        if avg_vol > 0 and last_bullish["volume"] > avg_vol * 1.2:
            score += 10
            reasons.append("거래량 동반 양봉")
        else:
            score += 5
            reasons.append("양봉 출현")

    # 차트 데이터 (최근 60일)
    chart_data = df.tail(60).copy()
    chart_data.index = chart_data.index.strftime("%Y-%m-%d")

    return {
        "ticker": ticker,
        "name": ticker_name,
        "current_price": int(current_price),
        "change_pct": float(latest.get("change", 0)) if pd.notna(latest.get("change", 0)) else 0,
        "score": score,
        "grade": get_grade(score),
        "high_price": int(high_price),
        "low_price": int(low_price),
        "pullback_pct": round(pullback_pct, 2),
        "fib_ratio": round(fib_ratio * 100, 1),
        "rise_pct": round(swing["rise_pct"], 1),
        "ma5": int(latest["ma5"]),
        "ma20": int(latest["ma20"]),
        "ma60": int(latest["ma60"]),
        "ma120": int(latest["ma120"]),
        "rsi": round(float(latest["rsi"]), 1) if pd.notna(latest["rsi"]) else None,
        "volume": int(latest["volume"]),
        "reasons": reasons,
        "warnings": warnings,
        "stop_loss": int(latest["ma20"] * 0.97),
        "target_1": int(high_price * 0.95),
        "target_2": int(high_price),
        # 차트 데이터
        "chart": {
            "dates": chart_data.index.tolist(),
            "close": chart_data["close"].astype(int).tolist(),
            "ma5": chart_data["ma5"].fillna(0).astype(int).tolist(),
            "ma20": chart_data["ma20"].fillna(0).astype(int).tolist(),
            "ma60": chart_data["ma60"].fillna(0).astype(int).tolist(),
            "volume": chart_data["volume"].astype(int).tolist(),
        }
    }


def get_grade(score):
    if score >= 80:
        return {"label": "매우 우수", "color": "#00d4aa"}
    elif score >= 70:
        return {"label": "양호", "color": "#5dade2"}
    elif score >= 60:
        return {"label": "보통", "color": "#f4d03f"}
    elif score >= 50:
        return {"label": "주의", "color": "#eb984e"}
    else:
        return {"label": "비추천", "color": "#e74c3c"}


# ============================================================
# 라우트
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/analyze/<ticker>")
def analyze(ticker):
    """단일 종목 분석"""
    ticker = ticker.strip().zfill(6)

    df = fetch_ohlcv_cached(ticker)
    if df is None:
        return jsonify({"error": f"종목 {ticker} 데이터를 찾을 수 없습니다"}), 404

    name = get_ticker_name_cached(ticker)
    result = analyze_pullback(df, ticker_name=name, ticker=ticker)

    if "error" in result:
        return jsonify(result), 400

    return jsonify(result)


@app.route("/api/watchlist", methods=["POST"])
def watchlist():
    """관심종목 일괄 분석"""
    data = request.get_json()
    tickers = data.get("tickers", [])

    if not tickers or len(tickers) > 30:
        return jsonify({"error": "종목은 1~30개까지 가능합니다"}), 400

    results = []
    for ticker in tickers:
        ticker = str(ticker).strip().zfill(6)
        try:
            df = fetch_ohlcv_cached(ticker)
            if df is None:
                continue
            name = get_ticker_name_cached(ticker)
            result = analyze_pullback(df, ticker_name=name, ticker=ticker)
            if "error" not in result:
                results.append(result)
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"results": results, "count": len(results)})


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

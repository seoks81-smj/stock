"""
눌림목 스크리너 - Flask 백엔드 v2
====================================
v2 추가 기능:
- 전종목 스캔 (백그라운드 비동기 작업)
- 진행 상황 실시간 조회
- 결과 캐싱 (1시간)
"""

import os
import time
import json
import uuid
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from functools import lru_cache, wraps
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, make_response
from flask_cors import CORS
from pykrx import stock

# FinanceDataReader 폴백 (pykrx 안될 때 대체)
try:
    import FinanceDataReader as fdr
    HAS_FDR = True
except ImportError:
    HAS_FDR = False
    print("FinanceDataReader 미설치 - pykrx만 사용")


app = Flask(__name__)
CORS(app)

# ============================================================
# 비밀번호 인증
# ============================================================

# 환경변수 APP_PASSWORD에서 읽음. 없으면 인증 비활성화 (개발용)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)
TOKEN_LIFETIME_HOURS = 24  # 토큰 유효 기간

# 유효한 토큰 저장 (메모리)
_valid_tokens = {}  # token -> expiry_timestamp
_tokens_lock = threading.Lock()


def hash_password(password):
    """비밀번호 해시 (간단한 방식)"""
    return hashlib.sha256(password.encode()).hexdigest()


def generate_token():
    """랜덤 토큰 생성"""
    return secrets.token_urlsafe(32)


def is_token_valid(token):
    """토큰 유효성 검사"""
    if not AUTH_ENABLED:
        return True
    if not token:
        return False
    with _tokens_lock:
        expiry = _valid_tokens.get(token)
        if expiry is None:
            return False
        if time.time() > expiry:
            # 만료된 토큰 정리
            del _valid_tokens[token]
            return False
        return True


def cleanup_expired_tokens():
    """만료된 토큰 정리 (백그라운드)"""
    with _tokens_lock:
        now = time.time()
        expired = [t for t, exp in _valid_tokens.items() if exp < now]
        for t in expired:
            del _valid_tokens[t]


def require_auth(f):
    """인증 필요 데코레이터"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AUTH_ENABLED:
            return f(*args, **kwargs)
        token = request.cookies.get("auth_token") or request.headers.get("X-Auth-Token")
        if not is_token_valid(token):
            return jsonify({"error": "인증이 필요합니다", "auth_required": True}), 401
        return f(*args, **kwargs)
    return decorated


LOOKBACK_DAYS = 120
CACHE_DIR = Path("/tmp/pullback_cache")
CACHE_DIR.mkdir(exist_ok=True, parents=True)

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

SCAN_JOBS = {}
SCAN_LOCK = threading.Lock()


def get_date_range(days=LOOKBACK_DAYS):
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


_cache = {}
CACHE_TTL = 300


def fetch_ohlcv_cached(ticker):
    now = time.time()
    if ticker in _cache:
        cached_data, cached_time = _cache[ticker]
        if now - cached_time < CACHE_TTL:
            return cached_data
    start_date, end_date = get_date_range()

    # 1차: pykrx 시도
    try:
        df = stock.get_market_ohlcv(start_date, end_date, ticker)
        if df is not None and len(df) >= 60:
            df.columns = ["open", "high", "low", "close", "volume", "change"]
            _cache[ticker] = (df, now)
            return df
    except Exception:
        pass

    # 2차: FinanceDataReader 폴백
    if HAS_FDR:
        try:
            start = datetime.strptime(start_date, "%Y%m%d")
            end = datetime.strptime(end_date, "%Y%m%d")
            df = fdr.DataReader(ticker, start, end)
            if df is not None and len(df) >= 60:
                # FDR 컬럼명을 pykrx와 통일
                df = df.rename(columns={
                    "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume", "Change": "change"
                })
                # change가 비율(0.05 = 5%)로 오므로 % 변환
                if "change" in df.columns:
                    df["change"] = df["change"] * 100
                else:
                    df["change"] = df["close"].pct_change() * 100
                _cache[ticker] = (df, now)
                return df
        except Exception:
            pass

    return None


@lru_cache(maxsize=3000)
def get_ticker_name_cached(ticker):
    # 1차: pykrx
    try:
        name = stock.get_market_ticker_name(ticker)
        if name and name != ticker:
            return name
    except Exception:
        pass
    # 2차: FDR (전체 종목 리스트에서 찾기)
    if HAS_FDR:
        try:
            for market in ["KOSPI", "KOSDAQ"]:
                df = fdr.StockListing(market)
                row = df[df["Code"].astype(str).str.zfill(6) == ticker]
                if not row.empty:
                    return row.iloc[0]["Name"]
        except Exception:
            pass
    return ticker


def get_all_tickers(market="ALL"):
    """종목 리스트 가져오기 - pykrx 우선, 실패 시 FinanceDataReader 폴백"""
    # 1차 시도: pykrx (영업일 자동 검색)
    for days_back in range(0, 8):
        try_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            if market == "ALL":
                kospi = stock.get_market_ticker_list(try_date, market="KOSPI")
                kosdaq = stock.get_market_ticker_list(try_date, market="KOSDAQ")
                if kospi and kosdaq:
                    print(f"✅ pykrx로 종목 가져옴 ({try_date}): KOSPI {len(kospi)}, KOSDAQ {len(kosdaq)}")
                    return kospi + kosdaq
            else:
                tickers = stock.get_market_ticker_list(try_date, market=market)
                if tickers:
                    print(f"✅ pykrx로 종목 가져옴 ({try_date}): {market} {len(tickers)}")
                    return tickers
        except Exception as e:
            print(f"pykrx 실패 ({try_date}): {e}")
            time.sleep(0.3)
            continue

    # 2차 시도: FinanceDataReader 폴백
    if HAS_FDR:
        try:
            print("🔄 pykrx 실패, FinanceDataReader로 재시도...")
            if market == "ALL":
                kospi_df = fdr.StockListing("KOSPI")
                kosdaq_df = fdr.StockListing("KOSDAQ")
                kospi = kospi_df["Code"].astype(str).str.zfill(6).tolist()
                kosdaq = kosdaq_df["Code"].astype(str).str.zfill(6).tolist()
                print(f"✅ FDR로 종목 가져옴: KOSPI {len(kospi)}, KOSDAQ {len(kosdaq)}")
                return kospi + kosdaq
            else:
                df = fdr.StockListing(market)
                tickers = df["Code"].astype(str).str.zfill(6).tolist()
                print(f"✅ FDR로 종목 가져옴: {market} {len(tickers)}")
                return tickers
        except Exception as e:
            print(f"FDR도 실패: {e}")

    return []


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


def analyze_pullback(df, ticker_name="", ticker="", include_chart=True):
    if df is None or len(df) < 120:
        return {"error": "데이터 부족"}

    df = calculate_indicators(df)
    latest = df.iloc[-1]
    score = 0
    reasons = []
    warnings = []

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

    tolerance = CONFIG["ma_support_tolerance"] / 100
    ma5_dist = abs(current_price - latest["ma5"]) / latest["ma5"]
    ma20_dist = abs(current_price - latest["ma20"]) / latest["ma20"]
    ma60_dist = abs(current_price - latest["ma60"]) / latest["ma60"]

    if ma5_dist <= tolerance and current_price >= latest["ma5"] * 0.99:
        score += 20
        reasons.append("5일선 지지")
    elif ma20_dist <= tolerance and current_price >= latest["ma20"] * 0.97:
        score += 18
        reasons.append("20일선 지지")
    elif ma60_dist <= tolerance and current_price >= latest["ma60"] * 0.97:
        score += 12
        reasons.append("60일선 지지")
    elif latest["ma5"] >= current_price >= latest["ma20"] * 0.97:
        score += 10
        reasons.append("5-20일선 사이")

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

    result = {
        "ticker": ticker,
        "name": ticker_name,
        "current_price": int(current_price),
        "change_pct": round(float(latest.get("change", 0)), 2) if pd.notna(latest.get("change", 0)) else 0,
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
    }

    if include_chart:
        chart_data = df.tail(60).copy()
        chart_data.index = chart_data.index.strftime("%Y-%m-%d")
        result["chart"] = {
            "dates": chart_data.index.tolist(),
            "close": chart_data["close"].astype(int).tolist(),
            "ma5": chart_data["ma5"].fillna(0).astype(int).tolist(),
            "ma20": chart_data["ma20"].fillna(0).astype(int).tolist(),
            "ma60": chart_data["ma60"].fillna(0).astype(int).tolist(),
            "volume": chart_data["volume"].astype(int).tolist(),
        }
    return result


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


def background_scan(job_id, market, min_score):
    """백그라운드에서 실행되는 전종목 스캔"""
    try:
        with SCAN_LOCK:
            SCAN_JOBS[job_id]["status"] = "fetching_tickers"

        tickers = get_all_tickers(market)
        if not tickers:
            with SCAN_LOCK:
                SCAN_JOBS[job_id]["status"] = "error"
                SCAN_JOBS[job_id]["error"] = (
                    "종목 리스트를 가져올 수 없습니다. "
                    "KRX 서버가 일시적으로 응답하지 않을 수 있습니다. "
                    "주말/공휴일이나 새벽 시간엔 불안정할 수 있으니 "
                    "평일 저녁에 다시 시도해주세요."
                )
            return

        total = len(tickers)
        with SCAN_LOCK:
            SCAN_JOBS[job_id]["status"] = "scanning"
            SCAN_JOBS[job_id]["total"] = total
            SCAN_JOBS[job_id]["processed"] = 0
            SCAN_JOBS[job_id]["found"] = 0

        results = []
        start_time = time.time()

        for i, ticker in enumerate(tickers, 1):
            with SCAN_LOCK:
                if SCAN_JOBS[job_id].get("cancel"):
                    SCAN_JOBS[job_id]["status"] = "cancelled"
                    return

            try:
                df = fetch_ohlcv_cached(ticker)
                if df is None:
                    continue
                name = get_ticker_name_cached(ticker)
                result = analyze_pullback(df, ticker_name=name, ticker=ticker, include_chart=False)
                if "error" not in result and result["score"] >= min_score:
                    results.append(result)

                if i % 10 == 0:
                    with SCAN_LOCK:
                        SCAN_JOBS[job_id]["processed"] = i
                        SCAN_JOBS[job_id]["found"] = len(results)
                        SCAN_JOBS[job_id]["elapsed"] = int(time.time() - start_time)
                time.sleep(0.03)
            except Exception:
                continue

        results.sort(key=lambda x: x["score"], reverse=True)

        with SCAN_LOCK:
            SCAN_JOBS[job_id]["status"] = "completed"
            SCAN_JOBS[job_id]["processed"] = total
            SCAN_JOBS[job_id]["found"] = len(results)
            SCAN_JOBS[job_id]["elapsed"] = int(time.time() - start_time)
            SCAN_JOBS[job_id]["results"] = results
            SCAN_JOBS[job_id]["completed_at"] = datetime.now().isoformat()

        cache_file = CACHE_DIR / f"scan_{market}_{min_score}.json"
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({
                "results": results,
                "completed_at": datetime.now().isoformat(),
                "total": total,
                "market": market,
                "min_score": min_score,
            }, f, ensure_ascii=False)

    except Exception as e:
        with SCAN_LOCK:
            SCAN_JOBS[job_id]["status"] = "error"
            SCAN_JOBS[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/robots.txt")
def robots():
    """검색엔진 봇 차단"""
    content = """User-agent: *
Disallow: /

# 모든 검색엔진의 모든 페이지 수집을 차단합니다.
# Block all search engine crawlers.
"""
    from flask import Response
    return Response(content, mimetype="text/plain")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ============================================================
# 인증 엔드포인트
# ============================================================

@app.route("/api/auth/check")
def auth_check():
    """현재 인증 상태 확인"""
    if not AUTH_ENABLED:
        return jsonify({"auth_enabled": False, "authenticated": True})
    token = request.cookies.get("auth_token") or request.headers.get("X-Auth-Token")
    return jsonify({
        "auth_enabled": True,
        "authenticated": is_token_valid(token)
    })


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """비밀번호 로그인"""
    if not AUTH_ENABLED:
        return jsonify({"success": True, "message": "인증 비활성화 상태"})

    data = request.get_json() or {}
    password = data.get("password", "")

    # 약간의 딜레이 (브루트포스 방지)
    time.sleep(0.5)

    if password != APP_PASSWORD:
        return jsonify({"success": False, "error": "비밀번호가 올바르지 않습니다"}), 401

    # 토큰 발급
    token = generate_token()
    expiry = time.time() + TOKEN_LIFETIME_HOURS * 3600
    with _tokens_lock:
        _valid_tokens[token] = expiry
        cleanup_expired_tokens()

    response = make_response(jsonify({
        "success": True,
        "token": token,
        "expires_in_hours": TOKEN_LIFETIME_HOURS
    }))
    response.set_cookie(
        "auth_token", token,
        max_age=TOKEN_LIFETIME_HOURS * 3600,
        httponly=False,  # JS에서 접근 가능 (간단한 구현)
        samesite="Lax",
    )
    return response


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """로그아웃"""
    token = request.cookies.get("auth_token") or request.headers.get("X-Auth-Token")
    if token:
        with _tokens_lock:
            _valid_tokens.pop(token, None)
    response = make_response(jsonify({"success": True}))
    response.set_cookie("auth_token", "", max_age=0)
    return response


@app.route("/api/analyze/<ticker>")
@require_auth
def analyze(ticker):
    ticker = ticker.strip()
    # 종목명으로 들어왔을 경우 코드로 변환 시도
    if not ticker.isdigit():
        resolved = resolve_ticker(ticker)
        if resolved:
            ticker = resolved
        else:
            return jsonify({"error": f"종목명 '{ticker}'을(를) 찾을 수 없습니다"}), 404
    else:
        ticker = ticker.zfill(6)

    df = fetch_ohlcv_cached(ticker)
    if df is None:
        return jsonify({"error": f"종목 {ticker} 데이터를 찾을 수 없습니다"}), 404
    name = get_ticker_name_cached(ticker)
    result = analyze_pullback(df, ticker_name=name, ticker=ticker)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


# ============================================================
# 종목 검색 (이름 → 코드 변환)
# ============================================================

_ticker_map = None
_ticker_map_lock = threading.Lock()


def build_ticker_map():
    """전체 종목 목록을 메모리에 캐싱 (이름 → 코드)"""
    global _ticker_map
    with _ticker_map_lock:
        if _ticker_map is not None:
            return _ticker_map

        result = {}
        # 1차: pykrx로 시도
        try:
            today = datetime.now().strftime("%Y%m%d")
            for market in ["KOSPI", "KOSDAQ"]:
                tickers = stock.get_market_ticker_list(today, market=market)
                for t in tickers:
                    try:
                        name = stock.get_market_ticker_name(t)
                        if name and name != t:
                            result[name] = t
                    except Exception:
                        continue
                    time.sleep(0.01)
        except Exception:
            pass

        # 2차: FDR로 보완
        if HAS_FDR and len(result) < 100:
            try:
                for market in ["KOSPI", "KOSDAQ"]:
                    df = fdr.StockListing(market)
                    for _, row in df.iterrows():
                        code = str(row["Code"]).zfill(6)
                        name = row["Name"]
                        if name and name not in result:
                            result[name] = code
            except Exception as e:
                print(f"FDR 종목 맵 빌드 실패: {e}")

        _ticker_map = result
        print(f"✅ 종목 맵 빌드 완료: {len(result)}개")
        return result


def resolve_ticker(query):
    """종목명을 종목코드로 변환 (정확 일치 우선)"""
    ticker_map = build_ticker_map()
    query = query.strip()

    # 정확 일치
    if query in ticker_map:
        return ticker_map[query]

    # 부분 일치 (대소문자 무시)
    query_lower = query.lower()
    for name, code in ticker_map.items():
        if name.lower() == query_lower:
            return code

    # 시작 일치
    for name, code in ticker_map.items():
        if name.lower().startswith(query_lower):
            return code

    return None


@app.route("/api/search")
@require_auth
def search():
    """종목명/코드 검색 자동완성"""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify({"results": []})

    ticker_map = build_ticker_map()
    q_lower = q.lower()

    matches = []

    # 코드로 검색 (숫자)
    if q.isdigit():
        for name, code in ticker_map.items():
            if code.startswith(q):
                matches.append({"code": code, "name": name})
                if len(matches) >= 10:
                    break
    else:
        # 정확 일치 우선
        exact = []
        starts = []
        contains = []
        for name, code in ticker_map.items():
            name_lower = name.lower()
            if name_lower == q_lower:
                exact.append({"code": code, "name": name})
            elif name_lower.startswith(q_lower):
                starts.append({"code": code, "name": name})
            elif q_lower in name_lower:
                contains.append({"code": code, "name": name})
        matches = (exact + starts + contains)[:10]

    return jsonify({"results": matches})


@app.route("/api/watchlist", methods=["POST"])
@require_auth
def watchlist():
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


@app.route("/api/scan/start", methods=["POST"])
@require_auth
def scan_start():
    """전종목 스캔 시작"""
    data = request.get_json() or {}
    market = data.get("market", "ALL")
    min_score = int(data.get("min_score", 60))

    with SCAN_LOCK:
        for jid, job in SCAN_JOBS.items():
            if job["status"] in ("scanning", "fetching_tickers", "starting"):
                return jsonify({
                    "job_id": jid,
                    "message": "이미 진행 중인 스캔이 있습니다",
                    "existing": True
                }), 200

        cache_file = CACHE_DIR / f"scan_{market}_{min_score}.json"
        if cache_file.exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached_time = datetime.fromisoformat(cached["completed_at"])
            age = (datetime.now() - cached_time).total_seconds()
            if age < 3600:
                job_id = str(uuid.uuid4())[:8]
                SCAN_JOBS[job_id] = {
                    "status": "completed",
                    "from_cache": True,
                    "results": cached["results"],
                    "total": cached["total"],
                    "processed": cached["total"],
                    "found": len(cached["results"]),
                    "completed_at": cached["completed_at"],
                    "elapsed": 0,
                    "market": market,
                    "min_score": min_score,
                }
                return jsonify({"job_id": job_id, "from_cache": True, "cached_at": cached["completed_at"]})

        job_id = str(uuid.uuid4())[:8]
        SCAN_JOBS[job_id] = {
            "status": "starting",
            "market": market,
            "min_score": min_score,
            "total": 0,
            "processed": 0,
            "found": 0,
            "started_at": datetime.now().isoformat(),
        }

    thread = threading.Thread(
        target=background_scan,
        args=(job_id, market, min_score),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "from_cache": False})


@app.route("/api/scan/status/<job_id>")
@require_auth
def scan_status(job_id):
    """스캔 진행 상황 조회"""
    with SCAN_LOCK:
        job = SCAN_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "작업을 찾을 수 없습니다"}), 404
        if job["status"] == "completed":
            return jsonify(job)
        else:
            response = {k: v for k, v in job.items() if k != "results"}
            return jsonify(response)


@app.route("/api/scan/cancel/<job_id>", methods=["POST"])
@require_auth
def scan_cancel(job_id):
    """스캔 취소"""
    with SCAN_LOCK:
        if job_id in SCAN_JOBS:
            SCAN_JOBS[job_id]["cancel"] = True
            return jsonify({"message": "취소 요청됨"})
    return jsonify({"error": "작업을 찾을 수 없습니다"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

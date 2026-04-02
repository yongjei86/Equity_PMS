from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import numpy as np

app = Flask(__name__)
CORS(app)


def get_price_data(ticker):
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = info.last_price
        prev = info.previous_close
        if price is None or prev is None:
            return None
        change = price - prev
        change_pct = (change / prev) * 100 if prev else 0
        name = getattr(info, 'description', None) or ticker
        # fast_info doesn't have name; use t.info for name but it's slow
        # use ticker symbol as fallback
        try:
            full_info = t.info
            name = full_info.get('longName') or full_info.get('shortName') or ticker
            currency = (full_info.get('currency') or 'USD').upper()
        except Exception:
            name = ticker
            currency = 'USD'
        return {
            'price': round(price, 6),
            'change': round(change, 6),
            'changePct': round(change_pct, 4),
            'name': name,
            'currency': currency,
            'ok': True,
        }
    except Exception as e:
        return None


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def calculate_portfolio_metrics(tickers, weights=None, period='1y', benchmark='^GSPC', risk_free_rate=0.03):
    if not tickers:
        return {'error': '최소 1개 이상의 티커가 필요합니다.'}, 400

    clean_tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not clean_tickers:
        return {'error': '유효한 티커가 없습니다.'}, 400

    if weights is None or len(weights) == 0:
        weights_arr = np.array([1 / len(clean_tickers)] * len(clean_tickers), dtype=float)
    else:
        if len(weights) != len(clean_tickers):
            return {'error': 'weights 길이는 tickers 길이와 같아야 합니다.'}, 400
        weights_arr = np.array([_to_float(w) for w in weights], dtype=float)
        if np.any(weights_arr < 0):
            return {'error': 'weights는 0 이상이어야 합니다.'}, 400
        weight_sum = weights_arr.sum()
        if weight_sum == 0:
            return {'error': 'weights 합이 0일 수 없습니다.'}, 400
        weights_arr = weights_arr / weight_sum

    valid_periods = {'1mo', '3mo', '6mo', '1y', '2y', '5y', '10y', 'ytd', 'max'}
    if period not in valid_periods:
        return {'error': f'invalid period. allowed: {sorted(valid_periods)}'}, 400

    try:
        close_prices = yf.download(
            tickers=clean_tickers,
            period=period,
            interval='1d',
            auto_adjust=True,
            progress=False
        )['Close']
    except Exception:
        return {'error': '가격 데이터를 가져오지 못했습니다.'}, 500

    if close_prices is None or len(close_prices) == 0:
        return {'error': '가격 데이터가 비어 있습니다.'}, 404

    if len(clean_tickers) == 1:
        close_prices = close_prices.to_frame(name=clean_tickers[0])
    else:
        close_prices = close_prices[clean_tickers]

    close_prices = close_prices.dropna(how='any')
    if close_prices.empty or len(close_prices) < 3:
        return {'error': '지표 계산을 위한 데이터가 충분하지 않습니다.'}, 422

    returns = close_prices.pct_change().dropna(how='any')
    portfolio_returns = (returns * weights_arr).sum(axis=1)

    benchmark_returns = None
    benchmark_period_return = None
    try:
        bench_close = yf.download(
            tickers=benchmark,
            period=period,
            interval='1d',
            auto_adjust=True,
            progress=False
        )['Close'].dropna()
        if len(bench_close) > 2:
            benchmark_returns = bench_close.pct_change().dropna()
            benchmark_period_return = _to_float(bench_close.iloc[-1] / bench_close.iloc[0] - 1)
    except Exception:
        benchmark_returns = None

    trading_days = 252
    total_days = max((portfolio_returns.index[-1] - portfolio_returns.index[0]).days, 1)
    annual_rf = _to_float(risk_free_rate, 0.03)
    daily_rf = annual_rf / trading_days

    cumulative = (1 + portfolio_returns).cumprod()
    rolling_max = cumulative.cummax()
    drawdown = (cumulative / rolling_max) - 1
    max_drawdown = _to_float(drawdown.min(), 0)

    annual_return = _to_float(portfolio_returns.mean() * trading_days)
    annual_volatility = _to_float(portfolio_returns.std(ddof=1) * np.sqrt(trading_days))
    downside = portfolio_returns[portfolio_returns < daily_rf] - daily_rf
    downside_deviation = _to_float(np.sqrt((downside ** 2).mean()) * np.sqrt(trading_days), 0)

    sharpe_ratio = None
    if annual_volatility > 0:
        sharpe_ratio = (annual_return - annual_rf) / annual_volatility

    sortino_ratio = None
    if downside_deviation > 0:
        sortino_ratio = (annual_return - annual_rf) / downside_deviation

    period_return = _to_float(cumulative.iloc[-1] - 1)
    cagr = (1 + period_return) ** (365 / total_days) - 1 if total_days > 0 else None

    var_95 = _to_float(np.percentile(portfolio_returns, 5))
    tail = portfolio_returns[portfolio_returns <= var_95]
    cvar_95 = _to_float(tail.mean()) if len(tail) > 0 else var_95

    beta = None
    alpha = None
    information_ratio = None
    correlation = None
    if benchmark_returns is not None and len(benchmark_returns) > 2:
        aligned = portfolio_returns.to_frame('p').join(benchmark_returns.to_frame('b'), how='inner').dropna()
        if len(aligned) > 2:
            cov = np.cov(aligned['p'], aligned['b'], ddof=1)[0][1]
            var_b = np.var(aligned['b'], ddof=1)
            if var_b > 0:
                beta = cov / var_b
                annual_bench_return = _to_float(aligned['b'].mean() * trading_days)
                alpha = annual_return - (annual_rf + beta * (annual_bench_return - annual_rf))
            active_return = aligned['p'] - aligned['b']
            tracking_error = _to_float(active_return.std(ddof=1) * np.sqrt(trading_days))
            if tracking_error > 0:
                information_ratio = _to_float(active_return.mean() * trading_days / tracking_error)
            correlation = _to_float(aligned['p'].corr(aligned['b']))

    result = {
        'ok': True,
        'inputs': {
            'tickers': clean_tickers,
            'weights': [round(float(x), 6) for x in weights_arr.tolist()],
            'period': period,
            'benchmark': benchmark,
            'riskFreeRate': round(annual_rf, 6),
        },
        'metrics': {
            'periodReturn': round(period_return, 6),
            'benchmarkPeriodReturn': round(benchmark_period_return, 6) if benchmark_period_return is not None else None,
            'cagr': round(float(cagr), 6) if cagr is not None else None,
            'annualReturn': round(annual_return, 6),
            'volatility': round(annual_volatility, 6),
            'sharpeRatio': round(float(sharpe_ratio), 6) if sharpe_ratio is not None else None,
            'sortinoRatio': round(float(sortino_ratio), 6) if sortino_ratio is not None else None,
            'maxDrawdown': round(max_drawdown, 6),
            'beta': round(float(beta), 6) if beta is not None else None,
            'alpha': round(float(alpha), 6) if alpha is not None else None,
            'informationRatio': round(float(information_ratio), 6) if information_ratio is not None else None,
            'correlationWithBenchmark': round(float(correlation), 6) if correlation is not None else None,
            'var95Daily': round(var_95, 6),
            'cvar95Daily': round(cvar_95, 6),
        },
    }
    return result, 200


@app.route('/api/price/<path:ticker>')
def price(ticker):
    data = get_price_data(ticker)
    if data is None:
        return jsonify({'ok': False, 'price': 0, 'change': 0, 'changePct': 0,
                        'name': ticker, 'currency': 'USD'}), 200
    return jsonify(data)


@app.route('/api/prices')
def prices():
    tickers = request.args.get('tickers', '')
    if not tickers:
        return jsonify({'error': 'tickers 파라미터가 필요합니다'}), 400
    ticker_list = [t.strip() for t in tickers.split(',') if t.strip()]
    result = {}
    for ticker in ticker_list:
        data = get_price_data(ticker)
        result[ticker] = data if data else {
            'ok': False, 'price': 0, 'change': 0, 'changePct': 0,
            'name': ticker, 'currency': 'USD'
        }
    return jsonify(result)


@app.route('/api/history/<path:ticker>')
def history(ticker):
    range_param = request.args.get('range', '1mo')
    if range_param not in {'5d', '1mo', '3mo', 'ytd', '1y'}:
        return jsonify({'error': 'invalid range'}), 400
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=range_param)
        if hist.empty:
            return jsonify({'startPrice': None})
        start_price = float(hist['Close'].dropna().iloc[0])
        return jsonify({'startPrice': round(start_price, 6)})
    except Exception:
        return jsonify({'startPrice': None})


@app.route('/api/news/<path:ticker>')
def news(ticker):
    try:
        t = yf.Ticker(ticker)
        raw = t.news
        if not raw:
            return jsonify([])
        articles = []
        for item in raw[:10]:
            content = item.get('content', {})
            title = content.get('title') or item.get('title', '')
            url = (content.get('canonicalUrl', {}) or {}).get('url') or \
                  (content.get('clickThroughUrl', {}) or {}).get('url') or \
                  item.get('link', '')
            provider = (content.get('provider', {}) or {}).get('displayName') or \
                       item.get('publisher', '')
            pub_time = content.get('pubDate') or item.get('providerPublishTime')
            if pub_time:
                import datetime
                try:
                    if isinstance(pub_time, (int, float)):
                        dt = datetime.datetime.fromtimestamp(pub_time)
                    else:
                        dt = datetime.datetime.fromisoformat(pub_time.replace('Z', '+00:00'))
                    diff = datetime.datetime.now(datetime.timezone.utc) - dt.astimezone(datetime.timezone.utc)
                    hours = int(diff.total_seconds() // 3600)
                    time_str = f'{hours}시간 전' if hours < 24 else f'{hours // 24}일 전'
                except Exception:
                    time_str = ''
            else:
                time_str = ''
            if title:
                articles.append({
                    'title': title,
                    'titleOrig': title,
                    'url': url,
                    'source': provider,
                    'time': time_str,
                    'sentiment': 'neu',
                })
        return jsonify(articles)
    except Exception as e:
        return jsonify([])


@app.route('/api/portfolio/metrics', methods=['POST'])
def portfolio_metrics():
    payload = request.get_json(silent=True) or {}
    tickers = payload.get('tickers', [])
    weights = payload.get('weights')
    period = payload.get('period', '1y')
    benchmark = payload.get('benchmark', '^GSPC')
    risk_free_rate = payload.get('riskFreeRate', 0.03)

    result, status = calculate_portfolio_metrics(
        tickers=tickers,
        weights=weights,
        period=period,
        benchmark=benchmark,
        risk_free_rate=risk_free_rate
    )
    return jsonify(result), status


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

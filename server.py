from flask import Flask, jsonify
from flask_cors import CORS
import yfinance as yf

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


@app.route('/api/price/<path:ticker>')
def price(ticker):
    data = get_price_data(ticker)
    if data is None:
        return jsonify({'ok': False, 'price': 0, 'change': 0, 'changePct': 0,
                        'name': ticker, 'currency': 'USD'}), 200
    return jsonify(data)


@app.route('/api/prices')
def prices():
    from flask import request
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


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

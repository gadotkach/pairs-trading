"""Загрузка цен: Tinkoff API (основной) + MOEX ISS (fallback)."""
import os
import requests
import pandas as pd
from datetime import datetime, timedelta
import time

TICKERS = ['GAZP', 'LKOH', 'SBER', 'SNGS', 'GMKN', 'NVTK', 'ROSN', 'TATN',
           'PLZL', 'MOEX', 'VTBR', 'SBERP', 'TATNP']
CACHE_DIR = 'cache'

FIGI_MAP = {
    'GAZP':  'BBG004730RP0',
    'LKOH':  'BBG004731032',
    'SBER':  'BBG004730N88',
    'SNGS':  'BBG0047315D0',
    'GMKN':  'BBG004731489',
    'NVTK':  'BBG00475KKY8',
    'ROSN':  'BBG004731354',
    'TATN':  'BBG004RVFFC0',
    'PLZL':  'BBG000R607Y3',
    'MOEX':  'BBG004730JJ5',
    'VTBR':  'BBG004730ZJ9',
    'SBERP': 'BBG0047315Y7',
    'TATNP': 'BBG004S68829',
}


def get_last_date(ticker):
    path = os.path.join(CACHE_DIR, "{}.csv".format(ticker))
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        return df.index.max().date() if len(df) else None
    except Exception:
        return None


def load_existing(ticker):
    path = os.path.join(CACHE_DIR, "{}.csv".format(ticker))
    if os.path.exists(path):
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        # Нормализация: если есть колонка тикера — объединить с close
        if ticker in df.columns and 'close' in df.columns:
            combined = df[ticker].combine_first(df['close'])
            return combined.to_frame(name='close')
        elif 'close' in df.columns:
            return df[['close']].copy()
        else:
            df2 = df.iloc[:, :1].copy()
            df2.columns = ['close']
            return df2
    return pd.DataFrame(columns=['close'])


def save_cache(ticker, df):
    path = os.path.join(CACHE_DIR, "{}.csv".format(ticker))
    df = df[['close']].copy()
    df.to_csv(path, index_label='date')


def fetch_tinkoff(ticker, from_date):
    token = os.environ.get("INVEST_TOKEN", "")
    if not token:
        raise RuntimeError("INVEST_TOKEN не задан")

    from t_tech.invest import Client, CandleInterval
    from datetime import timezone

    figi = FIGI_MAP.get(ticker)
    if not figi:
        raise RuntimeError("FIGI для {} не найден".format(ticker))

    from_dt = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    to_dt = datetime.now(timezone.utc)

    rows = []
    with Client(token) as client:
        candles = client.market_data.get_candles(
            figi=figi,
            from_=from_dt,
            to=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_DAY,
        )
        for c in candles.candles:
            close = c.close.units + c.close.nano / 1e9
            rows.append({
                'date': c.time.date(),
                'close': close,
            })

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    return df


def fetch_moex(ticker, start_date):
    url = ("https://iss.moex.com/iss/history/engines/stock/markets/shares/"
           "boards/TQBR/securities/{}.json".format(ticker))
    params = {
        'from': start_date.strftime('%Y-%m-%d'),
        'iss.meta': 'off',
        'iss.only': 'history',
        'history.columns': 'TRADEDATE,LEGALCLOSEPRICE,CLOSE',
        'limit': 100,
    }

    all_data = []
    start = 0
    while True:
        params['start'] = start
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print("    MOEX ошибка {}: {}".format(ticker, e))
            break

        rows = data.get('history', {}).get('data', [])
        if not rows:
            break
        all_data.extend(rows)
        start += len(rows)
        if len(rows) < params['limit']:
            break
        time.sleep(0.3)

    if not all_data:
        return None

    df = pd.DataFrame(all_data, columns=['date', 'legal_close', 'close'])
    df['close'] = df['legal_close'].combine_first(df['close'])
    df['date'] = pd.to_datetime(df['date'])
    df = df[['date', 'close']].set_index('date')
    return df


def update_cache(ticker):
    print("  {}:".format(ticker), end=' ')

    today = datetime.now().date()
    last_date = get_last_date(ticker)
    if last_date is None:
        from_date = (datetime.now() - timedelta(days=5 * 365)).date()
        print("кэш пуст, с {}".format(from_date), end=' -> ')
    else:
        from_date = last_date + timedelta(days=1)
        # Если from_date в будущем — данных ещё нет
        if from_date > today:
            print("последняя {} (данных нет)".format(last_date))
            return
        print("последняя {}, с {}".format(last_date, from_date), end=' -> ')

    new_df = None
    source = None

    try:
        new_df = fetch_tinkoff(ticker, from_date)
        source = 'Tinkoff'
    except Exception as e:
        print("Tinkoff ошибка: {}".format(str(e)[:60]), end=' -> ')

    if new_df is None or new_df.empty:
        try:
            new_df = fetch_moex(ticker, from_date)
            source = 'MOEX'
        except Exception as e:
            print("MOEX ошибка: {}".format(str(e)[:60]), end=' -> ')

    if new_df is None or new_df.empty:
        print("новых данных нет")
        return

    existing = load_existing(ticker)
    combined = pd.concat([existing, new_df])
    combined = combined[~combined.index.duplicated(keep='last')]
    combined.sort_index(inplace=True)
    save_cache(ticker, combined)

    print("{}: +{} строк, всего {}".format(source, len(new_df), len(combined)))


if __name__ == "__main__":
    print("Начало обновления кэша: {}".format(datetime.now()))
    for t in TICKERS:
        update_cache(t)
    print("Обновление кэша завершено.")

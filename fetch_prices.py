import os
import requests
import pandas as pd
from datetime import datetime, timedelta
import time

TICKERS = ['GAZP', 'LKOH', 'SBER', 'SNGS', 'GMKN', 'NVTK', 'ROSN', 'TATN', 'PLZL', 'MOEX', 'VTBR', 'SBERP', 'TATNP']
CACHE_DIR = 'cache'

def fetch_moex_history(ticker, start_date):
    """Загружает историю с MOEX ISS API."""
    url = "https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities/{}.json".format(ticker)
    params = {
        'from': start_date,
        'iss.meta': 'off',
        'iss.only': 'history',
        'history.columns': 'TRADEDATE,CLOSE',
        'limit': 100
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
            print(f"  Ошибка запроса для {ticker}: {e}")
            break
            
        rows = data.get('history', {}).get('data', [])
        if not rows:
            break
            
        all_data.extend(rows)
        start += len(rows)
        if len(rows) < params['limit']:
            break
        time.sleep(0.5)  # Пауза, чтобы не превышать лимиты API

    if not all_data:
        return None
        
    df = pd.DataFrame(all_data, columns=['date', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    return df

def update_cache(ticker):
    cache_file = os.path.join(CACHE_DIR, f"{ticker}.csv")
    
    # Определяем, с какой даты начинать загрузку
    if os.path.exists(cache_file):
        existing_df = pd.read_csv(cache_file, parse_dates=['date'], index_col='date')
        last_date = existing_df.index.max()
        start_date = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
        print(f"  {ticker}: последняя дата в кэше {last_date.date()}, загружаем с {start_date}")
    else:
        start_date = (datetime.now() - timedelta(days=5*365)).strftime('%Y-%m-%d')
        print(f"  {ticker}: кэш пуст, загружаем с {start_date}")
        existing_df = pd.DataFrame()

    # Загружаем новые данные
    new_df = fetch_moex_history(ticker, start_date)
    
    if new_df is not None and not new_df.empty:
        # Объединяем и убираем дубликаты
        combined = pd.concat([existing_df, new_df])
        combined = combined[~combined.index.duplicated(keep='last')]
        combined.sort_index(inplace=True)
        
        # Сохраняем
        combined.to_csv(cache_file)
        print(f"  {ticker}: добавлено {len(new_df)} строк, всего {len(combined)}")
    else:
        print(f"  {ticker}: новых данных нет")

if __name__ == "__main__":
    print(f"Начало обновления кэша: {datetime.now()}")
    for ticker in TICKERS:
        update_cache(ticker)
    print("Обновление кэша завершено.")

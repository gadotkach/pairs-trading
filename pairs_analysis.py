# -*- coding: utf-8 -*-
"""
Pair trading: Стратегия 1 на парах голубых фишек MOEXBC.
Python 3.8.2, macOS Catalina.

ЛОГИКА СУФФИКСОВ:
- Тикер БЕЗ суффикса = ПОСЛЕ последнего сплита (текущий).
- Тикер С суффиксом _ГОД = ДО сплита этого года.

ОБЪЕДИНЕНИЕ ПАР СО СПЛИТОМ:
- Для каждого периода независимо для КАЖДОГО тикера решаем:
  "до" или "после" — по числу дней в окне.
- При равенстве — берём "до".
- Объединённая пара называется БЕЗ суффиксов.

ФИЛЬТР ТОПА (A2):
- Средняя доходность теории по 4 периодам > 10%.

ФИЛЬТР ПО ПОРЯДКУ ЦЕН:
- На текущую дату считаем "порядок" цены каждого тикера.
- Если порядок X ≠ порядок Y — пара исключается из ВСЕХ периодов.

ФИЛЬТР ПО СДЕЛКАМ (только для основных таблиц):
- Если у пары <= 1 сделки в периоде — она не попадает в {период}_ТОП.
- Применяется к каждой таблице отдельно.
- На Теорию, Проверку_фильтра и Telegram не влияет.

СИМУЛЯЦИЯ (Стратегия 1):
- Старт: 100 000 ₽ ТОЛЬКО в X.
- Переливы X↔Y по ±5%.
- Фильтр 3% по цене бумаги.
- Сигнал без актива → «Отменено» +1.

СТАРТ / ФИНАЛ В ТАБЛИЦАХ:
- «Старт X, акций» — количество X при старте.
- «Старт Y, акций» — количество Y при ПЕРВОМ переливе X→Y.
- «Финал X/Y, акций» — количество в конце.
- Округление до 4 знаков.

ТИКЕРЫ (13):
- 11 обычных + SBERP, TATNP.
"""

import os
import json
import time
import pandas as pd
import requests
from datetime import datetime, timedelta

# ================= НАСТРОЙКИ =================
TICKERS = ['GAZP', 'LKOH', 'SBER', 'SNGS', 'GMKN', 'NVTK',
           'ROSN', 'TATN', 'PLZL', 'MOEX', 'VTBR',
           'SBERP', 'TATNP']

STEP = 0.05
INITIAL_CAPITAL = 100_000
PRICE_FILTER = 1.03

KNOWN_SPLITS = {
    'GMKN': ['2024-04-08'],
    'PLZL': ['2025-03-27'],
    'VTBR': ['2024-07-15'],
}
USE_SPLIT_HANDLING = True

PERIOD_DAYS = {
    "10 лет": 2500,
    "5 лет":  1825,
    "3 года": 1200,
    "1 год":  365,
}

THEORY_THRESHOLD = 10.0
TOP_N = 30
MIN_TRADES = 2               # <= 1 сделки → не попадает в {период}_ТОП

OUTPUT_FILE = 'pair_strategies_analysis.xlsx'
STATE_FILE = 'state.json'

TAX_RATE = 0.13
COMMISSION_RATE = 0.003

USE_CACHE = True
CACHE_DIR = 'cache'
CACHE_FRESH_DAYS = 1

# ---------- TELEGRAM ----------
import os
SEND_TELEGRAM = os.environ.get("SEND_TELEGRAM", "True") == "True"
TG_PROXY = os.environ.get("TG_PROXY", "https://tg-proxy.shvaboe.workers.dev")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
# =============================================

today = datetime.today()
end_date = today.strftime('%Y-%m-%d')
start_date_10y = (today - timedelta(days=365 * 10)).strftime('%Y-%m-%d')

os.makedirs(CACHE_DIR, exist_ok=True)


# ---------- ПОРЯДОК ЦЕНЫ ----------
def price_order(p):
    if p >= 1:
        return len(str(int(p)))
    else:
        s = f"{p:.10f}"
        frac = s.split('.')[1] if '.' in s else ''
        zeros = 0
        for c in frac:
            if c == '0':
                zeros += 1
            else:
                break
        return f"0.{zeros}"


# ---------- STATE.JSON ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {'excluded_notified': [], 'last_update': None}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if 'excluded_notified' not in data:
            data['excluded_notified'] = []
        return data
    except Exception as e:
        print(f"[state] ошибка чтения: {e}")
        return {'excluded_notified': [], 'last_update': None}


def save_state(state):
    state['last_update'] = datetime.now().isoformat()
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- ЗАГРУЗКА С MOEX ----------
def fetch_moex_history(ticker, start, end):
    base_url = (f"https://iss.moex.com/iss/history/engines/stock/markets/shares/"
                f"boards/TQBR/securities/{ticker}.json?"
                f"from={start}&till={end}")
    all_rows, columns, start_offset, page_size = [], None, 0, 100

    while True:
        url = f"{base_url}&start={start_offset}"
        data = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=30)
                if resp.status_code != 200:
                    time.sleep(2 ** attempt)
                    continue
                data = resp.json()
                break
            except requests.exceptions.JSONDecodeError:
                time.sleep(2 ** attempt)
            except Exception as e:
                print(f"  ! {ticker}: {type(e).__name__} — {e}")
                time.sleep(2 ** attempt)

        if data is None:
            return None

        h = data.get('history', {})
        if columns is None:
            columns = h.get('columns', [])
        rows = h.get('data', [])
        if not rows:
            break

        all_rows.extend(rows)
        start_offset += page_size
        time.sleep(0.3)
        if start_offset > 50000:
            break

    if not all_rows or columns is None:
        return None

    df = pd.DataFrame(all_rows, columns=columns)
    if 'TRADEDATE' not in df.columns or 'CLOSE' not in df.columns:
        return None

    df = df[['TRADEDATE', 'CLOSE']].rename(
        columns={'TRADEDATE': 'date', 'CLOSE': ticker})
    df['date'] = pd.to_datetime(df['date'])
    df = df[df[ticker].notna() & (df[ticker] > 0)]
    df = df.drop_duplicates(subset='date', keep='last')
    return df.set_index('date')


def cache_path(ticker):
    return os.path.join(CACHE_DIR, f"{ticker}.csv")


def load_from_cache(ticker):
    path = cache_path(ticker)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        if ticker not in df.columns or df.empty:
            return None
        return df
    except Exception:
        return None


def save_to_cache(ticker, df):
    df.to_csv(cache_path(ticker))


def get_history(ticker, start, end):
    df = load_from_cache(ticker) if USE_CACHE else None
    if df is not None:
        last_date = df.index.max()
        days_behind = (datetime.today() - last_date).days
        if days_behind <= CACHE_FRESH_DAYS:
            print(f"✓ {ticker}: {len(df)} дней (кэш)")
            return df
        fetch_from = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
        new_df = fetch_moex_history(ticker, fetch_from, end)
        if new_df is not None and not new_df.empty:
            combined = pd.concat([df, new_df])
            combined = combined[~combined.index.duplicated(keep='last')].sort_index()
            save_to_cache(ticker, combined)
            print(f"✓ {ticker}: {len(combined)} дней (кэш + догрузка)")
            return combined
        print(f"✓ {ticker}: {len(df)} дней (кэш, догрузка не удалась)")
        return df

    print(f"⬇ {ticker}: качаем {start} → {end}...")
    df = fetch_moex_history(ticker, start, end)
    if df is not None and not df.empty:
        save_to_cache(ticker, df)
        return df
    return None


# ---------- АВТОДЕТЕКТ СПЛИТА ----------
def _is_round_ratio(ratio):
    for r in (2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 25, 50, 100, 1000, 5000):
        if abs(ratio / r - 1) < 0.05:
            return True
    return False


def detect_split_date(ticker, price_series, approx_year):
    window = price_series[
        (price_series.index.year >= approx_year - 1) &
        (price_series.index.year <= approx_year + 1)
    ]
    if len(window) < 3:
        return None
    pct = window.pct_change().dropna()
    for date, chg in pct.items():
        if abs(chg) < 0.50:
            continue
        ratio = (1 / (1 + chg)) if chg < 0 else (1 + chg)
        if _is_round_ratio(ratio):
            return date
    return None


# ---------- РАЗБИЕНИЕ НА ПОД-ПЕРИОДЫ ----------
def split_window_by_ticker(ticker, window, splits_dict):
    if not USE_SPLIT_HANDLING or ticker not in splits_dict:
        return [(ticker, window)]

    split_dates = []
    for split_date_str in splits_dict[ticker]:
        split_date = pd.Timestamp(split_date_str)
        if split_date in window.index:
            split_dates.append(split_date)
        else:
            detected = detect_split_date(ticker, window, split_date.year)
            if detected is not None:
                split_dates.append(detected)

    if not split_dates:
        return [(ticker, window)]

    split_dates = sorted(set(split_dates))
    sub_windows = []
    current_start = window.index[0]

    for split_date in split_dates:
        mask_before = (window.index >= current_start) & (window.index < split_date)
        sub = window.loc[mask_before]
        if len(sub) >= 2:
            year = split_date.year
            name = f"{ticker}_{year}"
            sub_windows.append((name, sub))
        current_start = split_date

    mask_after = window.index >= current_start
    sub = window.loc[mask_after]
    if len(sub) >= 2:
        sub_windows.append((ticker, sub))

    return sub_windows


def build_all_subseries(window, splits_dict):
    result = {}
    for t in window.columns:
        result[t] = split_window_by_ticker(t, window[t], splits_dict)
    return result


def select_subpair_for_period(subseries_a, subseries_b, period_window):
    def choose_for_ticker(subs):
        best = None
        best_days = -1
        for name, series in subs:
            days = len(series)
            if days > best_days:
                best_days = days
                best = (name, series)
            elif days == best_days:
                is_suffix = '_' in name and name.split('_')[-1].isdigit()
                best_is_suffix = '_' in best[0] and best[0].split('_')[-1].isdigit()
                if is_suffix and not best_is_suffix:
                    best = (name, series)
        return best

    a_choice = choose_for_ticker(subseries_a)
    b_choice = choose_for_ticker(subseries_b)

    if a_choice is None or b_choice is None:
        return None

    name_a, series_a = a_choice
    name_b, series_b = b_choice

    common = series_a.index.intersection(series_b.index)
    if len(common) >= 2:
        return (name_a, series_a.loc[common], name_b, series_b.loc[common])

    best = None
    best_days = -1
    for na, sa in subseries_a:
        for nb, sb in subseries_b:
            common = sa.index.intersection(sb.index)
            if len(common) >= 2:
                days = len(common)
                if days > best_days:
                    best_days = days
                    best = (na, sa.loc[common], nb, sb.loc[common])
    return best


# ---------- СИМУЛЯЦИЯ ----------
def simulate(ratio, prices_x, prices_y, ticker_x, ticker_y,
             initial=INITIAL_CAPITAL, step=STEP):
    if len(ratio) < 2:
        return None

    start_px = float(prices_x.iloc[0])
    start_py = float(prices_y.iloc[0])

    shares_x = initial / start_px
    shares_y = 0.0
    start_shares_x = shares_x
    start_shares_y = 0.0

    first_buy_shares_y = None

    cost_x = initial
    cost_y = 0.0

    x_last_buy = start_px
    y_last_buy = None

    level = float(ratio.iloc[0])
    total_invested = initial
    total_tax = 0.0
    total_commission = 0.0
    trades_count = 0
    cancelled_count = 0
    signal_up_count = 0
    signal_down_count = 0

    last_buy_price_holding = start_px

    for i in range(1, len(ratio)):
        date = ratio.index[i]
        val = float(ratio.iloc[i])
        px = float(prices_x.loc[date])
        py = float(prices_y.loc[date])

        if val >= level * (1 + step):
            signal_up_count += 1
            if shares_x > 0:
                if px >= x_last_buy * PRICE_FILTER:
                    sell_amount = shares_x * px
                    commission_sell = sell_amount * COMMISSION_RATE
                    net_sell = sell_amount - commission_sell
                    total_commission += commission_sell
                    profit = sell_amount - cost_x
                    if profit > 0:
                        tax = profit * TAX_RATE
                        total_tax += tax
                        net_sell -= tax
                    bought_y = net_sell / py
                    shares_y += bought_y
                    cost_y += net_sell
                    trades_count += 1
                    shares_x = 0
                    cost_x = 0
                    x_last_buy = px
                    y_last_buy = py
                    last_buy_price_holding = py
                    if first_buy_shares_y is None:
                        first_buy_shares_y = bought_y
                else:
                    cancelled_count += 1
            else:
                cancelled_count += 1
            level = val

        elif val <= level * (1 - step):
            signal_down_count += 1
            if shares_y > 0:
                if py >= y_last_buy * PRICE_FILTER:
                    sell_amount = shares_y * py
                    commission_sell = sell_amount * COMMISSION_RATE
                    net_sell = sell_amount - commission_sell
                    total_commission += commission_sell
                    profit = sell_amount - cost_y
                    if profit > 0:
                        tax = profit * TAX_RATE
                        total_tax += tax
                        net_sell -= tax
                    shares_x += net_sell / px
                    cost_x += net_sell
                    trades_count += 1
                    shares_y = 0
                    cost_y = 0
                    x_last_buy = px
                    y_last_buy = py
                    last_buy_price_holding = px
                else:
                    cancelled_count += 1
            else:
                cancelled_count += 1
            level = val

    final_px = float(prices_x.iloc[-1])
    final_py = float(prices_y.iloc[-1])

    if shares_x > 0:
        holding_ticker = ticker_x
        shares_holding = shares_x
        price_start_holding = start_px
        price_end_holding = final_px
    elif shares_y > 0:
        holding_ticker = ticker_y
        shares_holding = shares_y
        price_start_holding = start_py
        price_end_holding = final_py
    else:
        holding_ticker = None
        shares_holding = 0.0
        price_start_holding = None
        price_end_holding = None

    final_value = shares_x * final_px + shares_y * final_py
    final_after_tax = final_value - total_tax - total_commission
    return_pct = ((final_after_tax - total_invested) / total_invested * 100) if total_invested > 0 else 0

    if holding_ticker is not None:
        final_value_theory = shares_holding * last_buy_price_holding
        final_position = f"{shares_holding:.4f} {holding_ticker}"
    else:
        final_value_theory = 0.0
        final_position = "нет акций"

    theory_after_tax = final_value_theory - total_tax - total_commission
    return_theory_pct = ((theory_after_tax - total_invested) / total_invested * 100) if total_invested > 0 else 0

    return {
        'внесено': total_invested,
        'заработано_до': final_value,
        'налог': total_tax,
        'комиссия': total_commission,
        'заработано_после': final_after_tax,
        'доходность_после': round(return_pct, 2),
        'сделок': trades_count,
        'отменено': cancelled_count,
        'signal_up': signal_up_count,
        'signal_down': signal_down_count,
        'start_shares_x': start_shares_x,
        'start_shares_y': first_buy_shares_y if first_buy_shares_y is not None else 0.0,
        'end_shares_x': shares_x,
        'end_shares_y': shares_y,
        'final_position': final_position,
        'holding_ticker': holding_ticker,
        'last_buy_price_holding': last_buy_price_holding,
        'price_start_holding': price_start_holding,
        'price_end_holding': price_end_holding,
        'shares_holding': shares_holding,
        'доходность_теория': round(return_theory_pct, 2),
        'деньги_теория': round(theory_after_tax, 2),
        'last_level': level,
        'last_ratio': float(ratio.iloc[-1]),
    }


# ---------- ЗАГРУЗКА ----------
print("=" * 60)
print("ЗАГРУЗКА ДАННЫХ")
print("=" * 60)

prices = pd.DataFrame()
for t in TICKERS:
    df = get_history(t, start_date_10y, end_date)
    if df is not None:
        prices = prices.join(df, how='outer') if not prices.empty else df

if prices.empty:
    raise SystemExit("Нет данных.")

prices = prices.dropna()
print(f"\nВсего общих дней: {len(prices)}")
print(f"Период: {prices.index.min().date()} → {prices.index.max().date()}\n")


# ---------- ФИЛЬТР ПО ПОРЯДКУ ЦЕН ----------
last_prices = prices.iloc[-1]
current_orders = {t: price_order(last_prices[t]) for t in prices.columns}

print("=" * 60)
print("ПОРЯДОК ЦЕН НА ТЕКУЩУЮ ДАТУ")
print("=" * 60)
for t in prices.columns:
    print(f"  {t}: {last_prices[t]:.4f} → порядок {current_orders[t]}")

excluded_by_order = set()
tickers_list = list(prices.columns)
for i in range(len(tickers_list)):
    for j in range(i + 1, len(tickers_list)):
        a, b = tickers_list[i], tickers_list[j]
        if current_orders[a] != current_orders[b]:
            excluded_by_order.add(f"{a}/{b}")

print(f"\nПар исключено по порядку цен: {len(excluded_by_order)}")
for p in sorted(excluded_by_order):
    print(f"  {p}")


# ---------- STATE.JSON ----------
state = load_state()
previously_notified = set(state.get('excluded_notified', []))

newly_excluded = excluded_by_order - previously_notified
returned = previously_notified - excluded_by_order

print(f"\nНовых исключений: {len(newly_excluded)}")
for p in sorted(newly_excluded):
    print(f"  {p}")
print(f"Вернувшихся: {len(returned)}")
for p in sorted(returned):
    print(f"  {p}")


# ---------- ПРОГОН ----------
period_results = {}
theory_rows = []

for period_name, n_days in PERIOD_DAYS.items():
    cutoff = today - timedelta(days=n_days)
    window = prices[prices.index >= cutoff]

    if len(window) < 50:
        print(f"\n⚠ Период {period_name}: мало данных, пропускаем")
        continue

    print(f"\n{'=' * 60}")
    print(f"ПЕРИОД: {period_name} ({window.index.min().date()} → {window.index.max().date()}, {len(window)} дней)")
    print("=" * 60)

    subseries = build_all_subseries(window, KNOWN_SPLITS)
    tickers = list(window.columns)

    period_results[period_name] = {}

    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            ta, tb = tickers[i], tickers[j]
            base_pair = f"{ta}/{tb}"

            if base_pair in excluded_by_order:
                continue

            selected = select_subpair_for_period(
                subseries[ta], subseries[tb], window
            )
            if selected is None:
                continue

            name_a, series_a, name_b, series_b = selected
            ratio = series_a / series_b
            res = simulate(ratio, series_a, series_b, name_a, name_b)
            if res is None:
                continue

            period_results[period_name][base_pair] = {
                'res': res,
                'series_a': series_a,
                'series_b': series_b,
                'name_a': name_a,
                'name_b': name_b,
            }

            ps = res['price_start_holding']
            pe = res['price_end_holding']
            if ps is not None and pe is not None and ps > 0:
                diverg = round((pe - ps) / ps * 100, 2)
            else:
                diverg = None

            theory_rows.append({
                'Период': period_name,
                'Пара': base_pair,
                'Под-пара': f"{name_a}/{name_b}",
                'Цена акции начало': round(ps, 2) if ps is not None else None,
                'Цена акции конец': round(pe, 2) if pe is not None else None,
                'Теория': round(ps, 2) if ps is not None else None,
                'Доходность (теория), %': res['доходность_теория'],
                'Расхождение %': diverg,
                'Доходность (как в прошлых), %': res['доходность_после'],
            })

theory_df = pd.DataFrame(theory_rows)


# ---------- СВОДНЫЙ ТОП (A2) ----------
all_pairs = set()
for period_name in period_results:
    all_pairs.update(period_results[period_name].keys())

top_rows = []
for pair_name in sorted(all_pairs):
    row = {'Пара': pair_name}
    period_vals = {}
    for period_name in PERIOD_DAYS:
        if period_name in period_results and pair_name in period_results[period_name]:
            val = period_results[period_name][pair_name]['res']['доходность_теория']
            row[period_name] = val
            period_vals[period_name] = val
        else:
            row[period_name] = None
            period_vals[period_name] = None

    vals = [v for v in period_vals.values() if v is not None]
    avg = round(sum(vals) / len(vals), 2) if vals else None
    avg_ok = (avg is not None and avg > THEORY_THRESHOLD)

    row['Средняя'] = avg
    row['Прошла фильтр'] = 'ДА' if avg_ok else 'НЕТ'
    row['Причина'] = 'прошла' if avg_ok else (f'средняя = {avg}' if avg is not None else 'нет данных')
    top_rows.append(row)

top_df = pd.DataFrame(top_rows)
if not top_df.empty:
    top_df = top_df.sort_values('Средняя', ascending=False).reset_index(drop=True)

passed_pairs = list(top_df[top_df['Прошла фильтр'] == 'ДА']['Пара']) if not top_df.empty else []

if len(passed_pairs) > TOP_N:
    passed_pairs = passed_pairs[:TOP_N]

print(f"\n{'=' * 60}")
print(f"ПАР, ПРОШЕДШИХ ФИЛЬТР (средняя > {THEORY_THRESHOLD}%): {len(passed_pairs)}")
print("=" * 60)
for p in passed_pairs:
    print(f"  {p}")


# ---------- ОСНОВНЫЕ ТАБЛИЦЫ ----------
main_results = []
skipped_by_trades = []

for period_name in PERIOD_DAYS:
    if period_name not in period_results:
        continue
    for pair_name in passed_pairs:
        if pair_name not in period_results[period_name]:
            continue
        info = period_results[period_name][pair_name]
        res = info['res']

        # НОВОЕ: исключаем пары с <= 1 сделкой в этом периоде
        if res['сделок'] < MIN_TRADES:
            skipped_by_trades.append({
                'Период': period_name,
                'Пара': pair_name,
                'Сделок': res['сделок'],
            })
            continue

        series_a = info['series_a']
        series_b = info['series_b']
        name_a = info['name_a']
        name_b = info['name_b']

        holding = res['holding_ticker']
        if holding == name_a:
            price_start = round(float(series_a.iloc[0]), 2)
            price_end = round(float(series_a.iloc[-1]), 2)
        elif holding == name_b:
            price_start = round(float(series_b.iloc[0]), 2)
            price_end = round(float(series_b.iloc[-1]), 2)
        else:
            price_start = None
            price_end = None

        main_results.append({
            'Период': period_name,
            'Пара': pair_name,
            'Под-пара': f"{name_a}/{name_b}",
            'Сделок': res['сделок'],
            'Отменено': res['отменено'],
            'Старт X, акций': round(res['start_shares_x'], 4),
            'Финал X, акций': round(res['end_shares_x'], 4),
            'Старт Y, акций': round(res['start_shares_y'], 4),
            'Финал Y, акций': round(res['end_shares_y'], 4),
            'Внесено, ₽': round(res['внесено'], 2),
            'Деньги в конце, ₽': round(res['заработано_до'], 2),
            'Налог, ₽': round(res['налог'], 2),
            'Комиссия, ₽': round(res['комиссия'], 2),
            'Заработано (после), ₽': round(res['заработано_после'], 2),
            'Доходность (после), %': res['доходность_после'],
            'Где деньги в конце': res['final_position'],
            'Цена акции начало': price_start,
            'Цена акции конец': price_end,
        })

main_df = pd.DataFrame(main_results)

if skipped_by_trades:
    print(f"\n{'=' * 60}")
    print(f"ПРОПУЩЕНО ИЗ ОСНОВНЫХ ТАБЛИЦ (Сделок < {MIN_TRADES}):")
    print("=" * 60)
    for s in skipped_by_trades:
        print(f"  {s['Период']}: {s['Пара']} — {s['Сделок']} сделок")


# ---------- EXCEL ----------
with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as w:
    if not top_df.empty:
        top_df.to_excel(w, sheet_name='Проверка_фильтра', index=False)

    if not main_df.empty:
        for period_name in PERIOD_DAYS:
            sub = main_df[main_df['Период'] == period_name]
            if sub.empty:
                continue
            sub = sub.sort_values('Доходность (после), %', ascending=False)
            sheet_name = f"{period_name}_ТОП".replace(" ", "")
            sub.to_excel(w, sheet_name=sheet_name[:31], index=False)

    if not theory_df.empty:
        for period_name in PERIOD_DAYS:
            sub = theory_df[theory_df['Период'] == period_name].copy()
            if sub.empty:
                continue
            sub = sub.sort_values('Доходность (теория), %', ascending=False)
            sheet_name = f"{period_name}_Теория".replace(" ", "")
            sub.to_excel(w, sheet_name=sheet_name[:31], index=False)

print(f"\n✓ Готово: {OUTPUT_FILE}")


# ---------- TELEGRAM ----------
def send_telegram(text):
    url = f"{TG_PROXY}/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=30)
        print(f"[telegram] status={r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"[telegram] ошибка: {e}")
        return False


if SEND_TELEGRAM:
    if newly_excluded:
        msg = "⚠️ Пары исключены по порядку цен:\n"
        for idx, p in enumerate(sorted(newly_excluded), 1):
            msg += f"{idx}. {p}\n"
        send_telegram(msg)
        print(f"[telegram] отправлено сообщение об исключениях: {len(newly_excluded)}")

    signaled = []

    if "1 год" in period_results:
        for pair_name in list(period_results["1 год"].keys()):
            if pair_name in excluded_by_order:
                continue

            info = period_results["1 год"][pair_name]
            res = info['res']
            series_a = info['series_a']
            series_b = info['series_b']

            z_now = float(series_a.iloc[-1] / series_b.iloc[-1])
            level_now = res['last_level']
            chg = (z_now - level_now) / level_now if level_now > 0 else 0.0

            if abs(chg) >= STEP:
                if chg > 0:
                    action = f"Перелить {info['name_a']} → {info['name_b']}"
                else:
                    action = f"Перелить {info['name_b']} → {info['name_a']}"

                passed_filter = pair_name in passed_pairs

                rank_1y = None
                rank_5y = None
                if not top_df.empty:
                    t1 = top_df.dropna(subset=['1 год']).sort_values('1 год', ascending=False).reset_index(drop=True)
                    m1 = t1[t1['Пара'] == pair_name]
                    if not m1.empty:
                        rank_1y = m1.index[0] + 1
                    t5 = top_df.dropna(subset=['5 лет']).sort_values('5 лет', ascending=False).reset_index(drop=True)
                    m5 = t5[t5['Пара'] == pair_name]
                    if not m5.empty:
                        rank_5y = m5.index[0] + 1

                signaled.append({
                    'pair_name': pair_name,
                    'name_a': info['name_a'],
                    'name_b': info['name_b'],
                    'z_now': z_now,
                    'level_now': level_now,
                    'chg': chg,
                    'action': action,
                    'rank_1y': rank_1y,
                    'rank_5y': rank_5y,
                    'signal_up': res['signal_up'],
                    'signal_down': res['signal_down'],
                    'passed_filter': passed_filter,
                })

    if signaled:
        for s in signaled:
            msg = "🔔 СИГНАЛ ПО ПАРЕ\n\n"
            if s['passed_filter']:
                r1 = s['rank_1y'] if s['rank_1y'] else '—'
                r5 = s['rank_5y'] if s['rank_5y'] else '—'
                msg += f"ТОП-{r1} (за год) / ТОП-{r5} (за 5 лет)\n"
            else:
                msg += "⚠️ Пара НЕ в ТОПе\n"
            msg += f"Пара: {s['name_a']}/{s['name_b']}\n\n"
            msg += f"📊 Всего сигналов: A↑={s['signal_up']}, B↑={s['signal_down']}\n"
            msg += f"📈 Сегодня: {s['name_a']} / {s['name_b']}\n"
            msg += f"💰 Было: {s['level_now']:.4f} → Стало: {s['z_now']:.4f} ({s['chg']*100:+.2f}%)\n"
            msg += f"➡️ Действие: {s['action']}"
            if not s['passed_filter']:
                msg += f"\n\n❗ Пара {s['name_a']}/{s['name_b']} исключается из ТОП"
            send_telegram(msg)
        print(f"[telegram] отправлено сигналов: {len(signaled)}")
    else:
        print("[telegram] сигналов нет — ничего не отправлено")


# ---------- STATE ----------
new_state = {
    'excluded_notified': sorted(excluded_by_order),
    'last_update': datetime.now().isoformat(),
}
save_state(new_state)
print(f"\n✓ state.json обновлён: {len(excluded_by_order)} исключённых пар")

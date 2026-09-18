# -*- coding: utf-8 -*-
"""
pairs_analysis.py — pair-trading MOEXBC.
Стратегия 3: Z1 фиксирован, целые акции, cash, довнесение остатка при сделке.

Логика:
- Z1 = p_x_start / p_y_start (фиксируется на старт окна).
- Сигнал X→Y: z >= Z1 * 1.05.
- Сигнал Y→X: z <= Z1 * 0.95.
- Фильтр 3%: цена >= x_last * 1.03 (от цены последней покупки).
- Целые акции. При сделке: продажа -> покупка Y -> остаток довносим до +1 акции.
- Обрезка окна по сплитам: сплит + 5 торговых дней.

Фильтр ТОПа:
- Средняя РЕАЛЬНАЯ доходность по 3 периодам (10/5/3) > 10%.

Telegram:
- Сигналы: ±5% от level (level обновляется).
- Если порядок цен изменился — предупреждение один раз.
- Если порядок вернулся — убираем из state.json.

state.json:
- excluded_notified: [...]
- orders: {ticker: order}
- last_update: ...
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
INITIAL_FREE = 1_000_000   # свободно на внешнем счёте
PRICE_FILTER = 1.03

KNOWN_SPLITS = {
    'GMKN': ['2024-04-08'],
    'PLZL': ['2025-03-27'],
    'VTBR': ['2024-07-15'],
}

PERIOD_DAYS = {
    "10 лет": 2500,
    "5 лет":  1825,
    "3 года": 1200,
    "1 год":  365,
}

THEORY_THRESHOLD = 10.0
TOP_N = 30
MIN_TRADES = 2

OUTPUT_FILE = 'pair_strategies_analysis.xlsx'
STATE_FILE = 'state.json'

TAX_RATE = 0.13
COMMISSION_RATE = 0.003

USE_CACHE = True
CACHE_DIR = 'cache'
CACHE_FRESH_DAYS = 1

# ---------- TELEGRAM ----------
import os as _os
SEND_TELEGRAM = _os.environ.get("SEND_TELEGRAM", "True") == "True"
TG_PROXY = _os.environ.get("TG_PROXY", "https://tg-proxy.shvaboe.workers.dev")
TG_TOKEN = _os.environ.get("TG_TOKEN", "")
TG_CHAT = _os.environ.get("TG_CHAT", "")
# =============================================

today = datetime.today()
end_date = today.strftime('%Y-%m-%d')
start_date_10y = (today - timedelta(days=365 * 10)).strftime('%Y-%m-%d')

os.makedirs(CACHE_DIR, exist_ok=True)


# ---------- УТИЛИТЫ ----------
def add_trading_days(index, start_date_str, n=5):
    """Дата start_date + n торговых дней (по индексу)."""
    start_date = pd.Timestamp(start_date_str)
    dates_after = index[index > start_date]
    if len(dates_after) < n:
        return dates_after[-1] if len(dates_after) else start_date
    return dates_after[n - 1]


def price_order(p):
    """Порядок цены: число знаков до запятой (или 0.N для <1)."""
    if p >= 1:
        return len(str(int(p)))
    else:
        s = '{:.10f}'.format(p)
        frac = s.split('.')[1] if '.' in s else ''
        zeros = 0
        for c in frac:
            if c == '0':
                zeros += 1
            else:
                break
        return '0.{}'.format(zeros)


# ---------- STATE.JSON ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            'excluded_notified': [],
            'last_update': None,
        }
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data.setdefault('excluded_notified', [])
        return data
    except Exception as e:
        print('[state] ошибка чтения: {}'.format(e))
        return {
            'excluded_notified': [],
            'last_update': None,
        }


def save_state(state):
    state['last_update'] = datetime.now().isoformat()
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- ЗАГРУЗКА С MOEX ----------
def fetch_moex_history(ticker, start, end):
    base_url = ("https://iss.moex.com/iss/history/engines/stock/markets/shares/"
                "boards/TQBR/securities/{}.json?"
                "from={}&till={}").format(ticker, start, end)
    all_rows, columns, start_offset, page_size = [], None, 0, 100

    while True:
        url = "{}&start={}".format(base_url, start_offset)
        data = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=30)
                if resp.status_code != 200:
                    time.sleep(2 ** attempt)
                    continue
                data = resp.json()
                break
            except Exception as e:
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
    return os.path.join(CACHE_DIR, "{}.csv".format(ticker))


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
            print("✓ {}: {} дней (кэш)".format(ticker, len(df)))
            return df
        fetch_from = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
        new_df = fetch_moex_history(ticker, fetch_from, end)
        if new_df is not None and not new_df.empty:
            combined = pd.concat([df, new_df])
            combined = combined[~combined.index.duplicated(keep='last')].sort_index()
            save_to_cache(ticker, combined)
            print("✓ {}: {} дней (кэш + догрузка)".format(ticker, len(combined)))
            return combined
        print("✓ {}: {} дней (кэш)".format(ticker, len(df)))
        return df

    print("⬇ {}: качаем {} → {}...".format(ticker, start, end))
    df = fetch_moex_history(ticker, start, end)
    if df is not None and not df.empty:
        save_to_cache(ticker, df)
        return df
    return None


def fifo_sell(lots, sell_price, qty_to_sell):
    """Продажа qty_to_sell по FIFO. Возвращает (tax, remaining_lots).

    lots — список dict: {'price': float, 'qty': int}
    """
    tax = 0.0
    remaining = []
    to_sell = qty_to_sell

    for lot in lots:
        if to_sell <= 0:
            remaining.append(lot)
            continue
        take = min(lot['qty'], to_sell)
        profit = (sell_price - lot['price']) * take
        if profit > 0:
            tax += profit * TAX_RATE
        to_sell -= take
        if lot['qty'] > take:
            remaining.append({'price': lot['price'], 'qty': lot['qty'] - take})

    return tax, remaining



# ---------- СИМУЛЯЦИЯ (Стратегия 6) ----------
def simulate_strategy6(ratio, px, py, tx, ty, initial=100_000, lot_x=1, lot_y=1):
    """Стратегия 6 (STEP=5%) для пары (tx, ty)."""
    if len(ratio) < 2:
        return None

    start_px = float(px.iloc[0])
    start_py = float(py.iloc[0])

    # Старт: покупаем целые лоты X
    lot_cost_x = start_px * lot_x
    lots_x_count = int(initial / lot_cost_x)
    shares_x = lots_x_count * lot_x
    cash = initial - shares_x * start_px

    shares_y = 0
    lots_x = [{'price': start_px, 'qty': shares_x}] if shares_x > 0 else []
    lots_y = []
    first_buy_shares_y = 0

    cost_x = initial
    cost_y = 0.0
    level_X = start_px / start_py
    level_Y = start_px / start_py

    invested = initial
    free_cash = INITIAL_FREE - initial
    tax = 0.0
    comm = 0.0
    trades = 0
    cancelled = 0
    topups = 0

    for i in range(1, len(ratio)):
        d = ratio.index[i]
        v = float(ratio.iloc[i])
        p_x = float(px.loc[d])
        p_y = float(py.loc[d])

        # Сигнал X→Y
        if v >= level_X * (1 + STEP):
            x_ref = lots_x[-1]['price'] if lots_x else start_px
            if shares_x > 0 and p_x >= x_ref * 1.03:
                # Продаём всё X (FIFO)
                sell = shares_x * p_x
                c = sell * COMMISSION_RATE
                comm += c
                net = sell - c
                tax_this, lots_x = fifo_sell(lots_x, p_x, shares_x)
                tax += tax_this
                net -= tax_this
                cash += net

                # Покупаем Y на весь cash
                lot_cost_y = p_y * lot_y
                lots_to_buy = int(cash / lot_cost_y)
                rem = cash - lots_to_buy * lot_cost_y

                if lots_to_buy > 0 and rem > 0:
                    # Довносим остаток → +1 лот (из free_cash)
                    free_cash -= rem
                    invested += rem
                    topups += 1
                    lots_to_buy += 1
                    cost_y = cash
                    cash = 0
                elif lots_to_buy > 0:
                    cost_y = cash
                    cash = 0
                elif rem > 0:
                    # Даже 1 лот не купить — довносим до 1 лота (из free_cash)
                    needed = lot_cost_y - cash
                    free_cash -= needed
                    invested += needed
                    topups += 1
                    lots_to_buy = 1
                    cost_y = cash + needed
                    cash = 0

                shares_y = lots_to_buy * lot_y
                if shares_y > 0:
                    lots_y.append({'price': p_y, 'qty': shares_y})
                    if first_buy_shares_y == 0:
                        first_buy_shares_y = shares_y

                shares_x = 0
                lots_x = []
                cost_x = 0
                trades += 1
                level_X = v

            else:
                cancelled += 1

        # Сигнал Y→X
        elif v <= level_Y * (1 - STEP):
            y_ref = lots_y[-1]['price'] if lots_y else start_py
            if shares_y > 0 and p_y >= y_ref * 1.03:
                sell = shares_y * p_y
                c = sell * COMMISSION_RATE
                comm += c
                net = sell - c
                tax_this, lots_y = fifo_sell(lots_y, p_y, shares_y)
                tax += tax_this
                net -= tax_this
                cash += net

                lot_cost_x = p_x * lot_x
                lots_to_buy = int(cash / lot_cost_x)
                rem = cash - lots_to_buy * lot_cost_x

                if lots_to_buy > 0 and rem > 0:
                    free_cash -= rem
                    invested += rem
                    topups += 1
                    lots_to_buy += 1
                    cost_x = cash
                    cash = 0
                elif lots_to_buy > 0:
                    cost_x = cash
                    cash = 0
                elif rem > 0:
                    needed = lot_cost_x - cash
                    free_cash -= needed
                    invested += needed
                    topups += 1
                    lots_to_buy = 1
                    cost_x = cash + needed
                    cash = 0

                shares_x = lots_to_buy * lot_x
                if shares_x > 0:
                    lots_x.append({'price': p_x, 'qty': shares_x})

                shares_y = 0
                lots_y = []
                cost_y = 0
                trades += 1
                level_Y = v

            else:
                cancelled += 1

    # Итог
    final_px = float(px.iloc[-1])
    final_py = float(py.iloc[-1])

    if shares_x > 0:
        holding = tx
        shares_h = shares_x
        price_start_h = float(lots_x[0]['price']) if lots_x else start_px
        price_end_h = final_px
    elif shares_y > 0:
        holding = ty
        shares_h = shares_y
        price_start_h = float(lots_y[0]['price']) if lots_y else start_py
        price_end_h = final_py
    else:
        holding = None
        shares_h = 0
        price_start_h = None
        price_end_h = None

    final_value = shares_x * final_px + shares_y * final_py + cash
    # налог/комиссия УЖЕ вычтены из cash при сделках
    final_after = final_value - invested
    return_pct = (final_after / invested * 100) if invested else 0

    # ТЕОРИЯ (последняя цена покупки)
    if holding is not None and shares_h > 0:
        last_price = lots_x[-1]['price'] if (holding == tx and lots_x) else \
                     (lots_y[-1]['price'] if lots_y else None)
        if last_price:
            theory_value = shares_h * last_price
            theory_after = theory_value - tax - comm
            theory_pct = ((theory_after - invested) / invested * 100) if invested else 0
        else:
            theory_pct = 0
    else:
        theory_pct = 0

    if holding:
        final_pos = '{} {} + cash {:.2f}'.format(int(shares_h), holding, cash)
    else:
        final_pos = 'cash {:.2f}'.format(cash)

    return {
        'внесено': invested,
        'заработано_до': final_value,
        'налог': tax,
        'комиссия': comm,
        'заработано_после': final_after,
        'доходность_после': round(return_pct, 2),
        'доходность_теория': round(theory_pct, 2),
        'сделок': trades,
        'отменено': cancelled,
        'довнесений': topups,
        'start_shares_x': lots_x_count * lot_x if 'lots_x_count' in dir() else shares_x,
        'end_shares_x': shares_x,
        'start_shares_y': first_buy_shares_y,
        'end_shares_y': shares_y,
        'final_position': final_pos,
        'holding_ticker': holding,
        'price_start_holding': price_start_h,
        'price_end_holding': price_end_h,
        'level_X': round(level_X, 6),
        'level_Y': round(level_Y, 6),
    }




def load_extrema_threshold(az_file='analytics_z.xlsx', threshold=30):
    """Возвращает set пар, где Экстремумы >= threshold.

    Логика: 5 лет -> если нет, 3 года.
    """
    import pandas as _pd
    try:
        az = _pd.read_excel(az_file, sheet_name='Analytics_Z')
    except Exception as e:
        print("[extrema_filter] ошибка: {}".format(e))
        return set()

    pairs_5y = {}
    pairs_3y = {}

    for _, row in az.iterrows():
        period = row.get('Период')
        pair = row.get('Пара')
        ext = row.get('Экстремумы')
        if pair is None or ext is None:
            continue
        if _pd.isna(ext):
            continue
        if period == '5лет':
            pairs_5y[pair] = ext
        elif period == '3года':
            pairs_3y[pair] = ext

    passed = set()
    all_pairs = set(list(pairs_5y.keys()) + list(pairs_3y.keys()))
    for pair in all_pairs:
        ext = pairs_5y.get(pair)
        if ext is not None:
            if ext >= threshold:
                passed.add(pair)
        else:
            ext3 = pairs_3y.get(pair)
            if ext3 is not None and ext3 >= threshold:
                passed.add(pair)

    print("[extrema_filter] пар с Экстремумы >= {}: {}".format(threshold, len(passed)))
    return passed


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
print("\nВсего общих дней: {}".format(len(prices)))
print("Период: {} → {}\n".format(
    prices.index.min().date(), prices.index.max().date()))


# ---------- ПОРЯДКИ ЦЕН (только инфо) ----------
last_prices = prices.iloc[-1]
current_orders = {t: price_order(last_prices[t]) for t in prices.columns}

print("=" * 60)
print("ПОРЯДКИ ЦЕН НА ТЕКУЩУЮ ДАТУ (инфо)")
print("=" * 60)
for t in prices.columns:
    print("  {}: {:.4f} → порядок {}".format(t, last_prices[t], current_orders[t]))

state = load_state()


# ---------- ПРОГОН ----------
period_results = {}
theory_rows = []

for period_name, n_days in PERIOD_DAYS.items():
    cutoff = today - timedelta(days=n_days)
    window = prices[prices.index >= cutoff]

    if len(window) < 50:
        print("\n⚠ Период {}: мало данных, пропускаем".format(period_name))
        continue

    print("\n" + "=" * 60)
    print("ПЕРИОД: {} ({} → {}, {} дней)".format(
        period_name, window.index.min().date(), window.index.max().date(), len(window)))
    print("=" * 60)

    tickers_list = list(window.columns)
    period_results[period_name] = {}

    for i in range(len(tickers_list)):
        for j in range(i + 1, len(tickers_list)):
            ta, tb = tickers_list[i], tickers_list[j]
            pair_name = '{}/{}'.format(ta, tb)

            # Обрезка окна по сплитам
            pair_start = window.index.min()
            for t in [ta, tb]:
                if t in KNOWN_SPLITS:
                    split_date_str = KNOWN_SPLITS[t][0]
                    adjusted = add_trading_days(window.index, split_date_str, 5)
                    if adjusted > pair_start:
                        pair_start = adjusted

            sa = window[ta].loc[window.index >= pair_start]
            sb = window[tb].loc[window.index >= pair_start]
            common = sa.index.intersection(sb.index)
            if len(common) < 50:
                continue
            sa = sa.loc[common]
            sb = sb.loc[common]
            ratio = sa / sb

            res = simulate_strategy6(ratio, sa, sb, ta, tb, INITIAL_CAPITAL)
            if res is None:
                continue

            period_results[period_name][pair_name] = {
                'res': res,
                'series_a': sa,
                'series_b': sb,
                'name_a': ta,
                'name_b': tb,
            }

            ps = res['price_start_holding']
            pe = res['price_end_holding']
            if ps is not None and pe is not None and ps > 0:
                diverg = round((pe - ps) / ps * 100, 2)
            else:
                diverg = None

            theory_rows.append({
                'Период': period_name,
                'Пара': pair_name,
                'Цена акции начало': round(ps, 2) if ps is not None else None,
                'Цена акции конец': round(pe, 2) if pe is not None else None,
                'Теория': round(ps, 2) if ps is not None else None,
                'Доходность (теория), %': res['доходность_теория'],
                'Расхождение %': diverg,
                'Доходность (как в прошлых), %': res['доходность_после'],
            })

theory_df = pd.DataFrame(theory_rows)


# ---------- ФИЛЬТР ТОПа (Экстремумы >= 30: 5 лет, иначе 3 года) ----------
passed_extrema = load_extrema_threshold('analytics_z.xlsx', threshold=30)

all_pairs = set()
for pn in period_results:
    all_pairs.update(period_results[pn].keys())

top_rows = []
for pair_name in sorted(all_pairs):
    ok = pair_name in passed_extrema
    top_rows.append({
        'Пара': pair_name,
        'Прошла фильтр': 'ДА' if ok else 'НЕТ',
    })

top_df = pd.DataFrame(top_rows)
passed_pairs = sorted(passed_extrema)

# ---------- EXCLUDED FROM SIGNAL (3 года < 30) ----------
excluded_from_signal = set()
excluded_extrema_3y = {}
try:
    az_all = pd.read_excel('analytics_z.xlsx', sheet_name='Analytics_Z')
    az_3y = az_all[az_all['Период'] == '3года']
    extrema_5_3y = {}
    for _, row in az_3y.iterrows():
        pair = row.get('Пара')
        ext = row.get('Экстремумы')
        if pair is None or pd.isna(ext):
            continue
        ext = int(ext)
        extrema_5_3y[pair] = ext
        if ext < 30:
            excluded_from_signal.add(pair)
            excluded_extrema_3y[pair] = ext
    print("[excluded_from_signal] пар с 3 года < 30: {}".format(len(excluded_from_signal)))
except Exception as e:
    print("[excluded_from_signal] ошибка: {}".format(e))

prev_excluded = set(state.get('excluded_from_signal', []))
newly_excluded = excluded_from_signal - prev_excluded
returned = prev_excluded - excluded_from_signal

if newly_excluded:
    print("  Новые исключения: {}".format(', '.join(sorted(newly_excluded))))
if returned:
    print("  Вернулись в сигнал: {}".format(', '.join(sorted(returned))))

print("\n" + "=" * 60)
print("ПАР, ПРОШЕДШИХ ФИЛЬТР: {}".format(len(passed_pairs)))
print("=" * 60)
for p in passed_pairs:
    print("  {}".format(p))


# ---------- ОСНОВНЫЕ ТАБЛИЦЫ ----------
main_results = []
for period_name in PERIOD_DAYS:
    if period_name not in period_results:
        continue
    for pair_name in passed_pairs:
        if pair_name not in period_results[period_name]:
            continue
        info = period_results[period_name][pair_name]
        res = info['res']
        sa = info['series_a']
        sb = info['series_b']

        # ФИЛЬТР: если хотя бы одна позиция не менялась — пропускаем
        if (res['start_shares_x'] == res['end_shares_x'] or
                res['start_shares_y'] == res['end_shares_y']):
            continue

        h = res['holding_ticker']
        if h == info['name_a']:
            ps = round(float(sa.iloc[0]), 2)
            pe = round(float(sa.iloc[-1]), 2)
        elif h == info['name_b']:
            ps = round(float(sb.iloc[0]), 2)
            pe = round(float(sb.iloc[-1]), 2)
        else:
            ps = None
            pe = None

        main_results.append({
            'Период': period_name,
            'Пара': pair_name,
            'Сделок': res['сделок'],
            'Отменено': res['отменено'],
            'Довнесений': res['довнесений'],
            'Старт X, акций': res['start_shares_x'],
            'Финал X, акций': res['end_shares_x'],
            'Старт Y, акций': res['start_shares_y'],
            'Финал Y, акций': res['end_shares_y'],
            'Внесено, руб.': round(res['внесено'], 2),
            'Деньги в конце, руб.': round(res['заработано_до'], 2),
            'Налог, руб.': round(res['налог'], 2),
            'Комиссия, руб.': round(res['комиссия'], 2),
            'Заработано (после), руб.': round(res['заработано_после'], 2),
            'Доходность (после), %': res['доходность_после'],
            'Где деньги в конце': res['final_position'],
            'Цена акции начало': ps,
            'Цена акции конец': pe,
        })

main_df = pd.DataFrame(main_results)

# ---------- ТОП по периодам (для сообщений) ----------
top_by_period = {}
for period_name in ['5 лет', '3 года']:
    sub = main_df[main_df['Период'] == period_name].copy() if not main_df.empty else pd.DataFrame()
    if sub.empty:
        top_by_period[period_name] = {}
        continue
    sub = sub.sort_values('Доходность (после), %', ascending=False).reset_index(drop=True)
    total = len(sub)
    d = {}
    for i, row in sub.iterrows():
        d[row['Пара']] = {
            'rank': i + 1,
            'total': total,
            'pct': row['Доходность (после), %'],
        }
    top_by_period[period_name] = d
print("[top_by_period] 5 лет: {} пар, 3 года: {} пар".format(
    len(top_by_period.get('5 лет', {})),
    len(top_by_period.get('3 года', {}))))

# Вывод основных таблиц в консоль
if not main_df.empty:
    print()
    print("=" * 100)
    print("ОСНОВНЫЕ ТАБЛИЦЫ (ТОП-5 пар по доходности):")
    print("=" * 100)
    for period_name in PERIOD_DAYS:
        sub = main_df[main_df['Период'] == period_name]
        if sub.empty:
            continue
        print()
        print("--- {} ({} строк) ---".format(period_name, len(sub)))
        sub_top = sub.sort_values('Доходность (после), %', ascending=False).head(5)
        cols_show = ['Пара', 'Сделок', 'Отменено', 'Довнесений',
                     'Внесено, руб.', 'Доходность (после), %', 'Где деньги в конце']
        available = [c for c in cols_show if c in sub_top.columns]
        print(sub_top[available].to_string(index=False))
    print()
    print("=" * 100)
    print("Всего строк в основных таблицах: {}".format(len(main_df)))
    print("=" * 100)


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
            sheet_name = '{}_ТОП'.format(period_name).replace(' ', '')
            sub.to_excel(w, sheet_name=sheet_name[:31], index=False)

    if not theory_df.empty:
        for period_name in PERIOD_DAYS:
            sub = theory_df[theory_df['Период'] == period_name].copy()
            if sub.empty:
                continue
            sub = sub.sort_values('Доходность (теория), %', ascending=False)
            sheet_name = '{}_Теория'.format(period_name).replace(' ', '')
            sub.to_excel(w, sheet_name=sheet_name[:31], index=False)

print("\n✓ Готово: {}".format(OUTPUT_FILE))


# ---------- ANALYTICS_10 (загрузка) ----------
current_analytics_10 = {}
try:
    az10 = pd.read_excel('analytics_z.xlsx', sheet_name='Analytics_Z_10')
    az10_3y = az10[az10['Период'] == '3года']
    for _, row in az10_3y.iterrows():
        pair = row.get('Пара')
        if pair is None or pd.isna(pair):
            continue
        current_analytics_10[pair] = {
            'min_z_10': float(row['min Z 10']) if not pd.isna(row.get('min Z 10')) else None,
            'max_z_10': float(row['max Z 10']) if not pd.isna(row.get('max Z 10')) else None,
            'p_max_10': float(row['P max']) if not pd.isna(row.get('P max')) else None,
            'p_min_10': float(row['P min']) if not pd.isna(row.get('P min')) else None,
        }
    print("[analytics_10] загружено пар (3 года): {}".format(len(current_analytics_10)))
except Exception as e:
    print("[analytics_10] ошибка: {}".format(e))


# ---------- TELEGRAM ----------
def send_telegram(text):
    url = "{}/bot{}/sendMessage".format(TG_PROXY, TG_TOKEN)
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=30)
        print("[telegram] status={}".format(r.status_code))
        return r.status_code == 200
    except Exception as e:
        print("[telegram] ошибка: {}".format(e))
        return False


if SEND_TELEGRAM:
    # 1. Уведомления об изменениях порядка цен
    # 2. Сигналы (как было — level обновляется)
    signaled = []

    if "1 год" in period_results:
        for pair_name in list(period_results["1 год"].keys()):
            info = period_results["1 год"][pair_name]
            res = info['res']
            sa = info['series_a']
            sb = info['series_b']

            z_now = float(sa.iloc[-1] / sb.iloc[-1])
            level_X_now = res.get('last_level_X', 0) or 0
            level_Y_now = res.get('last_level_Y', 0) or 0

            action = None
            chg = 0.0
            level_now = 0.0

            if level_X_now > 0 and z_now >= level_X_now * (1 + STEP):
                chg = (z_now - level_X_now) / level_X_now
                level_now = level_X_now
                action = "Перелить {} → {}".format(info['name_a'], info['name_b'])
            elif level_Y_now > 0 and z_now <= level_Y_now * (1 - STEP):
                chg = (z_now - level_Y_now) / level_Y_now
                level_now = level_Y_now
                action = "Перелить {} → {}".format(info['name_b'], info['name_a'])

            if action is not None:
                # Пропускаем пары, исключённые из сигналов (3 года < 30)
                if pair_name in excluded_from_signal:
                    continue

                passed_filter = pair_name in passed_pairs

                az10 = current_analytics_10.get(pair_name, {})
                t5 = top_by_period.get('5 лет', {}).get(pair_name, {})
                t3 = top_by_period.get('3 года', {}).get(pair_name, {})

                signaled.append({
                    'pair_name': pair_name,
                    'name_a': info['name_a'],
                    'name_b': info['name_b'],
                    'z_now': z_now,
                    'level_now': level_now,
                    'level_X': level_X_now,
                    'level_Y': level_Y_now,
                    'chg': chg,
                    'action': action,
                    'passed_filter': passed_filter,
                    'signal_up': 0,
                    'signal_down': 0,
                    # новые поля
                    'extrema_5_3y': extrema_5_3y.get(pair_name),
                    'min_z_10': az10.get('min_z_10'),
                    'max_z_10': az10.get('max_z_10'),
                    'p_max_10': az10.get('p_max_10'),
                    'p_min_10': az10.get('p_min_10'),
                    'top_5y_pct': t5.get('pct'),
                    'top_5y_rank': t5.get('rank'),
                    'top_5y_total': t5.get('total'),
                    'top_3y_pct': t3.get('pct'),
                    'top_3y_rank': t3.get('rank'),
                    'top_3y_total': t3.get('total'),
                })

    if signaled:
        for s in signaled:
            msg = "🔔 СИГНАЛ ПО ПАРЕ\n\n"
            if s['passed_filter']:
                msg += "📊 ТОП (в фильтре)\n"
            else:
                msg += "⚠️ Пара НЕ в ТОПе\n"
            msg += "Пара: {}/{}\n\n".format(s['name_a'], s['name_b'])
            msg += "📈 Сегодня: {} / {}\n".format(s['name_a'], s['name_b'])
            msg += "💰 Z: {:.4f} → {:.4f} ({:+.2f}%)\n".format(
                s['level_now'], s['z_now'], s['chg'] * 100)

            # level_X / level_Y
            msg += "\n📋 level_X = {:.4f}, level_Y = {:.4f}\n".format(
                s.get('level_X', 0) or 0, s.get('level_Y', 0) or 0)

            # Экстремумы 5% (3 года)
            ext = s.get('extrema_5_3y')
            if ext is not None:
                msg += "🔬 Экстремумы 5% (3 года): {}\n".format(ext)

            # Z min/max 10% (3 года)
            mn = s.get('min_z_10')
            mx = s.get('max_z_10')
            if mn is not None and mx is not None:
                msg += "📉 Z min/max (10%, 3 года): {:.4f} / {:.4f}\n".format(mn, mx)

            # P max / P min 10%
            pmax = s.get('p_max_10')
            pmin = s.get('p_min_10')
            if pmax is not None and pmin is not None:
                msg += "📊 P max / P min (10%, 3 года): {:.4f} / {:.4f}\n".format(pmax, pmin)

            # ТОП 5 лет
            if s.get('top_5y_pct') is not None:
                msg += "🏆 ТОП 5 лет: {:.2f}% (место {}/{})\n".format(
                    s['top_5y_pct'], s['top_5y_rank'], s['top_5y_total'])

            # ТОП 3 года
            if s.get('top_3y_pct') is not None:
                msg += "🏆 ТОП 3 года: {:.2f}% (место {}/{})\n".format(
                    s['top_3y_pct'], s['top_3y_rank'], s['top_3y_total'])

            msg += "\n➡️ Действие: {}".format(s['action'])

            send_telegram(msg)
        print("[telegram] отправлено сигналов: {}".format(len(signaled)))
    else:
        print("[telegram] сигналов нет")


# ---------- ANALYTICS_10 (сравнение) ----------
prev_analytics_10 = state.get('analytics_10', {})
analytics_changes = []

for pair, cur in current_analytics_10.items():
    prev = prev_analytics_10.get(pair)
    if prev is None:
        continue  # новая пара — не сообщаем
    pair_changes = []
    for key, label in [('min_z_10', 'min Z'), ('max_z_10', 'max Z'),
                       ('p_max_10', 'P max'), ('p_min_10', 'P min')]:
        old_v = prev.get(key)
        new_v = cur.get(key)
        if old_v is None or new_v is None:
            continue
        if abs(old_v - new_v) > 1e-6:
            pair_changes.append((label, old_v, new_v))
    if pair_changes:
        analytics_changes.append((pair, pair_changes))

if analytics_changes:
    print()
    print("=" * 60)
    print("ANALYTICS_10 (3 года): ИЗМЕНЕНИЯ")
    print("=" * 60)
    for pair, changes in analytics_changes:
        print()
        print("  {}".format(pair))
        for label, old_v, new_v in changes:
            print("    {}: {:.4f} → {:.4f}".format(label, old_v, new_v))

    # Telegram-сообщение (одно на все изменения)
    msg = "📊 Analytics_Z (10%): изменения\n\n"
    for i, (pair, changes) in enumerate(analytics_changes, 1):
        msg += "{}. {}\n".format(i, pair)
        for label, old_v, new_v in changes:
            msg += "   {}: {:.4f} → {:.4f}\n".format(label, old_v, new_v)
        msg += "\n"
    send_telegram(msg)
    print("[telegram] отправлено сообщение об изменениях analytics_10")
else:
    print("[analytics_10] изменений нет")


# ---------- TELEGRAM: исключения / возвраты ----------
if newly_excluded or returned:
    msg = "🔔 Analytics_Z: изменения фильтра (3 года)\n\n"
    if newly_excluded:
        msg += "⚠️ Исключены из сигнала (Экстремумы 3 года &lt; 30):\n"
        for pair in sorted(newly_excluded):
            ext = excluded_extrema_3y.get(pair, '?')
            msg += "  • {} (Экстремумы 3 года = {})\n".format(pair, ext)
        msg += "\n"
    if returned:
        msg += "✅ Вернулись в сигнал (Экстремумы 3 года >= 30):\n"
        for pair in sorted(returned):
            msg += "  • {}\n".format(pair)
        msg += "\n"
    send_telegram(msg)


# ---------- STATE ----------
new_state = {
    'excluded_notified': state.get('excluded_notified', []),
    'excluded_from_signal': sorted(excluded_from_signal),
    'analytics_10': current_analytics_10,
    'last_update': datetime.now().isoformat(),
}
save_state(new_state)
print("\n✓ state.json обновлён")

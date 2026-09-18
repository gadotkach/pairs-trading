# -*- coding: utf-8 -*-
"""ТЕСТ: 01.06.2022 -> сегодня, сумма 100 000 руб.
Две стратегии: 1 (без довнесений) и 2 (с довнесениями).
Один фильтр ТОПа (доходность за период > 10%).
НЕ трогает state.json и Telegram."""

import os
from datetime import datetime, timedelta
import pandas as pd

TICKERS = ['GAZP', 'LKOH', 'SBER', 'SNGS', 'GMKN', 'NVTK',
           'ROSN', 'TATN', 'PLZL', 'MOEX', 'VTBR',
           'SBERP', 'TATNP']

CACHE_DIR = 'cache'
TEST_START = '2022-06-01'
TEST_AMOUNT = 100_000
TOPUP_AMOUNT = 10_000
OUTPUT_FILE = 'test_analysis.xlsx'

STEP = 0.05
PRICE_FILTER = 1.03
TAX_RATE = 0.13
COMMISSION_RATE = 0.003
MIN_TRADES = 2
THEORY_THRESHOLD = 10.0

SPLITS = {
    'VTBR': ('2024-07-15', 5000),
    'PLZL': ('2025-03-27', 10),
    'GMKN': ('2024-04-08', 100),
}
# SPLIT_TICKERS_IN_WINDOW больше не используется (разбиваем на под-пары)
SPLIT_TICKERS_IN_WINDOW = set()

TOPA_TABLE = None


def load_prices():
    series = {}
    for t in TICKERS:
        path = os.path.join(CACHE_DIR, t + '.csv')
        if not os.path.exists(path):
            print('  ! net kesha ' + t)
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        if t not in df.columns:
            for c in df.columns:
                if c.upper() == t.upper():
                    df = df.rename(columns={c: t})
                    break
        if t in df.columns:
            series[t] = df[t].astype(float)
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index().dropna()


def corrected_prices(prices_raw):
    p = prices_raw.copy()
    for t, (d_str, coeff) in SPLITS.items():
        if t not in p.columns:
            continue
        d = pd.Timestamp(d_str)
        mask = p.index < d
        p.loc[mask, t] = p.loc[mask, t] / coeff
    return p


def build_split_subseries(window, ticker, splits):
    """Возвращает список (name, series) — под-серии тикера с учётом сплитов.

    До сплита: name = 'VTBR_2024', цены делим на коэффициент.
    После сплита: name = 'VTBR', цены без изменений.
    """
    if ticker not in splits:
        return [(ticker, window[ticker])]

    series = window[ticker]
    split_date_str, coeff = splits[ticker]  # только один сплит на тикер
    split_date = pd.Timestamp(split_date_str)

    subseries = []
    # до сплита
    mask_before = series.index < split_date
    sub_before = series.loc[mask_before]
    if len(sub_before) >= 2:
        year = split_date.year
        name_before = '{}_{}'.format(ticker, year)
        # НЕ делим на коэффициент — оставляем реальные цены
        subseries.append((name_before, sub_before.copy()))

    # после сплита
    mask_after = series.index >= split_date
    sub_after = series.loc[mask_after]
    if len(sub_after) >= 2:
        subseries.append((ticker, sub_after))

    return subseries


def build_split_pairs(window, splits):
    """Возвращает список под-пар: [(name_a, sa, name_b, sb), ...]."""
    tickers = list(window.columns)
    subseries = {}
    for t in tickers:
        subseries[t] = build_split_subseries(window, t, splits)

    pairs = []
    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            ta, tb = tickers[i], tickers[j]
            for name_a, ser_a in subseries[ta]:
                for name_b, ser_b in subseries[tb]:
                    common = ser_a.index.intersection(ser_b.index)
                    if len(common) < 2:
                        continue
                    sa = ser_a.loc[common]
                    sb = ser_b.loc[common]
                    pairs.append((name_a, sa, name_b, sb))
    return pairs


def price_order(p):
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


def add_trading_days(index, start_date_str, n=5):
    """Возвращает дату start_date + n торговых дней (по индексу)."""
    start_date = pd.Timestamp(start_date_str)
    dates_after = index[index > start_date]
    if len(dates_after) < n:
        return dates_after[-1] if len(dates_after) else start_date
    return dates_after[n - 1]


def simulate_strategy1(ratio, px, py, tx, ty, initial):
    """Стратегия 1: целые акции, cash обнуляется при каждой сделке
    через внешнее довнесение остатка."""
    if len(ratio) < 2:
        return None

    start_px = float(px.iloc[0])
    start_py = float(py.iloc[0])

    shares_x = int(initial / start_px)
    cash = initial - shares_x * start_px
    shares_y = 0
    start_shares_x = shares_x
    first_buy_y = None
    cost_x = initial
    cost_y = 0.0

    x_last = start_px
    y_last = None
    level = float(ratio.iloc[0])

    invested = initial
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

        if v >= level * (1 + STEP):
            if shares_x > 0 and p_x >= x_last * PRICE_FILTER:
                sell = shares_x * p_x
                c = sell * COMMISSION_RATE
                net = sell - c
                comm += c
                profit = sell - cost_x
                if profit > 0:
                    t_ = profit * TAX_RATE
                    tax += t_
                    net -= t_
                cash += net
                # покупаем Y на весь cash (целое)
                new_shares_y = int(cash / p_y)
                rem = cash - new_shares_y * p_y
                if rem > 0:
                    invested += rem
                    topups += 1
                    new_shares_y += 1
                    cost_y = cash
                    cash = 0
                else:
                    cost_y = cash
                    cash = 0
                shares_y = new_shares_y
                if first_buy_y is None:
                    first_buy_y = new_shares_y
                trades += 1
                shares_x = 0
                cost_x = 0
                x_last = p_x
                y_last = p_y
            else:
                cancelled += 1
            level = v

        elif v <= level * (1 - STEP):
            if shares_y > 0 and p_y >= y_last * PRICE_FILTER:
                sell = shares_y * p_y
                c = sell * COMMISSION_RATE
                net = sell - c
                comm += c
                profit = sell - cost_y
                if profit > 0:
                    t_ = profit * TAX_RATE
                    tax += t_
                    net -= t_
                cash += net
                new_shares_x = int(cash / p_x)
                rem = cash - new_shares_x * p_x
                if rem > 0:
                    invested += rem
                    topups += 1
                    new_shares_x += 1
                    cost_x = cash
                    cash = 0
                else:
                    cost_x = cash
                    cash = 0
                shares_x = new_shares_x
                trades += 1
                shares_y = 0
                cost_y = 0
                x_last = p_x
                y_last = p_y
            else:
                cancelled += 1
            level = v

    return _finalize_v2(shares_x, shares_y, cash, cost_x, cost_y,
                        px, py, tx, ty, start_px, start_py,
                        invested, tax, comm, trades, cancelled,
                        topups, start_shares_x, first_buy_y)


def simulate_strategy2(ratio, px, py, tx, ty, initial, topup_amount):
    """Стратегия 2: целые акции, свободный кэш, внешние довнесения."""
    if len(ratio) < 2:
        return None

    start_px = float(px.iloc[0])
    start_py = float(py.iloc[0])

    # Старт: целое число акций X + остаток в cash
    shares_x = int(initial / start_px)
    cash = initial - shares_x * start_px
    shares_y = 0
    start_shares_x = shares_x
    first_buy_y = None
    cost_x = initial  # вся сумма
    cost_y = 0.0

    level = float(ratio.iloc[0])
    invested = initial
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

        if v >= level * (1 + STEP):
            if shares_x > 0:
                avg_x = cost_x / shares_x
                if p_x >= avg_x * PRICE_FILTER:
                    # продажа X, покупка Y
                    sell = shares_x * p_x
                    c = sell * COMMISSION_RATE
                    net = sell - c
                    comm += c
                    profit = sell - cost_x
                    if profit > 0:
                        t_ = profit * TAX_RATE
                        tax += t_
                        net -= t_
                    cash += net
                    # покупаем Y на весь кэш (целое)
                    new_shares_y = int(cash / p_y)
                    if new_shares_y > 0:
                        buy_sum = new_shares_y * p_y
                        cost_y = cash
                        cash = cash - buy_sum
                        shares_y = new_shares_y
                        if first_buy_y is None:
                            first_buy_y = new_shares_y
                    # если не хватило — оставляем в cash
                    trades += 1
                    shares_x = 0
                    cost_x = 0
                else:
                    # попытка довнесения
                    if cash + topup_amount >= p_x:
                        invested += topup_amount
                        buy_amount = cash + topup_amount
                        new_shares = int(buy_amount / p_x)
                        shares_x += new_shares
                        cost_x += buy_amount
                        cash = buy_amount - new_shares * p_x
                        topups += 1
                    else:
                        cancelled += 1
            else:
                pass
            level = v

        elif v <= level * (1 - STEP):
            if shares_y > 0:
                avg_y = cost_y / shares_y
                if p_y >= avg_y * PRICE_FILTER:
                    sell = shares_y * p_y
                    c = sell * COMMISSION_RATE
                    net = sell - c
                    comm += c
                    profit = sell - cost_y
                    if profit > 0:
                        t_ = profit * TAX_RATE
                        tax += t_
                        net -= t_
                    cash += net
                    new_shares_x = int(cash / p_x)
                    if new_shares_x > 0:
                        buy_sum = new_shares_x * p_x
                        cost_x = cash
                        cash = cash - buy_sum
                        shares_x = new_shares_x
                    trades += 1
                    shares_y = 0
                    cost_y = 0
                else:
                    if cash + topup_amount >= p_y:
                        invested += topup_amount
                        buy_amount = cash + topup_amount
                        new_shares = int(buy_amount / p_y)
                        shares_y += new_shares
                        cost_y += buy_amount
                        cash = buy_amount - new_shares * p_y
                        topups += 1
                    else:
                        cancelled += 1
            else:
                pass
            level = v

    return _finalize_v2(shares_x, shares_y, cash, cost_x, cost_y,
                        px, py, tx, ty, start_px, start_py,
                        invested, tax, comm, trades, cancelled,
                        topups, start_shares_x, first_buy_y)


def simulate_strategy3(ratio, px, py, tx, ty, initial):
    """Стратегия 3: Z1 фиксирован, остальное как в Стратегии 1."""
    if len(ratio) < 2:
        return None

    start_px = float(px.iloc[0])
    start_py = float(py.iloc[0])

    shares_x = int(initial / start_px)
    cash = initial - shares_x * start_px
    shares_y = 0
    start_shares_x = shares_x
    first_buy_y = None
    cost_x = initial
    cost_y = 0.0

    x_last = start_px
    y_last = None
    Z1 = start_px / start_py   # ЗАФИКСИРОВАН, не меняется

    invested = initial
    tax = 0.0
    comm = 0.0
    trades = 0
    cancelled = 0
    topups = 0
    last_buy_price_holding = start_px  # для теории

    for i in range(1, len(ratio)):
        d = ratio.index[i]
        v = float(ratio.iloc[i])
        p_x = float(px.loc[d])
        p_y = float(py.loc[d])

        if v >= Z1 * (1 + STEP):
            # сигнал X→Y
            if shares_x > 0 and p_x >= x_last * PRICE_FILTER:
                sell = shares_x * p_x
                c = sell * COMMISSION_RATE
                net = sell - c
                comm += c
                profit = sell - cost_x
                if profit > 0:
                    t_ = profit * TAX_RATE
                    tax += t_
                    net -= t_
                cash += net
                new_shares_y = int(cash / p_y)
                rem = cash - new_shares_y * p_y
                if rem > 0:
                    invested += rem
                    topups += 1
                    new_shares_y += 1
                    cost_y = cash
                    cash = 0
                else:
                    cost_y = cash
                    cash = 0
                shares_y = new_shares_y
                if first_buy_y is None:
                    first_buy_y = new_shares_y
                trades += 1
                shares_x = 0
                cost_x = 0
                x_last = p_x
                y_last = p_y
                last_buy_price_holding = p_y
            else:
                cancelled += 1
            # Z1 НЕ ОБНОВЛЯЕТСЯ

        elif v <= Z1 * (1 - STEP):
            # сигнал Y→X
            if shares_y > 0 and p_y >= y_last * PRICE_FILTER:
                sell = shares_y * p_y
                c = sell * COMMISSION_RATE
                net = sell - c
                comm += c
                profit = sell - cost_y
                if profit > 0:
                    t_ = profit * TAX_RATE
                    tax += t_
                    net -= t_
                cash += net
                new_shares_x = int(cash / p_x)
                rem = cash - new_shares_x * p_x
                if rem > 0:
                    invested += rem
                    topups += 1
                    new_shares_x += 1
                    cost_x = cash
                    cash = 0
                else:
                    cost_x = cash
                    cash = 0
                shares_x = new_shares_x
                trades += 1
                shares_y = 0
                cost_y = 0
                x_last = p_x
                y_last = p_y
                last_buy_price_holding = p_x
            else:
                cancelled += 1
            # Z1 НЕ ОБНОВЛЯЕТСЯ

    return _finalize_v2(shares_x, shares_y, cash, cost_x, cost_y,
                        px, py, tx, ty, start_px, start_py,
                        invested, tax, comm, trades, cancelled,
                        topups, start_shares_x, first_buy_y,
                        last_buy_price_holding)


def simulate_strategy4(ratio, px, py, tx, ty, initial, topup_amount):
    """Стратегия 4: Z1 фиксирован + довнесения как в Стратегии 2 + целые акции."""
    if len(ratio) < 2:
        return None

    start_px = float(px.iloc[0])
    start_py = float(py.iloc[0])

    shares_x = int(initial / start_px)
    cash = initial - shares_x * start_px
    shares_y = 0
    start_shares_x = shares_x
    first_buy_y = None
    cost_x = initial
    cost_y = 0.0

    x_last = start_px
    y_last = None
    Z1 = start_px / start_py   # фиксирован

    invested = initial
    tax = 0.0
    comm = 0.0
    trades = 0
    cancelled = 0
    topups = 0
    last_buy_price_holding = start_px

    for i in range(1, len(ratio)):
        d = ratio.index[i]
        v = float(ratio.iloc[i])
        p_x = float(px.loc[d])
        p_y = float(py.loc[d])

        if v >= Z1 * (1 + STEP):
            if shares_x > 0:
                avg_x = cost_x / shares_x
                if p_x >= avg_x * PRICE_FILTER:
                    # сделка: продажа X -> целые Y + довнесение остатка
                    sell = shares_x * p_x
                    c = sell * COMMISSION_RATE
                    net = sell - c
                    comm += c
                    profit = sell - cost_x
                    if profit > 0:
                        t_ = profit * TAX_RATE
                        tax += t_
                        net -= t_
                    cash += net
                    new_shares_y = int(cash / p_y)
                    rem = cash - new_shares_y * p_y
                    if rem > 0:
                        invested += rem
                        topups += 1
                        new_shares_y += 1
                        cost_y = cash
                        cash = 0
                    else:
                        cost_y = cash
                        cash = 0
                    shares_y = new_shares_y
                    if first_buy_y is None:
                        first_buy_y = new_shares_y
                    trades += 1
                    shares_x = 0
                    cost_x = 0
                    x_last = p_x
                    y_last = p_y
                    last_buy_price_holding = p_y
                elif avg_x > p_x * 1.10:
                    # довнесение 10 000 (при отмене)
                    buy_amount = cash + topup_amount
                    new_shares = int(buy_amount / p_x)
                    if new_shares > 0:
                        invested += topup_amount
                        shares_x += new_shares
                        cost_x += buy_amount
                        cash = buy_amount - new_shares * p_x
                        topups += 1
                    else:
                        cancelled += 1
                else:
                    cancelled += 1
            else:
                cancelled += 1
            # Z1 НЕ ОБНОВЛЯЕТСЯ

        elif v <= Z1 * (1 - STEP):
            if shares_y > 0:
                avg_y = cost_y / shares_y
                if p_y >= avg_y * PRICE_FILTER:
                    sell = shares_y * p_y
                    c = sell * COMMISSION_RATE
                    net = sell - c
                    comm += c
                    profit = sell - cost_y
                    if profit > 0:
                        t_ = profit * TAX_RATE
                        tax += t_
                        net -= t_
                    cash += net
                    new_shares_x = int(cash / p_x)
                    rem = cash - new_shares_x * p_x
                    if rem > 0:
                        invested += rem
                        topups += 1
                        new_shares_x += 1
                        cost_x = cash
                        cash = 0
                    else:
                        cost_x = cash
                        cash = 0
                    shares_x = new_shares_x
                    trades += 1
                    shares_y = 0
                    cost_y = 0
                    x_last = p_x
                    y_last = p_y
                    last_buy_price_holding = p_x
                elif avg_y > p_y * 1.10:
                    buy_amount = cash + topup_amount
                    new_shares = int(buy_amount / p_y)
                    if new_shares > 0:
                        invested += topup_amount
                        shares_y += new_shares
                        cost_y += buy_amount
                        cash = buy_amount - new_shares * p_y
                        topups += 1
                    else:
                        cancelled += 1
                else:
                    cancelled += 1
            else:
                cancelled += 1
            # Z1 НЕ ОБНОВЛЯЕТСЯ

    return _finalize_v2(shares_x, shares_y, cash, cost_x, cost_y,
                        px, py, tx, ty, start_px, start_py,
                        invested, tax, comm, trades, cancelled,
                        topups, start_shares_x, first_buy_y,
                        last_buy_price_holding)


def _finalize_v2(shares_x, shares_y, cash, cost_x, cost_y,
                 px, py, tx, ty, start_px, start_py,
                 invested, tax, comm, trades, cancelled,
                 topups, start_shares_x, first_buy_y,
                 last_buy_price_holding=None):
    final_px = float(px.iloc[-1])
    final_py = float(py.iloc[-1])
    if shares_x > 0:
        holding = tx
        shares_h = shares_x
        ps_h = start_px
        pe_h = final_px
    elif shares_y > 0:
        holding = ty
        shares_h = shares_y
        ps_h = start_py
        pe_h = final_py
    else:
        holding = None
        shares_h = 0
        ps_h = None
        pe_h = None

    final_value = shares_x * final_px + shares_y * final_py + cash
    final_after = final_value - tax - comm
    return_pct = ((final_after - invested) / invested * 100) if invested else 0

    # ТЕОРИЯ (Вариант 3): считаем по last_buy_price_holding
    if holding is not None and last_buy_price_holding is not None and shares_h > 0:
        theory_value = shares_h * last_buy_price_holding
        theory_after = theory_value - tax - comm
        return_theory_pct = ((theory_after - invested) / invested * 100) if invested else 0
    else:
        theory_value = 0.0
        theory_after = 0.0
        return_theory_pct = 0.0

    if holding:
        final_pos = '{} {} + cash {:.2f}'.format(int(shares_h), holding, cash)
    else:
        final_pos = 'cash {:.2f}'.format(cash)

    return {
        'vneseno': invested,
        'zarabotano_do': final_value,
        'nalog': tax,
        'komissiya': comm,
        'zarabotano_posle': final_after,
        'dohodnost_posle': round(return_pct, 2),
        'dohodnost_teoria': round(return_theory_pct, 2),
        'sdelok': trades,
        'otmeneno': cancelled,
        'dovneseniy': topups,
        'start_shares_x': start_shares_x,
        'end_shares_x': shares_x,
        'start_shares_y': first_buy_y if first_buy_y is not None else 0,
        'end_shares_y': shares_y,
        'final_position': final_pos,
        'holding_ticker': holding,
        'shares_holding': shares_h,
        'last_buy_price_holding': last_buy_price_holding,
        'price_start_holding': ps_h,
        'price_end_holding': pe_h,
        'cash_final': round(cash, 2),
    }


def compute_passed_pairs(prices_corr):
    """Фильтр ТОПа: Экстремумы >= 30 из analytics_z.xlsx."""
    global TOPA_TABLE
    AZ_FILE = 'analytics_z.xlsx'
    EXTREMA_THRESHOLD = 30

    print('=' * 60)
    print('FILTER TOPA (Экстремумы >= {})'.format(EXTREMA_THRESHOLD))
    print('=' * 60)

    # Загружаем analytics_z
    try:
        az = pd.read_excel(AZ_FILE, sheet_name='Analytics_Z')
        az_max = az.groupby('Пара')['Экстремумы'].max()
        print("Analytics_Z загружен: {} пар".format(len(az_max)))
    except Exception as e:
        print("Ошибка analytics_z: {}".format(e))
        TOPA_TABLE = pd.DataFrame()
        return []

    w = prices_corr[prices_corr.index >= TEST_START]
    if len(w) < 50:
        print('Malo dannyh v okne')
        TOPA_TABLE = pd.DataFrame()
        return []

    tickers = list(w.columns)
    table_rows = []
    passed = []

    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            ta, tb = tickers[i], tickers[j]
            pair_name = ta + '/' + tb

            extrema = az_max.get(pair_name, 0)
            if pd.isna(extrema):
                extrema = 0
            ok = extrema >= EXTREMA_THRESHOLD
            if ok:
                passed.append(pair_name)
            table_rows.append({
                'Пара': pair_name,
                'Экстремумы': int(extrema) if extrema else 0,
                'Прошла': 'ДА' if ok else 'НЕТ',
            })

    table_rows.sort(key=lambda x: x['Экстремумы'], reverse=True)
    TOPA_TABLE = pd.DataFrame(table_rows)
    return passed

def compute_excluded_by_order(prices_corr):
    """Порядок цен должен совпадать И на текущую дату, И на старт периода."""
    print('=' * 60)
    print('FILTER PORYADKA CEN (tekushchaya data + start perioda)')
    print('=' * 60)

    # Дата старта периода
    start_date = prices_corr.index[prices_corr.index >= TEST_START].min()
    if start_date is None:
        print('Net dannyh na start perioda')
        return set()

    # Цены на старт и на текущую дату
    start_row = prices_corr.loc[start_date]
    last_row = prices_corr.iloc[-1]

    orders_start = {t: price_order(start_row[t]) for t in prices_corr.columns}
    orders_now = {t: price_order(last_row[t]) for t in prices_corr.columns}

    print('  Тикер   |  {:<10} |  {:<10}'.format('Старт', 'Сейчас'))
    print('  ' + '-' * 40)
    for t in prices_corr.columns:
        flag = 'OK' if orders_start[t] == orders_now[t] else 'ИЗМЕНИЛСЯ'
        print('  {:<7} |  {:<10} |  {:<10}  {}'.format(
            t, orders_start[t], orders_now[t], flag))

    excluded = set()
    tickers = list(prices_corr.columns)
    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            a, b = tickers[i], tickers[j]
            pair = a + '/' + b

            # Проверяем на старте
            start_ok = orders_start[a] == orders_start[b]
            # Проверяем сейчас
            now_ok = orders_now[a] == orders_now[b]

            if not (start_ok and now_ok):
                excluded.add(pair)

    print('')
    print('Par isklyucheno po poryadku (start + now): {}'.format(len(excluded)))
    print('')
    return excluded


def run_strategy(prices_raw, passed_pairs, excluded_by_order, name):
    """Прогон стратегии по всем подходящим парам."""
    window = prices_raw[prices_raw.index >= TEST_START]
    tickers = list(window.columns)
    rows = []
    skipped_split = 0
    skipped_order = 0
    skipped_filter = 0
    skipped_trades = 0

    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            ta, tb = tickers[i], tickers[j]
            pair_name = ta + '/' + tb

            if ta in SPLIT_TICKERS_IN_WINDOW or tb in SPLIT_TICKERS_IN_WINDOW:
                skipped_split += 1
                continue
            # Для strategy3 фильтр ТОПа отключён
            if name != 'strategy3':
                if pair_name not in passed_pairs:
                    skipped_filter += 1
                    continue
            # 3.3 фильтр порядка цен ОТКЛЮЧЁН
            # if pair_name in excluded_by_order:
            #     skipped_order += 1
            #     continue

            sa = window[ta]
            sb = window[tb]
            common = sa.index.intersection(sb.index)
            if len(common) < 2:
                continue
            sa = sa.loc[common]
            sb = sb.loc[common]
            ratio = sa / sb

            if name == 'strategy1':
                res = simulate_strategy1(ratio, sa, sb, ta, tb, TEST_AMOUNT)
            elif name == 'strategy3':
                res = simulate_strategy3(ratio, sa, sb, ta, tb, TEST_AMOUNT)
            elif name == 'strategy4':
                res = simulate_strategy4(ratio, sa, sb, ta, tb,
                                         TEST_AMOUNT, TOPUP_AMOUNT)
            else:
                res = simulate_strategy2(ratio, sa, sb, ta, tb,
                                         TEST_AMOUNT, TOPUP_AMOUNT)

            if res is None:
                continue
            if (res['start_shares_x'] == res['end_shares_x'] or
                    res['start_shares_y'] == res['end_shares_y']):
                skipped_trades += 1
                continue

            h = res['holding_ticker']
            if h == ta:
                ps = round(float(sa.iloc[0]), 2)
                pe = round(float(sa.iloc[-1]), 2)
            elif h == tb:
                ps = round(float(sb.iloc[0]), 2)
                pe = round(float(sb.iloc[-1]), 2)
            else:
                ps = None
                pe = None

            rows.append({
                'Период': '01.06.2022',
                'Пара': pair_name,
                'Сделок': res['sdelok'],
                'Отменено': res['otmeneno'],
                'Довнесений': res['dovneseniy'],
                'Старт X, акций': round(res['start_shares_x'], 4),
                'Финал X, акций': round(res['end_shares_x'], 4),
                'Старт Y, акций': round(res['start_shares_y'], 4),
                'Финал Y, акций': round(res['end_shares_y'], 4),
                'Внесено, руб.': round(res['vneseno'], 2),
                'Деньги в конце, руб.': round(res['zarabotano_do'], 2),
                'Налог, руб.': round(res['nalog'], 2),
                'Комиссия, руб.': round(res['komissiya'], 2),
                'Заработано (после), руб.': round(res['zarabotano_posle'], 2),
                'Доходность (после), %': res['dohodnost_posle'],
                'Где деньги в конце': res['final_position'],
                'Цена акции начало': ps,
                'Цена акции конец': pe,
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values('Доходность (после), %', ascending=False).reset_index(drop=True)

    print('  Isklyucheno po split: {}'.format(skipped_split))
    print('  Isklyucheno po filtru TOP: {}'.format(skipped_filter))
    print('  Isklyucheno po poryadku cen: {}'.format(skipped_order))
    print('  Isklyucheno po sdelkam (<{}): {}'.format(MIN_TRADES, skipped_trades))
    print('  Strok v tablice: {}'.format(len(df)))
    return df


PERIODS = [
    ('01.06.2022', '2022-06-01', 0),
    ('10let', None, 2500),
    ('5let', None, 1825),
    ('3goda', None, 1200),
    ('1god', None, 365),
]


def run_strategy_for_window(window, passed_pairs, excluded_by_order, name):
    """Прогон стратегии по окну. Для сплит-тикеров окно обрезается:
    старт = дата сплита + 5 торговых дней."""
    rows = []
    tickers = list(window.columns)

    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            ta, tb = tickers[i], tickers[j]
            pair_name = ta + '/' + tb

            if name != 'strategy3' and name != 'strategy4':
                if pair_name not in passed_pairs:
                    continue

            # Обрезка окна по сплитам
            pair_start = window.index.min()
            for t in [ta, tb]:
                if t in SPLITS:
                    split_date_str, _ = SPLITS[t]
                    adjusted = add_trading_days(window.index, split_date_str, 5)
                    if adjusted > pair_start:
                        pair_start = adjusted

            sa = window[ta].loc[window.index >= pair_start]
            sb = window[tb].loc[window.index >= pair_start]
            common = sa.index.intersection(sb.index)
            if len(common) < 2:
                continue
            sa = sa.loc[common]
            sb = sb.loc[common]
            ratio = sa / sb

            if name == 'strategy1':
                res = simulate_strategy1(ratio, sa, sb, ta, tb, TEST_AMOUNT)
            elif name == 'strategy3':
                res = simulate_strategy3(ratio, sa, sb, ta, tb, TEST_AMOUNT)
            elif name == 'strategy4':
                res = simulate_strategy4(ratio, sa, sb, ta, tb,
                                         TEST_AMOUNT, TOPUP_AMOUNT)
            else:
                res = simulate_strategy2(ratio, sa, sb, ta, tb,
                                         TEST_AMOUNT, TOPUP_AMOUNT)

            if res is None:
                continue
            if res['sdelok'] < MIN_TRADES:
                continue

            h = res['holding_ticker']
            if h == ta:
                ps = round(float(sa.iloc[0]), 4)
                pe = round(float(sa.iloc[-1]), 4)
            elif h == tb:
                ps = round(float(sb.iloc[0]), 4)
                pe = round(float(sb.iloc[-1]), 4)
            else:
                ps = None
                pe = None

            ps_h = res.get('price_start_holding')
            pe_h = res.get('price_end_holding')
            if ps_h is not None and pe_h is not None and ps_h > 0:
                diverg = round((pe_h - ps_h) / ps_h * 100, 2)
            else:
                diverg = None

            rows.append({
                'Пара': pair_name,
                'Сделок': res['sdelok'],
                'Отменено': res['otmeneno'],
                'Довнесений': res['dovneseniy'],
                'Старт X, акций': int(res['start_shares_x']),
                'Финал X, акций': int(res['end_shares_x']),
                'Старт Y, акций': int(res['start_shares_y']) if res['start_shares_y'] else 0,
                'Финал Y, акций': int(res['end_shares_y']),
                'Внесено, руб.': round(res['vneseno'], 2),
                'Деньги в конце, руб.': round(res['zarabotano_do'], 2),
                'Налог, руб.': round(res['nalog'], 2),
                'Комиссия, руб.': round(res['komissiya'], 2),
                'Заработано (после), руб.': round(res['zarabotano_posle'], 2),
                'Доходность (после), %': res['dohodnost_posle'],
                'Где деньги в конце': res['final_position'],
                'Цена акции начало': ps,
                'Цена акции конец': pe,
                'Цена holding начало': round(ps_h, 4) if ps_h is not None else None,
                'Цена holding конец': round(pe_h, 4) if pe_h is not None else None,
                'Теория (цена нач.)': round(ps_h, 4) if ps_h is not None else None,
                'Доходность (теория), %': res.get('dohodnost_teoria', 0),
                'Расхождение %': diverg,
                'Доходность (реальная), %': res['dohodnost_posle'],
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values('Доходность (теория), %', ascending=False).reset_index(drop=True)
    return df


def main():
    today = datetime.today()
    print('=' * 70)
    print('TEST: multiple periods, summa {:,} rub.'.format(TEST_AMOUNT))
    print('=' * 70)

    prices_raw = load_prices()
    if prices_raw.empty:
        raise SystemExit('Net dannyh.')
    print('Vsego dney: {}'.format(len(prices_raw)))

    prices_corr = corrected_prices(prices_raw)
    passed_pairs = compute_passed_pairs(prices_corr)
    excluded_by_order = set()  # ФИЛЬТР ПОРЯДКА ОТКЛЮЧЁН

    # Собираем результаты для каждой комбинации (period, strategy)
    results = {}
    for period_name, start_date, days in PERIODS:
        print('')
        print('#' * 70)
        print('# PERIOD: {}'.format(period_name))
        print('#' * 70)

        # Определяем окно
        if days > 0:
            cutoff = today - timedelta(days=days)
            window = prices_raw[prices_raw.index >= cutoff]
        else:
            window = prices_raw[prices_raw.index >= start_date]

        if len(window) < 50:
            print('  Malo dannyh, propuskaem')
            continue

        print('  Okno: {} -> {} ({} dney)'.format(
            window.index.min().date(), window.index.max().date(), len(window)))

        # Для каждой стратегии
        for strat_name, strat_id in [('strategy3', 'Страт3'), ('strategy4', 'Страт4')]:
            # Свой пересчёт Z1 — берём начало окна
            df_result = run_strategy_for_window(
                window, passed_pairs, excluded_by_order, strat_name)
            sheet_key = '{}_{}'.format(period_name, strat_id)
            results[sheet_key] = df_result
            print('  {}: {} strok'.format(strat_id, len(df_result)))

    # Запись в Excel
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as w:
        if TOPA_TABLE is not None and not TOPA_TABLE.empty:
            TOPA_TABLE.to_excel(w, sheet_name='Фильтр_ТОПа', index=False)
        for sheet_key, df in results.items():
            if df is None:
                df = pd.DataFrame()
            df.to_excel(w, sheet_name=sheet_key[:31], index=False)

    print('')
    print('Gotovo: {}'.format(OUTPUT_FILE))


if __name__ == '__main__':
    main()

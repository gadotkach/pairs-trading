# -*- coding: utf-8 -*-
"""
strategy5.py — Стратегия 5.

Логика:
- Z = X/Y.
- level = Z на момент последнего СИГНАЛА (обновляется при каждом сигнале).
- Z_last_trade = Z на момент последней УСПЕШНОЙ сделки (для отчёта).
- Сигнал X→Y: Z >= level * 1.05.
- Сигнал Y→X: Z <= level * 0.95.
- После сигнала (и сделки, и отмены): level = Z_текущее.
- При успешной сделке: Z_last_trade = Z_текущее.
- Целые лоты (LOT_SIZE[тикер]).
- При покупке: покупаем целые лоты + довносим остаток до +1 лота (cash = 0).
- Налог FIFO (отдельный лот при каждой покупке).
- Обрезка окна по сплитам (сплит + 5 торговых дней).
- Все пары (14 тикеров, C(14,2) = 91).
- Фильтр ТОПа ОТКЛЮЧЁН.
- Фильтр 3% ОТКЛЮЧЁН.

Вывод: strategy5.xlsx (5 листов) + краткий вывод в терминал.
"""

import os
from datetime import datetime, timedelta
import pandas as pd

# ================= НАСТРОЙКИ =================
TICKERS = ['SBER', 'SBERP', 'TATNP', 'TATN', 'PLZL', 'LKOH',
           'NVTK', 'ROSN', 'VTBR', 'MOEX', 'GAZP', 'GMKN',
           'SNGS', 'SNGSP']

LOT_SIZE = {
    'SBER': 1, 'SBERP': 1, 'TATNP': 1, 'TATN': 1,
    'PLZL': 1, 'LKOH': 1, 'NVTK': 1, 'ROSN': 1, 'VTBR': 1,
    'MOEX': 10, 'GAZP': 10, 'GMKN': 10, 'SNGS': 100, 'SNGSP': 10,
}

KNOWN_SPLITS = {
    'GMKN': ['2024-04-08'],
    'PLZL': ['2025-03-27'],
    'VTBR': ['2024-07-15'],
}

PERIODS = {
    '10лет': 2500,
    '5лет':  1825,
    '3года': 1200,
    '1год':  365,
    '01.06.2022': 'from_date',
}

STEP = 0.05
INITIAL_CAPITAL = 100_000
INITIAL_FREE = 1_000_000   # свободно на внешнем счёте
TAX_RATE = 0.13
COMMISSION_RATE = 0.003

CACHE_DIR = 'cache'
OUTPUT_FILE = 'strategy6.xlsx'
# =============================================


def add_trading_days(index, start_date_str, n=5):
    start_date = pd.Timestamp(start_date_str)
    dates_after = index[index > start_date]
    if len(dates_after) < n:
        return dates_after[-1] if len(dates_after) else start_date
    return dates_after[n - 1]


def load_prices():
    series = {}
    for t in TICKERS:
        path = os.path.join(CACHE_DIR, t + '.csv')
        if not os.path.exists(path):
            print('  ! нет кэша ' + t)
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


def simulate_strategy5(ratio, px, py, tx, ty, initial, lot_x, lot_y, filter_mode='last'):
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
            if filter_mode == 'avg':
                # средневзвешенная из лотов
                if lots_x:
                    total_qty_x = sum(l['qty'] for l in lots_x)
                    total_cost_x = sum(l['qty'] * l['price'] for l in lots_x)
                    x_ref = total_cost_x / total_qty_x if total_qty_x > 0 else start_px
                else:
                    x_ref = start_px
            else:
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
            if filter_mode == 'avg':
                if lots_y:
                    total_qty_y = sum(l['qty'] for l in lots_y)
                    total_cost_y = sum(l['qty'] * l['price'] for l in lots_y)
                    y_ref = total_cost_y / total_qty_y if total_qty_y > 0 else start_py
                else:
                    y_ref = start_py
            else:
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


def main():
    today = datetime.today()

    # Загружаем Analytics_Z (фильтр по Экстремумы >= 30)
    AZ_FILE = 'analytics_z.xlsx'
    EXTREMA_THRESHOLD = 30
    try:
        az = pd.read_excel(AZ_FILE, sheet_name='Analytics_Z')
        az_max = az.groupby('Пара')['Экстремумы'].max()
        print("Analytics_Z загружен: {} пар".format(len(az_max)))
    except Exception as e:
        print("Ошибка analytics_z: {}".format(e))
        az_max = pd.Series(dtype=float)
    
    print('=' * 70)
    print('STRATEGY 6: all pairs, summa {:,} rub.'.format(INITIAL_CAPITAL))
    print('=' * 70)

    prices = load_prices()
    if prices.empty:
        print('Net dannyh.')
        return

    print('Vsego dney: {}'.format(len(prices)))
    print('Period: {} -> {}'.format(prices.index.min().date(), prices.index.max().date()))
    print('Tikerov: {}, par: {}'.format(len(prices.columns), len(prices.columns) * (len(prices.columns) - 1) // 2))
    print()

    results = {}

    for period_name, period_value in PERIODS.items():
        if isinstance(period_value, int):
            cutoff = today - timedelta(days=period_value)
            window = prices[prices.index >= cutoff]
        else:  # 'from_date'
            window = prices[prices.index >= '2022-06-01']

        if len(window) < 50:
            print('Period {}: malo dannyh'.format(period_name))
            continue

        print('=' * 70)
        print('PERIOD: {} ({} -> {}, {} dney)'.format(
            period_name, window.index.min().date(), window.index.max().date(), len(window)))
        print('=' * 70)

        tickers = list(window.columns)
        rows = []

        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                # Сначала X/Y, потом Y/X
                for reverse in [False, True]:
                    if reverse:
                        ta, tb = tickers[j], tickers[i]
                    else:
                        ta, tb = tickers[i], tickers[j]
                    pair_name = ta + '/' + tb

                    # ФИЛЬТР ТОПа по Экстремумы
                    extrema = az_max.get(pair_name, 0)
                    if pd.isna(extrema):
                        extrema = 0
                    if extrema < EXTREMA_THRESHOLD:
                        continue

                    # Обрезка по сплитам
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

                    res = simulate_strategy5(ratio, sa, sb, ta, tb,
                                             INITIAL_CAPITAL,
                                             LOT_SIZE.get(ta, 1),
                                             LOT_SIZE.get(tb, 1),
                                             filter_mode='last')
                    if res is None:
                        continue

                    # ФИЛЬТР: если хотя бы одна позиция не менялась — пропускаем
                    if (res['start_shares_x'] == res['end_shares_x'] or
                            res['start_shares_y'] == res['end_shares_y']):
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
                        'Период': period_name,
                        'Пара': pair_name,
                        'Сделок': res['сделок'],
                        'Отменено': res['отменено'],
                        'Довнесений': res['довнесений'],
                        'Старт X, шт': res['start_shares_x'],
                        'Финал X, шт': res['end_shares_x'],
                        'Старт Y, шт': res['start_shares_y'],
                        'Финал Y, шт': res['end_shares_y'],
                        'Внесено, руб.': round(res['внесено'], 2),
                        'Деньги в конце, руб.': round(res['заработано_до'], 2),
                        'Налог, руб.': round(res['налог'], 2),
                        'Комиссия, руб.': round(res['комиссия'], 2),
                        'Заработано (после), руб.': round(res['заработано_после'], 2),
                        'Доходность (после), %': res['доходность_после'],
                        'Доходность (теория), %': res['доходность_теория'],
                        'level_X': res['level_X'],
                        'level_Y': res['level_Y'],
                        'Где деньги в конце': res['final_position'],
                        'Цена акции начало': ps,
                        'Цена акции конец': pe,
                    })

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values('Доходность (после), %', ascending=False).reset_index(drop=True)
        results[period_name] = df

        print('Strok: {}'.format(len(df)))
        if not df.empty:
            print('TOP-5:')
            cols = ['Пара', 'Сделок', 'Внесено, руб.', 'Доходность (после), %', 'Где деньги в конце']
            available = [c for c in cols if c in df.columns]
            print(df[available].head(5).to_string(index=False))
        print()

    # Запись Excel
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as w:
        for period_name, df in results.items():
            sheet_name = '{}_Страт6'.format(period_name).replace(' ', '')
            df.to_excel(w, sheet_name=sheet_name[:31], index=False)

    print('=' * 70)
    print('Gotovo: {}'.format(OUTPUT_FILE))
    print('Listy: {}'.format(', '.join('{}_Страт6'.format(p) for p in results.keys())))
    print('=' * 70)


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""График Z = SBER/TATN за 3 года + экстремумы (ZigZag 5%)."""

import os
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

TICKERS = ['SBER', 'TATN']
CACHE_DIR = 'cache'
PERIOD_DAYS = 1200   # 3 года
ZIGZAG_THRESHOLD = 0.05
TEST_START = None    # 3 года от сегодня


def load_prices():
    series = {}
    for t in TICKERS:
        path = os.path.join(CACHE_DIR, t + '.csv')
        if not os.path.exists(path):
            print('Нет кэша: ' + t)
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        if t not in df.columns:
            for c in df.columns:
                if c.upper() == t.upper():
                    df = df.rename(columns={c: t})
                    break
        if t in df.columns:
            series[t] = df[t].astype(float)
    return pd.DataFrame(series).sort_index().dropna()


def count_extrema(z, threshold=0.05):
    if len(z) < 3:
        return 0, [], []
    values = z.values
    dates = z.index
    n = len(values)
    extrema_values = [values[0]]
    extrema_dates = [dates[0]]
    direction = 0
    last_extremum = values[0]

    for i in range(1, n):
        v = values[i]
        if direction == 0:
            if v >= last_extremum * (1 + threshold):
                direction = 1
                last_extremum = v
            elif v <= last_extremum * (1 - threshold):
                direction = -1
                last_extremum = v
        elif direction == 1:
            if v > last_extremum:
                last_extremum = v
            elif v <= last_extremum * (1 - threshold):
                extrema_values.append(last_extremum)
                extrema_dates.append(dates[i])
                direction = -1
                last_extremum = v
        elif direction == -1:
            if v < last_extremum:
                last_extremum = v
            elif v >= last_extremum * (1 + threshold):
                extrema_values.append(last_extremum)
                extrema_dates.append(dates[i])
                direction = 1
                last_extremum = v

    if extrema_values[-1] != values[-1]:
        extrema_values.append(values[-1])
        extrema_dates.append(dates[-1])

    n_extrema = max(0, len(extrema_values) - 1)
    return n_extrema, extrema_values, extrema_dates


def main():
    today = datetime.today()
    cutoff = today - timedelta(days=PERIOD_DAYS)

    prices = load_prices()
    window = prices[prices.index >= cutoff]

    sa = window['SBER']
    sb = window['TATN']
    common = sa.index.intersection(sb.index)
    sa = sa.loc[common]
    sb = sb.loc[common]
    z = sa / sb

    print("Окно: {} -> {} ({} дней)".format(
        z.index.min().date(), z.index.max().date(), len(z)))
    print("Z: min={:.4f}, max={:.4f}, mean={:.4f}".format(
        z.min(), z.max(), z.mean()))

    # Экстремумы
    n_extrema, extrema_vals, extrema_dates = count_extrema(z, threshold=ZIGZAG_THRESHOLD)
    print("Экстремумы (ZigZag 5%): {}".format(n_extrema))
    print("Значения:", [round(v, 4) for v in extrema_vals])
    print("Даты:", [d.strftime('%d-%m-%Y') for d in extrema_dates])

    # График
    fig, ax = plt.subplots(figsize=(18, 9))
    ax.plot(z.index, z.values, linewidth=1.0, color='steelblue',
            label='Z = SBER/TATN')

    # Отметки экстремумов
    if len(extrema_dates) > 0:
        ax.scatter(extrema_dates, extrema_vals, color='red', s=40, zorder=5,
                   label='Экстремумы (ZigZag 5%)')

    # Горизонтальная линия средней
    ax.axhline(y=z.mean(), color='gray', linestyle='--', linewidth=0.8,
               label='Средняя Z = {:.4f}'.format(z.mean()))

    # Горизонтальные линии min/max
    ax.axhline(y=z.min(), color='green', linestyle=':', linewidth=0.6,
               label='Мин Z = {:.4f}'.format(z.min()))
    ax.axhline(y=z.max(), color='orange', linestyle=':', linewidth=0.6,
               label='Макс Z = {:.4f}'.format(z.max()))

    ax.set_title('Z = SBER/TATN  ({} -> {})'.format(
        z.index.min().date(), z.index.max().date()), fontsize=14)
    ax.set_xlabel('Дата')
    ax.set_ylabel('Z = SBER / TATN')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=10)

    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.xticks(rotation=45)

    plt.tight_layout()

    out = 'sber_tatn_3y.png'
    plt.savefig(out, dpi=120)
    print('OK: ' + out)


if __name__ == '__main__':
    main()

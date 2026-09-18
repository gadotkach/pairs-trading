# -*- coding: utf-8 -*-
"""График Z = SBERP/TATN + Повтор макс/мин (красным)."""

import os
from datetime import datetime, timedelta
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

TICKERS = ['SBERP', 'TATN']
CACHE_DIR = 'cache'
PERIOD_DAYS = 1200   # 3 года
ZIGZAG_THRESHOLD = 0.10


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


def count_repeated_extrema(extrema_values, decimals=2):
    if not extrema_values:
        return None, None, 0
    from collections import Counter
    levels = [round(v, decimals) for v in extrema_values]
    counter = Counter(levels)
    repeat_levels = [lvl for lvl, cnt in counter.items() if cnt >= 2]
    if not repeat_levels:
        return None, None, 0
    return max(repeat_levels), min(repeat_levels), len(repeat_levels)


def main():
    today = datetime.today()
    cutoff = today - timedelta(days=PERIOD_DAYS)

    prices = load_prices()
    window = prices[prices.index >= cutoff]

    sa = window['SBERP']
    sb = window['TATN']
    common = sa.index.intersection(sb.index)
    sa = sa.loc[common]
    sb = sb.loc[common]
    z = sa / sb

    print("Период: {} -> {} ({} дней)".format(
        z.index.min().date(), z.index.max().date(), len(z)))
    print("Z: min={:.4f}, max={:.4f}, mean={:.4f}".format(
        z.min(), z.max(), z.mean()))

    # Экстремумы
    n_extrema, extrema_vals, extrema_dates = count_extrema(z, threshold=ZIGZAG_THRESHOLD)
    repeat_max, repeat_min, n_repeat_levels = count_repeated_extrema(extrema_vals, decimals=2)

    print("Экстремумы (ZigZag 10%): {}".format(n_extrema))
    print("Повтор макс: {}".format(repeat_max))
    print("Повтор минимум: {}".format(repeat_min))
    print("Уровней с повтором: {}".format(n_repeat_levels))

    # График
    fig, ax = plt.subplots(figsize=(18, 9))
    ax.plot(z.index, z.values, linewidth=1.0, color='steelblue',
            label='Z = SBERP/TATN')

    # Экстремумы (зелёные точки)
    if len(extrema_dates) > 0:
        ax.scatter(extrema_dates, extrema_vals, color='green', s=30, zorder=5,
                   label='Экстремумы (ZigZag 10%)')

    # Повтор макс — красная линия
    if repeat_max is not None:
        ax.axhline(y=repeat_max, color='red', linestyle='-', linewidth=2.0,
                   label='Повтор макс = {:.4f}'.format(repeat_max))

    # Повтор минимум — красная линия
    if repeat_min is not None:
        ax.axhline(y=repeat_min, color='red', linestyle='-', linewidth=2.0,
                   label='Повтор минимум = {:.4f}'.format(repeat_min))

    ax.set_title('Z = SBERP/TATN  ({} -> {})'.format(
        z.index.min().date(), z.index.max().date()), fontsize=14)
    ax.set_xlabel('Дата')
    ax.set_ylabel('Z = SBERP / TATN')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=10)

    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.xticks(rotation=45)

    plt.tight_layout()

    out = 'sberp_tatn_3y.png'
    plt.savefig(out, dpi=120)
    print('OK: ' + out)


if __name__ == '__main__':
    main()

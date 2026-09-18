# -*- coding: utf-8 -*-
"""Аналитика Z: синусоидальность пар."""

import os
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

TICKERS = ['SBER', 'SBERP', 'TATNP', 'TATN', 'PLZL', 'LKOH',
           'NVTK', 'ROSN', 'VTBR', 'MOEX', 'GAZP', 'GMKN',
           'SNGS', 'SNGSP']

CACHE_DIR = 'cache'
import sys

# Порог ZigZag: 5% по умолчанию, можно передать аргументом (например, 10)
ZIGZAG_THRESHOLD = 0.05
if len(sys.argv) > 1:
    try:
        ZIGZAG_THRESHOLD = float(sys.argv[1]) / 100.0
    except ValueError:
        print("Аргумент должен быть числом (например, 10)")
        sys.exit(1)

# Имя выходного файла зависит от порога
# Имя файла: analytics_z.xlsx для 5%, иначе analytics_z_<N>.xlsx
if int(ZIGZAG_THRESHOLD * 100) == 5:
    OUTPUT_FILE = 'analytics_z.xlsx'
else:
    OUTPUT_FILE = 'analytics_z_{}.xlsx'.format(int(ZIGZAG_THRESHOLD * 100))
PLOT_DIR = 'z_plots'
os.makedirs(PLOT_DIR, exist_ok=True)

KNOWN_SPLITS = {
    'GMKN': ['2024-04-08'],
    'PLZL': ['2025-03-27'],
    'VTBR': ['2024-07-15'],
}

PERIODS = {
    '10лет': (2500, 20),
    '5лет': (1825, 15),
    '3года': (1200, 10),
    '1год': (365, 5),
    '01.06.2022': ('from_date', 10),
}


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


def count_extrema(z, window, threshold=0.05):
    """Число экстремумов + движения + даты экстремумов.

    Возвращает: (n_extrema, avg_move, min_move, max_move, extrema_values, extrema_dates)
    """
    if len(z) < 3:
        return 0, 0.0, 0.0, 0.0, [], []

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

    # Последняя точка
    if extrema_values[-1] != values[-1]:
        extrema_values.append(values[-1])
        extrema_dates.append(dates[-1])

    n_extrema = max(0, len(extrema_values) - 1)

    # Движения между соседними экстремумами
    moves = []
    for i in range(1, len(extrema_values)):
        prev = extrema_values[i - 1]
        curr = extrema_values[i]
        if prev != 0:
            moves.append(abs(curr - prev) / abs(prev) * 100)

    if moves:
        avg_move = round(sum(moves) / len(moves), 2)
        min_move = round(min(moves), 2)
        max_move = round(max(moves), 2)
    else:
        avg_move = 0.0
        min_move = 0.0
        max_move = 0.0

    return n_extrema, avg_move, min_move, max_move, extrema_values, extrema_dates

def count_repeated_extrema(extrema_values, decimals=2):
    """Границы коридора по повторяющимся экстремумам.

    extrema_values — список значений Z в экстремумах.
    Возвращает: (repeat_max, repeat_min, n_repeat_levels).
      - repeat_max — максимальный уровень Z с повтором (верхняя граница).
      - repeat_min — минимальный уровень Z с повтором (нижняя граница).
      - n_repeat_levels — сколько уровней имеют >= 2 экстремума.
    """
    if not extrema_values:
        return None, None, 0
    from collections import Counter
    levels = [round(v, decimals) for v in extrema_values]
    counter = Counter(levels)

    # Уровни с >= 2 экстремумами
    repeat_levels = [lvl for lvl, cnt in counter.items() if cnt >= 2]

    if not repeat_levels:
        return None, None, 0

    repeat_max = max(repeat_levels)
    repeat_min = min(repeat_levels)

    return repeat_max, repeat_min, len(repeat_levels)


def get_last_repeat_date(extrema_values, extrema_dates, decimals=2):
    """Дата последнего экстремума, который имеет повтор на своём уровне (>= 2).

    Возвращает: строка DD-MM-YYYY или None.
    """
    if not extrema_values or not extrema_dates:
        return None
    from collections import Counter
    levels = [round(v, decimals) for v in extrema_values]
    counter = Counter(levels)

    last_repeat_date = None
    for i, lvl in enumerate(levels):
        if counter[lvl] >= 2:
            if i < len(extrema_dates):
                last_repeat_date = extrema_dates[i]
    if last_repeat_date is None:
        return None
    return last_repeat_date.strftime('%d-%m-%Y')


def count_mean_crossings(z):
    """Сколько раз Z пересекает среднюю."""
    mean = z.mean()
    diff = z - mean
    signs = np.sign(diff)
    signs[signs == 0] = 1
    changes = (signs.diff() != 0).sum()
    return int(changes)


def compute_r2_trend(z):
    """R² линейной регрессии."""
    if len(z) < 3:
        return 1.0
    x = np.arange(len(z))
    y = z.values
    # Линейная регрессия
    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = ((y - y_pred) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    if ss_tot == 0:
        return 1.0
    return float(1 - ss_res / ss_tot)


def compute_trend_ratio(z):
    """|Z_end - Z_start| / (max - min)."""
    zmax = z.max()
    zmin = z.min()
    if zmax == zmin:
        return 1.0
    return abs(z.iloc[-1] - z.iloc[0]) / (zmax - zmin)


def plot_top5(df_top, period_name, prices):
    """График для ТОП-5 пар."""
    fig, axes = plt.subplots(5, 1, figsize=(16, 20))

    for idx, (_, row) in enumerate(df_top.head(5).iterrows()):
        pair = row['Пара']
        ta, tb = pair.split('/')

        if isinstance(PERIODS[period_name][0], int):
            cutoff = datetime.today() - timedelta(days=PERIODS[period_name][0])
            window = prices[prices.index >= cutoff]
        else:
            window = prices[prices.index >= '2022-06-01']

        sa = window[ta]
        sb = window[tb]
        common = sa.index.intersection(sb.index)
        sa = sa.loc[common]
        sb = sb.loc[common]
        z = sa / sb

        ax = axes[idx]
        ax.plot(z.index, z.values, linewidth=1.0, color='steelblue')
        ax.set_title('{}. {}  (экстр: {}, ср: {:.2f}%, min: {:.2f}%, max: {:.2f}%, R²: {:.3f})'.format(
            idx + 1, pair, row['Экстремумы'], row['Среднее движение %'],
            row['Мин движение %'], row['Макс движение %'], row['R²']),
            fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))

    plt.tight_layout()
    out = os.path.join(PLOT_DIR, 'top5_{}.png'.format(period_name.replace(' ', '_')))
    plt.savefig(out, dpi=100)
    plt.close()
    return out


def main():
    today = datetime.today()
    print('=' * 80)
    print('АНАЛИТИКА Z (синусоидальность пар)')
    print('=' * 80)

    prices = load_prices()
    if prices.empty:
        print('Нет данных.')
        return

    all_rows = []
    all_rows_10 = []

    for period_name, (period_value, window) in PERIODS.items():
        if isinstance(period_value, int):
            cutoff = today - timedelta(days=period_value)
            w = prices[prices.index >= cutoff]
        else:
            w = prices[prices.index >= '2022-06-01']

        if len(w) < 50:
            print('Период {}: мало данных'.format(period_name))
            continue

        tickers = list(w.columns)
        rows5 = []    # ZigZag 5%
        rows10 = []   # ZigZag 10%

        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                ta, tb = tickers[i], tickers[j]
                pair_name = ta + '/' + tb

                # Обрезка по сплитам
                pair_start = w.index.min()
                for t in [ta, tb]:
                    if t in KNOWN_SPLITS:
                        adjusted = add_trading_days(w.index, KNOWN_SPLITS[t][0], 5)
                        if adjusted > pair_start:
                            pair_start = adjusted

                sa = w[ta].loc[w.index >= pair_start]
                sb = w[tb].loc[w.index >= pair_start]
                common = sa.index.intersection(sb.index)
                if len(common) < 50:
                    continue
                sa = sa.loc[common]
                sb = sb.loc[common]
                z = sa / sb

                # ---- ZigZag 5% ----
                n5, avg5, min5, max5, vals5, dates5 = count_extrema(z, window, threshold=0.05)
                rep_max5, rep_min5, n_rep5 = count_repeated_extrema(vals5, decimals=2)
                last_rep5 = get_last_repeat_date(vals5, dates5, decimals=2)

                # ---- ZigZag 10% ----
                n10, avg10, min10, max10, vals10, dates10 = count_extrema(z, window, threshold=0.10)
                rep_max10, rep_min10, n_rep10 = count_repeated_extrema(vals10, decimals=2)
                last_rep10 = get_last_repeat_date(vals10, dates10, decimals=2)

                # ---- Общие (не зависят от ZigZag) ----
                n_crossings = count_mean_crossings(z)
                r2 = compute_r2_trend(z)
                tr = compute_trend_ratio(z)

                base = {
                    'Период': period_name,
                    'Пара': pair_name,
                    'Окно': window,
                    'Дней': len(z),
                    'Мин Z': round(z.min(), 4),
                    'Макс Z': round(z.max(), 4),
                    'Z средняя': round(z.mean(), 4),
                    'Mean-crossings': n_crossings,
                    'R²': round(r2, 4),
                    'Trend ratio': round(tr, 4),
                    'Z start': round(z.iloc[0], 4),
                    'Z end': round(z.iloc[-1], 4),
                }

                # 5%
                rows5.append({**base,
                    'Экстремумы': n5,
                    'P max': rep_max5,
                    'P min': rep_min5,
                    'Шаг, %': round((rep_max5 - rep_min5) / rep_min5 * 100, 2) if (rep_max5 and rep_min5 and rep_min5 > 0) else None,
                    'Уровней с повтором': n_rep5,
                    'Последний повтор': last_rep5,
                    'Мин движение %': min5,
                    'Макс движение %': max5,
                    'Среднее движение %': avg5,
                })

                # 10%
                min_z_10 = round(min(vals10), 4) if vals10 else None
                max_z_10 = round(max(vals10), 4) if vals10 else None
                rows10.append({**base,
                    'Экстремумы': n10,
                    'min Z 10': min_z_10,
                    'max Z 10': max_z_10,
                    'P max': rep_max10,
                    'P min': rep_min10,
                    'Шаг, %': round((rep_max10 - rep_min10) / rep_min10 * 100, 2) if (rep_max10 and rep_min10 and rep_min10 > 0) else None,
                    'Уровней с повтором': n_rep10,
                    'Последний повтор': last_rep10,
                    'Мин движение %': min10,
                    'Макс движение %': max10,
                    'Среднее движение %': avg10,
                })

        df_period = pd.DataFrame(rows5)
        if not df_period.empty:
            df_period = df_period.sort_values('Экстремумы', ascending=False).reset_index(drop=True)

        df_period_10 = pd.DataFrame(rows10)
        if not df_period_10.empty:
            df_period_10 = df_period_10.sort_values('Экстремумы', ascending=False).reset_index(drop=True)

        all_rows.append(df_period)
        all_rows_10.append(df_period_10)

        print()
        print('=' * 100)
        print('ПЕРИОД: {}  (окно экстремумов = {}, всего пар: {})'.format(
            period_name, window, len(df_period)))
        print('=' * 100)
        if df_period.empty:
            print('  (пусто)')
            continue

        cols = ['Пара', 'Экстремумы', 'Мин Z', 'Макс Z', 'Z средняя', 'Мин движение %', 'Макс движение %', 'Среднее движение %', 'Mean-crossings', 'R²', 'Trend ratio', 'Дней']
        print(df_period[cols].head(20).to_string(index=False))

        # Графики ТОП-5
        if not df_period.empty:
            plot_file = plot_top5(df_period, period_name, prices)
            print('  График ТОП-5: {}'.format(plot_file))

    # Объединённые таблицы
    result = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    result_10 = pd.concat(all_rows_10, ignore_index=True) if all_rows_10 else pd.DataFrame()

    # Запись в Excel
    if not result.empty:
        print()
        print('Запись в {}...'.format(OUTPUT_FILE))

        # Колонки для листа 'Повторы'
        REPEAT_COLS = ['Период', 'Пара', 'Экстремумы',
                       'P max', 'P min', 'Шаг, %',
                       'Уровней с повтором', 'Последний повтор',
                       'Мин Z', 'Макс Z', 'Z средняя',
                       'R²', 'Trend ratio']

        repeats_df = result[[c for c in REPEAT_COLS if c in result.columns]].copy()

        repeats_df_10 = result_10[[c for c in REPEAT_COLS if c in result_10.columns]].copy() if not result_10.empty else pd.DataFrame()

        with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as w:
            result.to_excel(w, sheet_name='Analytics_Z', index=False)
            result_10.to_excel(w, sheet_name='Analytics_Z_10', index=False)
            repeats_df.to_excel(w, sheet_name='Повторы', index=False)
            repeats_df_10.to_excel(w, sheet_name='Повторы_10', index=False)
        print('OK: {} (Analytics_Z: {} строк, Analytics_Z_10: {} строк, Повторы: {}, Повторы_10: {})'.format(
            OUTPUT_FILE, len(result), len(result_10), len(repeats_df), len(repeats_df_10)))

    print()
    print('Готово. Графики в папке {}'.format(PLOT_DIR))


if __name__ == '__main__':
    main()

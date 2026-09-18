# -*- coding: utf-8 -*-
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

CACHE_DIR = '/Users/Gadotkach/pairs_project/cache'
TEST_START = '2022-06-01'
ta, tb = 'SBER', 'SNGSP'

def load_prices():
    series = {}
    for t in [ta, tb]:
        path = os.path.join(CACHE_DIR, t + '.csv')
        if not os.path.exists(path):
            print('Net kesha: ' + t)
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

def main():
    prices = load_prices()
    if prices.empty:
        print('Net dannyh.')
        return

    window = prices[prices.index >= TEST_START]
    sa = window[ta]
    sb = window[tb]
    common = sa.index.intersection(sb.index)
    sa = sa.loc[common]
    sb = sb.loc[common]
    z = sa / sb

    print('Period: {} -> {} ({} dney)'.format(
        z.index.min().date(), z.index.max().date(), len(z)))
    print('Z: min={:.4f}, max={:.4f}, mean={:.4f}'.format(
        z.min(), z.max(), z.mean()))
    print('Start Z = {:.4f}'.format(z.iloc[0]))

    fig, ax = plt.subplots(figsize=(16, 8))
    ax.plot(z.index, z.values, linewidth=1.2, color='steelblue',
            label='Z = {}/{}'.format(ta, tb))

    z_start = z.iloc[0]
    ax.axhline(y=z_start, color='gray', linestyle='--', linewidth=0.8,
               label='Z_start = {:.4f}'.format(z_start))
    ax.axhline(y=z_start * 1.05, color='green', linestyle=':', linewidth=0.8,
               label='+5% = {:.4f}'.format(z_start * 1.05))
    ax.axhline(y=z_start * 0.95, color='red', linestyle=':', linewidth=0.8,
               label='-5% = {:.4f}'.format(z_start * 0.95))

    ax.set_title('Z = {}/{}  ({} -> {})'.format(
        ta, tb, z.index.min().date(), z.index.max().date()), fontsize=14)
    ax.set_xlabel('Data')
    ax.set_ylabel('Z')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=10)

    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.xticks(rotation=45)

    plt.tight_layout()
    out = '/Users/Gadotkach/pairs_project/sber_sngsp_z.png'
    plt.savefig(out, dpi=120)
    print('OK: ' + out)

if __name__ == '__main__':
    main()

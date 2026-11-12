# -*- coding: utf-8 -*-
"""
huijin_tracker.py
汇金"国家队"宽基 ETF 池: 每日份额变化 + 加仓指数 单文件看板

复刻自外部"汪汪队ETF跟踪器"页面的两块图表, 数据源换成本仓库已抓好的
沪深交易所官方日度份额(etf_flow.parquet, 由 etf_broadbase_dashboard.py 生成),
不重复抓取; parquet 不存在时先跑 etf_broadbase_dashboard.py。

口径(与参考页面对齐处均注明):
  - 池 = 2025 年报中央汇金前十大持有人口径的 20 只宽基 ETF(Wind 统计, 2026-03 披露。
    此后信披新规不再披露前十大持有人, 名单为"绝版"历史标签, 不代表当前持仓)。
  - 每日份额变化 = 池内 20 只当日场内份额合计 - 上一交易日合计(亿份)。
    缺数日按前值顺延(与参考页面一致); 份额折算经"规模守恒"(份额×净值不变)验证后才回溯复权,
    未通过验证的按真实申赎保留原值——参考页面 2025-11 起算避开折算日, 本仓库 2021 年起算必须处理。
  - 加仓指数 = 100 × 当日池内份额合计 ÷ 基期(2025-11-03)合计, 与参考页面基期对齐;
    >100 净加仓, <100 净减仓。基期早于它的历史也按同一基期展示, 只是更长。
  - 全部份额变化都记为"国家队"操作是参考口径, 实际可能来自任何投资者。
  - "今日结论"判定照搬参考页面: 放量倍数(当日合计成交额/20日均额, akshare EOD)与
    当日主动净买入(腾讯行情外盘−内盘推算)两条件定级; 收盘后口径, 无盘中同时点对齐;
    另附本仓库独有的交易所官方净申赎作印证。成交额按代码增量缓存在 cache_huijin/。
"""
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from plotly.subplots import make_subplots

HERE = Path(__file__).resolve().parent
PARQUET = HERE / "etf_flow.parquet"
OUT_HTML = HERE / "huijin_tracker.html"
AMT_CACHE = HERE / "cache_huijin"
LIVE_URL = "https://qt.gtimg.cn/q={codes}"

BASE_DATE = "2025-11-03"          # 加仓指数基期, 与参考页面一致
PCT_WINDOW = 120                  # 滚动分位窗口, 与 etf_broadbase_dashboard 口径一致
EMBED_PLOTLY_JS = False           # CDN 引用, 与仓库近期脚本一致, 控制仓库体积
PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"
UP, DOWN = "#d62728", "#2a9d3a"   # A股习惯: 红=净申购, 绿=净赎回
INDEX_COLOR = "#005eb8"

# (代码, 名称, 汇金2025年报持仓亿份) — Wind 2025 年报前十大持有人口径, 2026-03 披露,
# 信披新规后不再更新, 名单冻结。
CORE = [
    ("510300", "沪深300ETF华泰柏瑞", 735.13),
    ("510310", "沪深300ETF易方达", 558.94),
    ("510050", "上证50ETF华夏", 487.59),
    ("510330", "沪深300ETF华夏", 420.28),
    ("159919", "沪深300ETF嘉实", 354.98),
    ("512100", "中证1000ETF南方", 221.69),
    ("588080", "科创50ETF易方达", 216.07),
    ("159915", "创业板ETF易方达", 170.2),
    ("510500", "中证500ETF南方", 142.17),
    ("159845", "中证1000ETF华夏", 137.26),
    ("560010", "中证1000ETF广发", 105.5),
    ("510180", "上证180ETF华安", 52.32),
    ("159629", "中证1000ETF富国", 36.34),
    ("510230", "金融ETF国泰", 25.64),
    ("159922", "中证500ETF嘉实", 23.11),
    ("512500", "中证500ETF华夏", 20.51),
    ("515800", "中证800ETF汇添富", 13.08),
    ("159952", "创业板ETF广发", 10.91),
    ("588050", "科创50ETF工银", 9.7),
    ("159901", "深证100ETF易方达", 7.19),
]


def _flag_to_bool(s: pd.Series) -> pd.Series:
    """疑似折算列在 parquet 里是 object 混合类型(bool/字符串/NaN), 统一转 bool。"""
    return s.map(lambda v: bool(v) if isinstance(v, (bool, np.bool_))
                 else str(v).strip().lower() == "true")


def back_adjust(sh: pd.Series, split_days: list) -> pd.Series:
    """份额折算历史回溯复权: 折算日之前的历史份额 × (折算后/折算前)。
    多个折算日从晚到早处理: 早期折算的比例分子分母都已被晚期折算等比缩放, 计算不受影响。"""
    adj: pd.Series = sh.copy()
    for t in sorted(split_days, reverse=True):
        hist = adj[adj.index < t]
        hist = hist[hist.notna()]
        cur = adj.loc[t]
        if hist.empty or pd.isna(cur):
            print(f"  [WARN] 折算日 {t:%Y-%m-%d} 前后份额缺失, 未复权")
            continue
        r = cur / hist.iloc[-1]
        if pd.isna(r) or r <= 0:
            continue
        adj.loc[adj.index < t] = adj.loc[adj.index < t] * r
    return adj


def build_pool(df: pd.DataFrame):
    """返回 (池内合计份额序列[亿份], 每只复权后份额[亿份], 缺失代码)。"""
    codes = [c for c, _, _ in CORE]
    name_of = {c: n for c, n, _ in CORE}
    have = [c for c in codes if c in set(df["基金代码"])]
    missing = [f"{c}({name_of[c]})" for c in codes if c not in have]
    if not have:
        sys.exit("etf_flow.parquet 中没有任何汇金池成员, 请先运行 etf_broadbase_dashboard.py")
    if missing:
        print(f"  [WARN] {len(missing)} 只池成员不在 parquet 中, 已剔除: {', '.join(missing)}")

    d = df[df["基金代码"].isin(have)]
    # pivot 保留 NaN(缺失日), 不用 pivot_table+sum —— 后者会把全 NaN 组求和成 0,
    # 把"上交所当日没取到数据"变成"份额清零"的假赎回
    shares = d.pivot(index="日期", columns="基金代码", values="基金份额").sort_index()
    flags = d.pivot(index="日期", columns="基金代码", values="疑似折算").sort_index()
    navs = d.pivot(index="日期", columns="基金代码", values="单位净值").sort_index()

    per = {}
    for c in have:
        sh = shares[c]
        fb = _flag_to_bool(flags[c]).fillna(False).astype(bool)
        # 折算标记三分类: 真折算(份额变+规模守恒)->回溯复权;
        # 其余(真实巨额申赎/缺数)->保留原值, 缺数靠 ffill 顺延
        true_splits = []
        for t in sh.index[fb.to_numpy()]:
            if pd.isna(sh.loc[t]):
                continue
            hist = sh[sh.index < t]
            hist = hist[hist.notna()]
            if hist.empty:
                continue
            p, prev = hist.index[-1], hist.iloc[-1]
            r = sh.loc[t] / prev
            if abs(r - 1) < 0.3:      # 份额变动不足三成, 谈不上折算
                continue
            nv_t, nv_p = navs[c].loc[t], navs[c].loc[p]
            if pd.isna(nv_t) or pd.isna(nv_p) or nv_p <= 0:
                continue               # 无净值可验证时不复权, 宁可漏不可错
            if abs(sh.loc[t] * nv_t / (prev * nv_p) - 1) <= 0.15:
                true_splits.append(t)
            else:
                print(f"  [INFO] {c} {t:%Y-%m-%d} 单日份额 {r-1:+.0%} 但规模"
                      f" {sh.loc[t]*nv_t/(prev*nv_p)-1:+.0%}, 判为真实申赎保留")
        per[c] = back_adjust(sh, true_splits)   # 暂不 ffill(单位: 份), 先做末端完整性检查

    raw = pd.DataFrame(per)
    # 末端完整性: 沪市份额 T+1 上午才发布, 当晚可能只有部分市场有最新日数据。
    # 最新日的当日披露数 < 近 20 日常态的 80% 时丢弃该日, 避免把"部分市场缺席"
    # 误读成申赎(与主看板对半截合计置空同理); 下一交易日补全后自然恢复。
    if len(raw) > 21:
        cov = raw.notna().sum(axis=1)
        norm = cov.iloc[-21:-1].median()
        if norm > 0 and cov.iloc[-1] < 0.8 * norm:
            print(f"  [INFO] 末端 {raw.index[-1]:%Y-%m-%d} 当日仅 {int(cov.iloc[-1])}/{int(norm)} 只有份额(沪深披露时差), 该日不计入, 待补全")
            raw = raw.iloc[:-1]
    per = {c: raw[c].ffill() / 1e8 for c in raw.columns}

    pool = pd.DataFrame(per).sum(axis=1, min_count=1)
    # 自检: 单日变化超过池内 12% 基本是漏掉的折算/数据错误(历史最大真实申购约 7%)
    jump = pool.diff()[pool.diff().abs() > 0.12 * pool.abs()]
    for t, v in jump.items():
        print(f"  [WARN] {t:%Y-%m-%d} 单日变化 {v:+.1f} 亿份 (>池内12%), 疑似未复权折算或数据异常")
    return pool, per, missing


def make_figure(pool: pd.Series, base_date):
    """加仓指数随所选时间范围以"窗口首日=100"重定基(与参考页面的交互一致);
    近1年/全部视图固定基期 2025-11-03, 便于跨日比较。"""
    dates = pool.index
    delta = pool.diff().values
    fixed = (100 * pool / pool.loc[base_date]).values

    def rebased(n_back):
        # 参考页面同款: 近3月/近6月窗口, 曲线以窗口首日=100 重定基
        n_back = min(n_back, len(pool) - 1)
        seg = pool.iloc[len(pool) - n_back - 1:]
        out = np.full(len(pool), np.nan)
        out[-len(seg):] = (100 * seg / seg.iloc[0]).values
        return out

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=("池内每日份额变化(亿份) 正=净申购 负=净赎回",
                                        "加仓指数 · 近3月/近6月以窗口首日=100 重定基 · 近1年/全部固定基期 2025-11-03"))
    fig.add_bar(x=dates, y=delta, name="份额变化",
                marker_color=np.where(delta >= 0, UP, DOWN),
                hovertemplate="%{y:+.2f} 亿份<extra>当日份额变化</extra>", row=1, col=1)
    for y, name, vis in ((rebased(60), "加仓指数(近3月首日=100)", False),
                         (rebased(120), "加仓指数(近6月首日=100)", False),
                         (fixed, "加仓指数(基期 2025-11-03=100)", True)):
        fig.add_scatter(x=dates, y=y, name=name, mode="lines", visible=vis,
                        line=dict(color=INDEX_COLOR, width=2),
                        hovertemplate="%{y:.2f}<extra>%{fullData.name}</extra>", row=2, col=1)
    fig.add_hline(y=100, line_dash="dash", line_color="#999", row=2, col=1)
    fig.add_vline(x=base_date, line_dash="dot", line_color="#b8860b",
                  annotation_text="基期", annotation_font_size=11)

    def rng(a, b):
        return {"xaxis.range": [a, b], "xaxis2.range": [a, b]}

    # 时间范围切换用 updatemenu: 同步缩放 + 切到对应重定基曲线(与参考页面行为一致)。
    # rangeselector 只支持按日期缩放, 无 method 属性, 无法联动切曲线, 故弃用。
    menu = dict(type="buttons", direction="right",
                x=1.0, xanchor="right", y=1.18, yanchor="top",
                buttons=[
                    dict(label="近3月", method="update",
                         args=[{"visible": [True, True, False, False]}, rng(dates[-61], dates[-1])]),
                    dict(label="近6月", method="update",
                         args=[{"visible": [True, False, True, False]}, rng(dates[-121], dates[-1])]),
                    dict(label="近1年", method="update",
                         args=[{"visible": [True, False, False, True]}, rng(base_date, dates[-1])]),
                    dict(label="全部", method="update",
                         args=[{"visible": [True, False, False, True]}, rng(dates[0], dates[-1])]),
                ])
    fig.update_layout(
        hovermode="x unified", template="plotly_white", height=760,
        margin=dict(l=50, r=30, t=60, b=40),
        font=dict(family="Microsoft YaHei, PingFang SC, sans-serif", size=12),
        legend=dict(orientation="h", y=1.05, x=0),
        updatemenus=[menu])
    fig.update_xaxes(range=[base_date, dates[-1]])   # 默认近1年, 与参考页面一致
    fig.update_yaxes(tickformat=",.0f", row=1, col=1)
    return fig


# ------------------------------------------------------------ 今日结论(照搬参考页面判定)
def _numfmt(v, f="{:+.1f}"):
    return f.format(v) if (v is not None and v == v) else "—"


def _tcode(code: str) -> str:
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def fetch_amounts(codes, trade_days) -> dict:
    """近 45 个交易日成交额(元)。akshare EOD, 按代码增量缓存; 缓存齐全时不联网。"""
    hi, lo = trade_days[-1], trade_days[-45]
    AMT_CACHE.mkdir(exist_ok=True)
    out, todo = {}, []
    for code in codes:
        f = AMT_CACHE / f"amt_{code}.parquet"
        df = pd.read_parquet(f) if f.exists() else pd.DataFrame()
        have_max = pd.to_datetime(df["日期"]).max() if len(df) else None
        if have_max is not None and have_max >= hi:
            out[code] = df
        else:
            todo.append((code, f, df, have_max))
    if not todo:
        return out
    import akshare as ak  # 延迟导入: 缓存齐全时全程离线
    for code, f, df, have_max in todo:
        start = lo if have_max is None else min(have_max - pd.Timedelta(days=15), lo)
        try:
            raw = ak.fund_etf_hist_em(symbol=code, period="daily",
                                     start_date=start.strftime("%Y%m%d"),
                                     end_date=hi.strftime("%Y%m%d"), adjust="")
            new = pd.DataFrame({"日期": pd.to_datetime(raw["日期"]),
                                "成交额": pd.to_numeric(raw["成交额"], errors="coerce")}).dropna()
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] {code} 成交额抓取失败: {exc}")
            new = pd.DataFrame(columns=["日期", "成交额"])
        if len(new):
            df = pd.concat([df, new]).drop_duplicates("日期", keep="last").sort_values("日期")
            df.to_parquet(f, index=False)
            time.sleep(0.3)
        if len(df):
            out[code] = df
    return out


def fetch_live(codes) -> dict:
    """腾讯行情快照(参考页面同款接口/字段): 外盘−内盘 -> 当日主动净买入。"""
    try:
        sess = requests.Session()
        sess.trust_env = False   # 国内源直连, 与 futures 脚本同理, 不走本地代理
        r = sess.get(LIVE_URL.format(codes=",".join(_tcode(c) for c in codes)),
                     timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        body = r.content.decode("gbk", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] 腾讯行情不可用, 当日主动净买入缺失: {exc}")
        return {}
    out = {}
    for m in re.finditer(r'v_\w{2}\d+="([^"]*)"', body):
        a = m.group(1).split("~")
        if len(a) < 40:
            continue
        def num(i, a=a):
            try:
                return float(a[i] or 0)
            except ValueError:
                return 0.0
        oi = num(7) + num(8)
        out[a[2]] = {"net": (num(7) - num(8)) / oi * num(37) / 1e4 if oi > 0 else 0.0,
                     "pct": num(32), "date": (a[30] or "")[:8]}
    return out


def build_verdict(df, pool, per, amounts, live) -> dict:
    """参考页面"今日结论"的收盘后口径: 放量倍数 + 主动净买入, 附交易所官方净申赎。"""
    codes = list(per.columns)
    last = pool.index[-1]
    # ① 当日合计成交额 / 20 日均额(不足 20 日不出判定); 判定日取份额与成交额孰新
    # (沪市份额 T+1 上午发布, 盘后当天主看板未重跑时, 成交额/行情已最新而份额仍缺)
    sum_amt = sum_base = ratio = np.nan
    if amounts:
        amt = pd.DataFrame({c: amounts[c].set_index("日期")["成交额"] / 1e8
                            for c in amounts}).sort_index()
        last = max(last, amt.index[-1])
        days = amt.index[amt.index <= last]
        if len(days):
            sum_amt = float(amt.loc[days[-1]].sum())
            if len(days[-21:-1]) >= 20:
                sum_base = float(amt.loc[days[-21:-1]].mean().sum())
    if not (np.isnan(sum_amt) or np.isnan(sum_base) or sum_base <= 0):
        ratio = sum_amt / sum_base
    # ② 合计主动净买入(腾讯快照; 不可用或与判定日不同日 -> None, 判定降级为"方向未知")
    live_day = max((v["date"] for v in live.values() if v.get("date")), default="") if live else ""
    sum_net = sum(v["net"] for v in live.values()) \
        if (live and live_day == last.strftime("%Y%m%d")) else None
    # ③ 池内规模加权涨跌(官方份额 × 收盘价)
    d = df[df["基金代码"].isin(codes)]
    close = d.pivot(index="日期", columns="基金代码", values="收盘价").sort_index()
    if last in close.index and close.index.get_loc(last) > 0:
        prev = close.index[close.index.get_loc(last) - 1]
        pct = (close.loc[last] - close.loc[prev]) / close.loc[prev]
        w = close.loc[last] * per.iloc[-1]
        ok = pct.notna() & w.notna() & (w > 0)
        wpct = float((pct[ok] * w[ok]).sum() / w[ok].sum() * 100) if ok.any() else np.nan
    else:
        wpct = np.nan
        if live and live_day == last.strftime("%Y%m%d"):
            # parquet 尚无判定日价格(主看板未重跑) -> 腾讯快照涨跌 × 前收市值权重兑底
            w = close.ffill().iloc[-1] * per.iloc[-1]
            ok = w.notna() & (w > 0)
            if ok.any():
                wpct = float(sum(live[c]["pct"] * w[c] for c in w.index[ok] if c in live)
                             / w[ok].sum())
    # ④ 当日净申赎(交易所官方, 本仓库独有); 沪市 T+1 上午发布, 判定日无官方份额 -> NaN
    net_flow = float(pool.diff().iloc[-1]) if pool.index[-1] == last else np.nan

    # ---- 定级(阈值与文案照搬参考页面)
    if np.isnan(ratio):
        level, title = "cool", "成交额数据不足"
        reason = "成交额历史不足 20 个交易日，判定需再积累几天缓存。"
    elif sum_net is None:
        if ratio >= 1.8:
            level, title = "mid", "放量了，方向未知"
            reason = f"池内成交额达到 20 日均额的 {ratio:.2f} 倍，但腾讯行情通道不可用，主动买卖方向未知。"
        elif ratio >= 1.3:
            level, title = "mid", "成交活跃，方向未知"
            reason = f"池内成交额达到 20 日均额的 {ratio:.2f} 倍，腾讯行情通道不可用，方向未知。"
        else:
            level, title = "cool", "没看出上班迹象"
            reason = f"池内成交额只有 20 日均额的 {ratio:.2f} 倍，量价都平静。"
    elif ratio >= 1.8 and sum_net > 0:
        level, title = "hot", "大概率上班了"
        reason = (f"汪汪队池成交额达到 20 日均额的 {ratio:.2f} 倍，且主动净买入 "
                  f"{sum_net:+.1f} 亿元。放量 + 净买入同向，是典型的护盘量价特征。")
    elif ratio >= 1.3 and sum_net > 0:
        level, title = "mid", "有护盘迹象"
        reason = (f"成交额放大到 20 日均额的 {ratio:.2f} 倍，主动净买入 {sum_net:+.1f} 亿元，"
                  f"方向偏买，但力度还不构成强信号。")
    elif ratio >= 1.8 and sum_net <= 0:
        level, title = "mid", "放量了，方向不对"
        reason = (f"成交额放大到 20 日均额的 {ratio:.2f} 倍，但主动净买入 {sum_net:+.1f} 亿元。"
                  f"大额成交未必是买盘，更像换手或出货，不能直接算上班。")
    elif ratio >= 1.3:
        level, title = "mid", "成交活跃，证据不够"
        reason = f"成交额达到 20 日均额的 {ratio:.2f} 倍，主动净买入 {sum_net:+.1f} 亿元，方向不清晰。"
    else:
        level, title = "cool", "没看出上班迹象"
        reason = (f"成交额只有 20 日均额的 {ratio:.2f} 倍，主动净买入 {sum_net:+.1f} 亿元，量价都平静。")
    if level in ("hot", "mid"):
        if not np.isnan(wpct) and wpct < -0.5:
            reason += f" 池内规模加权涨跌 {wpct:+.2f}%，下跌日放量更符合护盘场景。"
        if net_flow == net_flow:
            if net_flow > 0:
                reason += f" 当日交易所口径净申购 {net_flow:+.1f} 亿份，量价与份额同向。"
            elif net_flow < 0:
                reason += f" 当日交易所口径净赎回 {net_flow:+.1f} 亿份，量价信号未获份额数据印证。"
    if net_flow != net_flow and pool.index[-1] != last:
        reason += f" 判定日({last:%Y-%m-%d})交易所份额尚未发布（沪市 T+1 上午更新），净申赎待下次刷新。"
    return dict(date=last, level=level, title=title, reason=reason, sum_amt=sum_amt,
                sum_base=sum_base, ratio=ratio, sum_net=sum_net, wpct=wpct, net_flow=net_flow)


def member_table(per: pd.DataFrame, delta20: pd.Series, last) -> str:
    rows = []
    for code, name, huijin in sorted(CORE, key=lambda x: -x[2]):
        if code not in per:
            continue
        sh, d20 = per[code].iloc[-1], delta20.get(code, np.nan)
        d20_txt = "—" if pd.isna(d20) else f"{d20:+.1f}"
        color = DOWN if (pd.isna(d20) or d20 < 0) else UP
        rows.append(f"<tr><td>{code}</td><td>{name}</td><td class='num'>{huijin:.1f}</td>"
                    f"<td class='num'>{sh:.1f}</td>"
                    f"<td class='num' style='color:{color}'>{d20_txt}</td></tr>")
    return ("<table><thead><tr><th>代码</th><th>名称</th><th>汇金25年报(亿份)</th>"
            "<th>最新份额(亿份)</th><th>近20日变化(亿份)</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def build_html(fig, pool, per, base_date, missing, v, path: Path):
    delta = pool.diff()
    last, pool_now = pool.index[-1], pool.iloc[-1]
    d_last, idx_now = delta.iloc[-1], 100 * pool.iloc[-1] / pool.loc[base_date]
    r20 = delta.tail(20).sum()
    fig_html = fig.to_html(full_html=False, include_plotlyjs=EMBED_PLOTLY_JS,
                           config={"displaylogo": False})
    tag = "" if EMBED_PLOTLY_JS else f'<script src="{PLOTLY_CDN}"></script>'
    css = """
    body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;margin:0;background:#fafaf7;color:#222}
    main{max-width:1180px;margin:0 auto;padding:24px 20px 48px}
    h1{font-size:22px;font-weight:600;margin:0 0 4px}
    .sub{color:#777;font-size:13px;margin-bottom:20px}
    .stats{display:flex;gap:28px;flex-wrap:wrap;margin-bottom:20px}
    .stats b{display:block;font-size:20px;font-weight:800}
    .stats span{font-size:11px;color:#777}
    table{border-collapse:collapse;font-size:13px;background:#fff;margin:14px 0 6px}
    th,td{padding:4px 10px;text-align:right;border-bottom:1px solid #eee;white-space:nowrap}
    th{background:#f1efe8;font-weight:600}
    td:nth-child(1),td:nth-child(2){text-align:left;color:#555}
    .note{font-size:12px;color:#666;line-height:1.8;background:#fff;border-left:3px solid #b8860b;padding:10px 16px}
    .chart{margin-top:10px;background:#fff}
    .verdict{background:#fff;border:1px solid #ececec;border-left:4px solid #ccc;border-radius:12px;padding:18px 22px 14px;margin-bottom:18px}
    .verdict.hot{border-left-color:#ff4713}
    .verdict.mid{border-left-color:#e8a33c}
    .vlabel{font-size:11px;color:#8a8a8a;letter-spacing:1.5px;font-weight:700}
    .vtitle{font-size:26px;font-weight:800;letter-spacing:-.5px;margin:8px 0 6px}
    .vreason{font-size:13px;color:#444;line-height:1.7;max-width:900px}
    .vgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:14px}
    .vgrid>div{background:#fafafa;border-radius:8px;padding:12px 14px}
    .vgrid .k{font-size:11px;color:#8a8a8a;margin-bottom:4px}
    .vgrid .v{font-size:21px;font-weight:800}
    .vgrid .n{font-size:11px;color:#8a8a8a;margin-top:3px}
    .bar{height:5px;background:#ececec;border-radius:3px;margin-top:8px;overflow:hidden}
    .bar span{display:block;height:100%;background:#ff4713;border-radius:3px}
    .vcaveat{margin-top:12px;padding-top:8px;border-top:1px solid #f5f5f5;font-size:11px;color:#8a8a8a;line-height:1.7}
    """
    miss_html = (f"，另有 {len(missing)} 只不在本地 ETF 池中已剔除" if missing else "")
    bar_w = min(100, v["ratio"] / 2.5 * 100) if v["ratio"] == v["ratio"] else 0
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>汇金池 ETF 份额跟踪</title><style>{css}</style>{tag}</head><body><main>
<h1>汇金池宽基 ETF · 每日份额变化与加仓指数</h1>
<div class="sub">池 = 2025 年报汇金前十大持有人 20 只宽基 ETF（名单冻结）· 数据：沪深交易所官方日度份额
· 更新至 {last:%Y-%m-%d}{miss_html}</div>
<section class="verdict {v['level']}">
<div class="vlabel">今日结论 · {v['date']:%Y-%m-%d} 收盘复盘</div>
<div class="vtitle">{v['title']}</div>
<div class="vreason">{v['reason']}</div>
<div class="vgrid">
  <div><div class="k">汪汪队合计成交额</div><div class="v">{_numfmt(v['sum_amt'], "{:,.1f}")} 亿</div>
  <div class="n">20日均额 {_numfmt(v['sum_base'], "{:,.1f}")} 亿</div></div>
  <div><div class="k">成交额放大倍数</div>
  <div class="v" style="color:{'#ff4713' if v['ratio'] == v['ratio'] and v['ratio'] >= 1.8 else '#222'}">{_numfmt(v['ratio'], "{:.2f}")}×</div>
  <div class="n">阈值 1.8× 为强信号</div>
  <div class="bar"><span style="width:{bar_w:.0f}%"></span></div></div>
  <div><div class="k">合计主动净买入</div>
  <div class="v" style="color:{'#222' if v['sum_net'] is None else (UP if v['sum_net'] > 0 else DOWN)}">{_numfmt(v['sum_net'])} 亿</div>
  <div class="n">{"腾讯行情通道不可用" if v['sum_net'] is None else "外盘−内盘 逐笔方向推算"}</div></div>
  <div><div class="k">池内加权涨跌</div>
  <div class="v" style="color:{'#222' if v['wpct'] != v['wpct'] else (UP if v['wpct'] > 0 else DOWN)}">{_numfmt(v['wpct'], "{:+.2f}")}%</div>
  <div class="n">规模加权 · 官方份额×收盘价</div></div>
  <div><div class="k">当日净申赎</div>
  <div class="v" style="color:{'#222' if v['net_flow'] != v['net_flow'] else (UP if v['net_flow'] >= 0 else DOWN)}">{_numfmt(v['net_flow'])} 亿份</div>
  <div class="n">交易所官方份额口径</div></div>
</div>
<div class="vcaveat">判定为收盘后口径（无盘中同时点对齐）；「主动净买入」是外盘−内盘的逐笔方向推算，不是资金净流入，不含大宗与申赎套利对冲；全部结论均为量价推测，不能证明任何机构当日是否交易。不构成投资建议。</div>
</section>
<div class="stats">
  <div><b>{pool_now:,.0f}</b><span>池内合计份额(亿份)</span></div>
  <div><b style="color:{UP if d_last >= 0 else DOWN}">{d_last:+.1f}</b><span>当日份额变化(亿份)</span></div>
  <div><b style="color:{INDEX_COLOR}">{idx_now:.2f}</b><span>加仓指数(基期 2025-11-03=100)</span></div>
  <div><b style="color:{UP if r20 >= 0 else DOWN}">{r20:+,.0f}</b><span>近20日累计变化(亿份)</span></div>
</div>
{member_table(per, per.diff().tail(20).sum(), last)}
<div class="note">
口径与局限：① 名单为 2025 年报最后一次实名披露（信披新规后不再披露前十大持有人），只是"历史上买过"的静态标签；
② 全部日度份额变化均视为"国家队"操作是参考口径，实际可能来自任何投资者，不构成对任何机构当日交易的确认；
③ 份额拆分/折算日已按比例对历史回溯复权，缺数日按前值顺延；④ 与股指期货席位（futures/cffex_net_position.html）互为印证的角度是同一件事的另一面。不构成投资建议。
</div>
<div class="chart">{fig_html}</div></main></body></html>"""
    path.write_text(html, encoding="utf-8")


def main():
    if not PARQUET.exists():
        sys.exit(f"未找到 {PARQUET.name}, 先运行: python etf_broadbase_dashboard.py")
    print("读取", PARQUET.name, "...")
    df = pd.read_parquet(PARQUET)
    pool, per, missing = build_pool(df)
    per = pd.DataFrame(per)
    base_ts = pd.Timestamp(BASE_DATE)
    tail = pool.loc[base_ts:]
    base_date = tail.index[0] if len(tail) else pool.first_valid_index()
    if pool.loc[base_date] <= 0:
        sys.exit(f"基期 {base_date:%Y-%m-%d} 池内份额异常: {pool.loc[base_date]}")

    index = 100 * pool / pool.loc[base_date]
    print(f"池内 {per.shape[1]} 只, 序列 {pool.index[0]:%Y-%m-%d} ~ {pool.index[-1]:%Y-%m-%d}, "
          f"基期 {base_date:%Y-%m-%d}")
    print(f"当前: 合计 {pool.iloc[-1]:,.0f} 亿份, 加仓指数 {index.iloc[-1]:.2f}")

    hi = max(pool.index[-1], pd.Timestamp.now().normalize())
    amounts = fetch_amounts(list(per.columns), pool.index.union(pd.DatetimeIndex([hi])))
    live = fetch_live(list(per.columns))
    v = build_verdict(df, pool, per, amounts, live)
    print(f"今日结论({v['date']:%Y-%m-%d}): {v['title']}")
    print(f"  放大 {_numfmt(v['ratio'], '{:.2f}')}× | 主动净买入 {_numfmt(v['sum_net'])} 亿 | "
          f"加权涨跌 {_numfmt(v['wpct'], '{:+.2f}')}% | 净申赎 {_numfmt(v['net_flow'])} 亿份")

    fig = make_figure(pool, base_date)
    build_html(fig, pool, per, base_date, missing, v, OUT_HTML)
    print(f"已生成: {OUT_HTML}")


if __name__ == "__main__":
    main()

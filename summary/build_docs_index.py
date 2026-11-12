# -*- coding: utf-8 -*-
"""
build_docs_index.py
扫描 docs/*.html 生成分组导航首页 docs/index.html（GitHub Pages 用）。

主看板按固定清单分组、带中文名与说明；清单外的新看板自动落入「更多看板」，
以后新增脚本产物无需改本文件。由 daily_update.yml 在收集 HTML 之后调用。
"""
from datetime import datetime
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

# (文件名, 中文名, 一句话说明) —— 固定分组清单
SECTIONS = [
    ("① 宽基指数", [
        ("stock_8indicators_matrix.html", "八大指标矩阵", "16 个宽基指数 × MACD/RSI/KDJ/BOLL 等"),
        ("stock_zigzag_signal_analyzer.html", "ZigZag 波段看板", "波段辨识与信号"),
        ("stock_scorematrix.html", "风险评分矩阵", "过热评分六档色块"),
        ("index_return_calendar.html", "宽基收益日历", "12 宫格挂历 + 月度排名看板"),
    ]),
    ("② ETF 资金流", [
        ("etf_flow_dashboard.html", "45 只宽基 ETF 资金流看板", "份额 / 净申赎 / 滚动分位 · 观察护盘资金"),
        ("etf_net_flow_calendar.html", "ETF 净申赎金额日历", "合并板块日历视图"),
        ("huijin_tracker.html", "汇金池份额跟踪 · 加仓指数", "每日份额变化 + 今日结论（复刻“汪汪队跟踪器”）"),
    ]),
    ("③ 板块基金", [
        ("fund_indicators_matrix.html", "八大指标矩阵", ""),
        ("fund_score_matrix.html", "评分矩阵", ""),
        ("fund_riskrank_percentile.html", "风险排名与历史分位", ""),
        ("holdings_calendar.html", "板块基金收益日历", ""),
        ("fund_correlation_report.html", "相关性与聚类", "叠加 16 个宽基指数对照"),
        ("fund_anomaly_analysis.html", "收益率异常分析", "8 种判定方法"),
    ]),
    ("④ 回撤与信号回测", [
        ("drawdown_dashboard.html", "回撤 / 过热信号回测看板", "区间收益、最大回撤、信号后 5/20/60 日表现"),
    ]),
    ("⑤ 股指期货持仓", [
        ("cffex_net_position.html", "中信期货 / 前20会员净持仓", "与 ETF 资金流互为印证"),
    ]),
    ("⑥ 黄金", [
        ("gold_ma_slopes_interactive.html", "COMEX 黄金多周期均线与斜率", "四周期斜率分位"),
    ]),
]

CSS = """
    body{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#fafaf7;color:#222;max-width:960px;margin:0 auto;padding:24px 20px 48px}
    h1{font-size:22px;font-weight:600;margin:0 0 4px}
    .sub{color:#777;font-size:13px;margin-bottom:24px}
    h2{font-size:16px;font-weight:600;margin:28px 0 10px;padding-left:10px;border-left:4px solid #b8860b}
    a{color:#2c5f8a;text-decoration:none}
    a:hover{text-decoration:underline}
    ul{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px 24px}
    li{padding:5px 0;border-bottom:1px solid #eee;font-size:14px}
    li small{display:block;color:#999;font-size:12px}
    .note{margin-top:26px;font-size:12px;color:#888;line-height:1.8}
    @media (max-width:640px){ul{grid-template-columns:1fr}}
"""


def main():
    if not DOCS.exists():
        raise SystemExit(f"未找到 {DOCS}, 请在仓库根目录结构下运行")
    htmls = {p.name for p in DOCS.glob("*.html")} - {"index.html"}

    parts = []
    listed = set()
    for title, items in SECTIONS:
        rows = []
        for fname, name, desc in items:
            if fname in htmls:
                listed.add(fname)
                rows.append(f'<li><a href="{fname}">{name}</a>'
                            + (f"<small>{desc}</small>" if desc else "") + "</li>")
        if rows:
            parts.append(f"<h2>{title}</h2>\n<ul>\n" + "\n".join(rows) + "\n</ul>")

    others = sorted(htmls - listed)
    if others:
        rows = "\n".join(f'<li><a href="{f}">{f}</a></li>' for f in others)
        parts.append(f"<h2>更多看板（{len(others)} 个单页）</h2>\n<ul>\n{rows}\n</ul>")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📊 看板导航 · broad-index-dashboard</title><style>{CSS}</style></head><body>
<h1>📊 看板导航</h1>
<div class="sub">GitHub Pages 每晚自动更新 · 完整介绍与风险口径见
<a href="https://github.com/nayihz/broad-index-dashboard">仓库 README</a></div>
{chr(10).join(parts)}
<div class="note">共 {len(htmls)} 个看板 · 生成于 {datetime.now():%Y-%m-%d %H:%M} ·
清单外新增的看板会自动出现在「更多看板」。</div>
</body></html>
"""
    out = DOCS / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}（主看板 {len(listed)} 个 + 更多 {len(others)} 个）")


if __name__ == "__main__":
    main()

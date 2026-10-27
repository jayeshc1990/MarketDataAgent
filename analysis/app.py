"""MarketDataAgent analysis dashboard.

    streamlit run analysis/app.py            # data_dir from config.yaml
    MDA_DATA_DIR=/path/to/Data streamlit run analysis/app.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import auth  # noqa: E402
from analysis import data as D  # noqa: E402

st.set_page_config(page_title=auth.PUBLIC_NAME, layout="centered")    # neutral name until logged in
auth.require_login()     # nothing below runs (no data is read) until the user has logged in
st.set_page_config(page_title="NIFTY Options Analysis", layout="wide")

CE_COLOR, PE_COLOR, SPOT_COLOR = "#2a9d8f", "#e76f51", "#264653"
BUILDUP_COLORS = {"Long build-up": "#2a9d8f", "Short covering": "#8ecae6",
                  "Short build-up": "#e76f51", "Long unwinding": "#f4a261"}
REFRESH_SECONDS = 30   # how often the page checks the CSVs for new bars
# hide nights and weekends on time axes
RANGEBREAKS = [dict(bounds=["sat", "mon"]), dict(bounds=[15.5, 9.25], pattern="hour")]


def time_axes(fig):
    fig.update_xaxes(rangebreaks=RANGEBREAKS)
    return fig


@st.cache_data(show_spinner="Loading data ...", max_entries=8)
def load(root: str, underlying: str, expiry: str, tf: str, sig):
    # `sig` (file names + modification times) is part of the cache key, so a CSV the
    # collector rewrites makes this reload. (Arguments named _x are not hashed by Streamlit.)
    opts = D.load_options(Path(root), underlying, expiry, tf)
    idx = D.load_index(Path(root), underlying, opts["date"].unique() if not opts.empty else [], tf)
    return opts, idx


@st.cache_data(show_spinner="Computing metrics ...", max_entries=8)
def metrics(opts, idx):
    spot = D.spot_series(opts, idx)
    return spot, D.pcr(opts, spot), D.max_pain(opts), D.atm_straddle(opts, spot), D.order_flow(opts, spot)


# ---------------------------------------------------------------- sidebar
root = D.data_root()
st.sidebar.title("Selection")
unders = D.list_underlyings(root)
if not unders:
    st.error(f"No collected data found in {root}")
    st.stop()
underlying = st.sidebar.selectbox("Index", unders)
expiry = st.sidebar.selectbox("Expiry", D.list_expiries(root, underlying))
tf = st.sidebar.radio("Candle", ["1min", "5min"], horizontal=True)
watched = (root / underlying / expiry, root / underlying / "INDEX")
sig = D.files_signature(*watched)
st.session_state["data_sig"] = sig
opts_all, idx_all = load(str(root), underlying, expiry, tf, sig)

auto = st.sidebar.toggle("Auto-refresh when new data arrives", value=True)


@st.fragment(run_every=REFRESH_SECONDS if auto else None)
def _watch_files():
    """Checks the CSVs every REFRESH_SECONDS and reruns the page when the collector has written new bars."""
    if D.files_signature(*watched) != st.session_state.get("data_sig"):
        st.rerun(scope="app")
    st.caption(f"Data as of {pd.Timestamp.now(tz='Asia/Kolkata'):%H:%M:%S} IST"
               + (f" · checking every {REFRESH_SECONDS}s" if auto else " · auto-refresh off"))


with st.sidebar:
    _watch_files()
if opts_all.empty:
    st.warning("No option files for this expiry yet.")
    st.stop()

days = sorted(opts_all["date"].unique())
sel_days = st.sidebar.multiselect("Trading days", days, default=days,
                                  format_func=lambda d: d.strftime("%a %d %b"))
if not sel_days:
    st.stop()
strikes = sorted(opts_all["strike"].unique())
lo, hi = st.sidebar.select_slider("Strikes", strikes, value=(strikes[0], strikes[-1]),
                                  format_func=lambda k: f"{k:,.0f}")
opts = opts_all[opts_all["date"].isin(sel_days) & opts_all["strike"].between(lo, hi)]
idx = idx_all[idx_all["date"].isin(sel_days)] if not idx_all.empty else idx_all
if opts.empty:
    st.stop()
spot, pcr, mpain, straddle, flow = metrics(opts, idx)
times = sorted(opts["timestamp"].unique())
st.sidebar.caption(f"{len(opts['symbol'].unique())} contracts · {len(times)} bars · data: {root}")

# ---------------------------------------------------------------- header KPIs
last_t = times[-1]
st.title(f"{underlying} · expiry {pd.Timestamp(expiry):%d %b %Y}")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Spot", f"{spot.iloc[-1]:,.2f}", f"{spot.iloc[-1] - spot.iloc[0]:+,.2f} over selection")
pv = pcr["pcr"].dropna()
if not pv.empty:
    c2.metric("PCR (OI)", f"{pv.iloc[-1]:.2f}", f"{pcr['pcr_chg_open'].dropna().iloc[-1]:+.2f} since open")
c3.metric("Max pain", f"{mpain.iloc[-1]:,.0f}")
# minutes with OI but no candle leave gaps, so the KPIs use the latest bar that has a value
sv, iv_s = (straddle.get("straddle", pd.Series(dtype=float)).dropna(),
            straddle.get("atm_iv", pd.Series(dtype=float)).dropna())
if not sv.empty:
    c4.metric("ATM straddle", f"{sv.iloc[-1]:,.1f}", f"{sv.iloc[-1] - sv.iloc[0]:+,.1f}")
if not iv_s.empty:
    c5.metric("ATM IV", f"{iv_s.iloc[-1]:.2f}%", f"{iv_s.iloc[-1] - iv_s.iloc[0]:+.2f}")
st.caption(f"Latest bar: {pd.Timestamp(last_t):%a %d %b %H:%M} · PCR / max pain use only the tracked strikes")

tabs = st.tabs(["Overview", "PCR", "Open interest", "Build-up", "Volatility & gamma", "Order flow", "Contract",
                "Data explorer"])

# ---------------------------------------------------------------- overview
with tabs[0]:
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[0.45, 0.18, 0.18, 0.19],
                        subplot_titles=["Spot, futures and max pain", "Futures OI", "Put-call ratio (OI)",
                                        "ATM straddle premium"])
    if not idx.empty:
        fig.add_trace(go.Candlestick(x=idx["timestamp"], open=idx["open"], high=idx["high"], low=idx["low"],
                                     close=idx["close"], name="Spot"), row=1, col=1)
        if "fut_close" in idx:
            fig.add_trace(go.Scatter(x=idx["timestamp"], y=idx["fut_close"], name="Futures",
                                     line=dict(width=1, color="#8d99ae")), row=1, col=1)
            fig.add_trace(go.Scatter(x=idx["timestamp"], y=idx["fut_oi"], name="Futures OI",
                                     line=dict(color="#6a4c93")), row=2, col=1)
    else:
        fig.add_trace(go.Scatter(x=spot.index, y=spot, name="Spot", line=dict(color=SPOT_COLOR)), row=1, col=1)
    fig.add_trace(go.Scatter(x=mpain.index, y=mpain, name="Max pain", line=dict(dash="dot", color="#bc4749"),
                             line_shape="hv"), row=1, col=1)
    fig.add_trace(go.Scatter(x=pcr.index, y=pcr["pcr"], name="PCR", line=dict(color="#386641")), row=3, col=1)
    fig.add_hline(y=1, line=dict(width=1, dash="dash", color="grey"), row=3, col=1)
    if not straddle.empty:
        fig.add_trace(go.Scatter(x=straddle.index, y=straddle["straddle"], name="ATM straddle",
                                 line=dict(color="#d4a373")), row=4, col=1)
    fig.update_layout(height=900, xaxis_rangeslider_visible=False, margin=dict(t=40, b=20),
                      legend=dict(orientation="h", yanchor="top", y=-0.07))
    st.plotly_chart(time_axes(fig), width="stretch")
    with st.expander("How to read this"):
        st.markdown(
            "- **Max pain** is the strike where option writers lose least at expiry. Spot is often pulled "
            "towards it in the last sessions before expiry.\n"
            "- **PCR above 1**: more put OI than call OI. Put writers are confident of support, which is usually "
            "read as bullish; a falling PCR is the opposite.\n"
            "- **Futures OI rising with price** points to fresh longs; rising while price falls points to fresh shorts.\n"
            "- **ATM straddle** shows how much movement the market is pricing in. It decays with time; when it "
            "*rises* while spot moves, traders are paying up for the move.")

# ---------------------------------------------------------------- PCR
with tabs[1]:
    p = pcr.dropna(subset=["pcr"])
    if p.empty:
        st.info("No OI yet for the selected strikes.")
    else:
        last = p.iloc[-1]

        def _num(v, fmt):
            return format(v, fmt) if pd.notna(v) else "–"

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("PCR (OI)", _num(last["pcr"], ".3f"),
                  f"{_num(last['pcr_chg_open'], '+.4f')} ({_num(last['pcr_chg_open_pct'], '+.2f')}%) since open")
        k2.metric("Change-in-OI PCR", _num(last["chg_oi_pcr"], ".3f"),
                  f"PE {_num(last['pe_oi_chg'], '+,.0f')} / CE {_num(last['ce_oi_chg'], '+,.0f')}",
                  delta_color="off")
        k3.metric("PCR change vs previous bar", _num(last["pcr_chg"], "+.4f"),
                  f"{_num(last['pcr_chg_pct'], '+.2f')}%")
        k4.metric("PCR (volume)", _num(last["pcr_volume"], ".3f"))

        fig = make_subplots(rows=7, cols=1, shared_xaxes=True, vertical_spacing=0.025,
                            row_heights=[0.19, 0.12, 0.12, 0.15, 0.15, 0.15, 0.12],
                            specs=[[{"secondary_y": True}], [{}], [{}], [{"secondary_y": True}], [{}], [{}], [{}]],
                            subplot_titles=["PCR (OI) and spot", "PCR change vs previous bar",
                                            "PCR % change vs previous bar",
                                            "PCR % change vs actual PCR at previous candle",
                                            "Change-in-OI PCR (OI added since open: PE ÷ CE)",
                                            "OI added since open", "PCR % change since open"])
        fig.add_trace(go.Scatter(x=p.index, y=p["pcr"], name="PCR", line=dict(color="#386641", width=2)),
                      row=1, col=1)
        fig.add_trace(go.Scatter(x=p.index, y=p["spot"], name="Spot", line=dict(color="#8d99ae", width=1)),
                      row=1, col=1, secondary_y=True)
        fig.add_hline(y=1, line=dict(width=1, dash="dash", color="grey"), row=1, col=1)
        up = np.where(p["pcr_chg"] >= 0, CE_COLOR, PE_COLOR)
        fig.add_trace(go.Bar(x=p.index, y=p["pcr_chg"], name="PCR change", marker_color=up), row=2, col=1)
        fig.add_trace(go.Bar(x=p.index, y=p["pcr_chg_pct"], name="PCR % change (bar)", marker_color=up),
                      row=3, col=1)
        # each % move next to the PCR level it started from
        fig.add_trace(go.Bar(x=p.index, y=p["pcr_chg_pct"], name="% change vs PCR at previous candle",
                             marker_color=up, opacity=0.8), row=4, col=1)
        fig.add_trace(go.Scatter(x=p.index, y=p["pcr_prev"], name="PCR at previous candle",
                                 line=dict(color="#f2cc8f", width=1.5)), row=4, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(x=p.index, y=p["chg_oi_pcr"], name="Change-in-OI PCR",
                                 line=dict(color="#6a4c93", width=2)), row=5, col=1)
        fig.add_hline(y=1, line=dict(width=1, dash="dash", color="grey"), row=5, col=1)
        fig.add_trace(go.Scatter(x=p.index, y=p["ce_oi_chg"], name="CE OI added", line=dict(color=CE_COLOR)),
                      row=6, col=1)
        fig.add_trace(go.Scatter(x=p.index, y=p["pe_oi_chg"], name="PE OI added", line=dict(color=PE_COLOR)),
                      row=6, col=1)
        fig.add_trace(go.Scatter(x=p.index, y=p["pcr_chg_open_pct"], name="PCR % change since open",
                                 fill="tozeroy", line=dict(color="#6a994e")), row=7, col=1)
        # early in the day CE OI added is tiny, so the ratio can spike; keep the axis on the typical range
        q = p["chg_oi_pcr"].dropna()
        if len(q) > 10:
            lo_q, hi_q = q.quantile(0.02), q.quantile(0.98)
            pad = (hi_q - lo_q) * 0.1 or 0.5
            fig.update_yaxes(range=[lo_q - pad, hi_q + pad], row=5, col=1)
        fig.update_yaxes(title_text="PCR", row=1, col=1, secondary_y=False)
        fig.update_yaxes(title_text="Spot", row=1, col=1, secondary_y=True, showgrid=False)
        fig.update_yaxes(tickformat=".4f", row=2, col=1)
        fig.update_yaxes(title_text="%", row=3, col=1)
        fig.update_yaxes(title_text="% change", row=4, col=1, secondary_y=False)
        fig.update_yaxes(title_text="PCR prev", row=4, col=1, secondary_y=True, showgrid=False)
        fig.update_yaxes(title_text="Ratio", row=5, col=1)
        fig.update_yaxes(title_text="OI", row=6, col=1)
        fig.update_yaxes(title_text="%", row=7, col=1)
        fig.update_layout(height=1550, margin=dict(t=40, b=20), bargap=0,
                          legend=dict(orientation="h", yanchor="top", y=-0.04))
        st.plotly_chart(time_axes(fig), width="stretch")

        st.caption(f"Strikes {lo:,.0f}–{hi:,.0f} ({int(last['contracts'])} contracts at the latest bar). "
                   f"Files with every tracked strike: {root / underlying / 'PCR' / expiry}")
        show = p.rename(columns={"pcr_chg_pct": "pcr_chg_%", "pcr_chg_open_pct": "pcr_chg_open_%"})
        st.dataframe(show.sort_index(ascending=False), width="stretch", height=300)
        st.download_button("Download PCR table (CSV)", show.to_csv().encode(),
                           file_name=f"{underlying}_PCR_{expiry}_{tf}.csv", mime="text/csv")
    with st.expander("How to read this"):
        st.markdown(
            "- **PCR = put OI ÷ call OI** over the selected strikes (the whole open interest). Above 1, more puts "
            "are open than calls.\n"
            "- **Change / % change vs previous bar** shows *when* the sentiment shifted. Spikes that come before "
            "a spot move are the pattern to look for.\n"
            "- **% change vs actual PCR at previous candle** puts each % move (bars) next to the PCR it started "
            "from (yellow line, right axis), so you can see whether big moves happen at high or low PCR levels.\n"
            "- **Change-in-OI PCR = PE OI added since 09:15 ÷ CE OI added since 09:15.** It shows today's fresh "
            "positioning only, so it moves much faster than PCR. Above 1: more puts written today (support being "
            "built, bullish); below 1: more calls written (bearish).\n"
            "- When one side's OI added is **negative** (positions closed), the ratio turns negative or very "
            "large; read the *OI added since open* panel to see which side is moving. Early in the session the "
            "OI added is small, so the ratio is jumpy until a few minutes have passed.\n"
            "- **PCR % change since open** shows how far the overall ratio has shifted today; it resets each day.\n"
            "- Each new strike counts only the OI added after it started being tracked.")

# ---------------------------------------------------------------- open interest
with tabs[2]:
    since = st.radio("OI change measured from", ["start of each day", "start of selection"], horizontal=True)
    since_key = "day" if since.startswith("start of each") else "selection"
    cols = st.columns(2)
    for col, typ in ((cols[0], "CE"), (cols[1], "PE")):
        m = D.oi_change_matrix(opts, typ, since_key)
        hm = go.Figure(go.Heatmap(z=m.values, x=m.columns, y=m.index, zmid=0, colorbar=dict(title="OI chg"),
                                  colorscale=[[0, "#2166ac"], [0.5, "#f7f7f7"], [1, "#b2182b"]]))
        hm.add_trace(go.Scatter(x=spot.index, y=spot, name="Spot", line=dict(color="black", width=1.5)))
        hm.update_layout(title=f"{typ} OI change by strike (red = OI added, blue = OI cut)", height=450,
                         yaxis_title="Strike", showlegend=False, margin=dict(t=50, b=20))
        col.plotly_chart(time_axes(hm), width="stretch")

    t_sel = st.select_slider("Snapshot time", times, value=times[-1],
                             format_func=lambda t: pd.Timestamp(t).strftime("%d %b %H:%M"))
    day_start = min(t for t in times if pd.Timestamp(t).date() == pd.Timestamp(t_sel).date())
    snap = opts[opts["timestamp"] == t_sel].pivot_table(index="strike", columns="type", values="oi")
    base = opts[opts["timestamp"] == day_start].pivot_table(index="strike", columns="type", values="oi")
    bar = go.Figure()
    for typ, color in (("CE", CE_COLOR), ("PE", PE_COLOR)):
        if typ in snap:
            bar.add_trace(go.Bar(x=snap.index, y=snap[typ], name=f"{typ} OI", marker_color=color))
            bar.add_trace(go.Bar(x=snap.index, y=snap[typ] - base.get(typ, 0), name=f"{typ} change today",
                                 marker_color=color, opacity=0.45, marker_pattern_shape="/"))
    sp_t = spot.get(t_sel, np.nan)
    bar.add_vline(x=sp_t, line=dict(color="black", dash="dash"), annotation_text=f"spot {sp_t:,.0f}")
    bar.update_layout(barmode="group", height=420, title=f"OI by strike at {pd.Timestamp(t_sel):%d %b %H:%M}",
                      xaxis_title="Strike", margin=dict(t=50, b=20))
    st.plotly_chart(bar, width="stretch")
    with st.expander("How to read this"):
        st.markdown(
            "- The strike with the **largest CE OI** often acts as **resistance**, and the largest **PE OI** as "
            "**support**, because writers defend those levels.\n"
            "- In the heatmaps, watch for **red bands forming just below spot on the PE side** (support being "
            "built) or **just above spot on the CE side** (resistance being built). Blue bands mean writers are "
            "exiting, so that level is weakening.")

# ---------------------------------------------------------------- build-up
with tabs[3]:
    c1, c2 = st.columns([2, 1])
    t0, t1 = c1.select_slider("Window", times, value=(times[0], times[-1]),
                              format_func=lambda t: pd.Timestamp(t).strftime("%d %b %H:%M"))
    bu = D.buildup(opts, t0, t1)
    if not bu.empty:
        summary = bu.groupby(["type", "buildup"])["oi_chg"].agg(["count", "sum"]).reset_index()
        c2.dataframe(summary.rename(columns={"count": "contracts", "sum": "OI change"}), hide_index=True)
        fig = px.bar(bu, x="strike", y="oi_chg", color="buildup", facet_row="type", barmode="relative",
                     color_discrete_map=BUILDUP_COLORS, height=480,
                     hover_data={"price_chg_%": ":.1f", "oi_chg_%": ":.1f"},
                     title=f"Build-up per strike, {pd.Timestamp(t0):%d %b %H:%M} → {pd.Timestamp(t1):%d %b %H:%M}")
        st.plotly_chart(fig, width="stretch")
        st.dataframe(bu.round(2), hide_index=True, width="stretch")

    window = st.slider("Rolling window for the timeline (bars)", 3, 60, 15)
    cats = list(BUILDUP_COLORS)
    for typ in ("CE", "PE"):
        lab = D.buildup_timeline(opts, typ, window)
        z = lab.apply(lambda s: s.map({c: i for i, c in enumerate(cats)})).astype(float)
        colorscale = []
        for i, c in enumerate(cats):
            colorscale += [[i / len(cats), BUILDUP_COLORS[c]], [(i + 1) / len(cats), BUILDUP_COLORS[c]]]
        hm = go.Figure(go.Heatmap(z=z.values, x=z.columns, y=z.index, colorscale=colorscale, zmin=0,
                                  zmax=len(cats), customdata=lab.values,
                                  hovertemplate="%{x}<br>strike %{y}<br>%{customdata}<extra></extra>",
                                  colorbar=dict(tickvals=[i + 0.5 for i in range(len(cats))], ticktext=cats)))
        hm.update_layout(title=f"{typ} build-up timeline (last {window} bars at each point)", height=380,
                         yaxis_title="Strike", margin=dict(t=50, b=20))
        st.plotly_chart(time_axes(hm), width="stretch")
    with st.expander("How to read this"):
        st.markdown(
            "| Price | OI | Label | Meaning |\n|---|---|---|---|\n"
            "| ↑ | ↑ | Long build-up | fresh buyers |\n| ↓ | ↑ | Short build-up | fresh writers/sellers |\n"
            "| ↑ | ↓ | Short covering | sellers exiting (squeeze) |\n| ↓ | ↓ | Long unwinding | buyers exiting |\n\n"
            "**Short build-up in PEs** below spot = put writers building support (bullish). "
            "**Short build-up in CEs** above spot = call writers capping the upside (bearish). "
            "A switch to **short covering** at those strikes often comes before a breakout through them.")

# ---------------------------------------------------------------- volatility & gamma
with tabs[4]:
    c1, c2 = st.columns(2)
    if not straddle.empty:
        f = go.Figure(go.Scatter(x=straddle.index, y=straddle["atm_iv"], name="ATM IV", line=dict(color="#6a4c93")))
        f.update_layout(title="ATM implied volatility (%)", height=380, margin=dict(t=50, b=20))
        c1.plotly_chart(time_axes(f), width="stretch")
    t_sel = c2.select_slider("Smile / gamma time", times, value=times[-1], key="iv_t",
                             format_func=lambda t: pd.Timestamp(t).strftime("%d %b %H:%M"))
    t_ref = c2.select_slider("Compare with", times, value=times[0], key="iv_ref",
                             format_func=lambda t: pd.Timestamp(t).strftime("%d %b %H:%M"))
    f = go.Figure()
    for t, dash in ((t_ref, "dot"), (t_sel, "solid")):
        sm = D.smile(opts, t)
        for typ, color in (("CE", CE_COLOR), ("PE", PE_COLOR)):
            if typ in sm:
                f.add_trace(go.Scatter(x=sm.index, y=sm[typ], name=f"{typ} {pd.Timestamp(t):%d %b %H:%M}",
                                       line=dict(color=color, dash=dash)))
    f.add_vline(x=spot.get(t_sel, np.nan), line=dict(color="black", dash="dash"))
    f.update_layout(title="IV smile (dotted = comparison time)", height=380, xaxis_title="Strike",
                    margin=dict(t=50, b=20))
    c2.plotly_chart(f, width="stretch")

    sp_t = spot.get(t_sel, np.nan)
    gex = D.gamma_exposure(opts, t_sel, sp_t) / 1e7
    f = go.Figure(go.Bar(x=gex.index, y=gex.values, marker_color=np.where(gex.values >= 0, CE_COLOR, PE_COLOR)))
    f.add_vline(x=sp_t, line=dict(color="black", dash="dash"), annotation_text=f"spot {sp_t:,.0f}")
    f.update_layout(title=f"Gamma exposure by strike (Rs crore per 1% move) at {pd.Timestamp(t_sel):%d %b %H:%M}"
                          f" · net {gex.sum():+,.1f}", height=380, xaxis_title="Strike", margin=dict(t=50, b=20))
    st.plotly_chart(f, width="stretch")
    with st.expander("How to read this"):
        st.markdown(
            "- **ATM IV rising** while spot moves means traders are paying for the move; **IV falling** "
            "into expiry is normal decay.\n"
            "- A **steeper put side** of the smile means downside protection is in demand.\n"
            "- **Gamma exposure** assumes option writers are short. A large **positive** net means hedgers buy "
            "dips and sell rallies, so the market tends to stay range-bound. **Negative** means they chase moves, "
            "so moves tend to accelerate. Strikes with large bars act like magnets or walls.")

# ---------------------------------------------------------------- order flow
with tabs[5]:
    f = make_subplots(specs=[[{"secondary_y": True}]])
    f.add_trace(go.Scatter(x=flow.index, y=flow["signal"].rolling(5, min_periods=1).mean(),
                           name="Flow signal (5-bar avg)", line=dict(color="#6a4c93")), secondary_y=False)
    f.add_trace(go.Scatter(x=flow.index, y=flow["spot"], name="Spot", line=dict(color=SPOT_COLOR, width=1)),
                secondary_y=True)
    f.add_hline(y=0, line=dict(color="grey", width=1))
    f.update_layout(title="Call bid pressure minus put bid pressure", height=420, margin=dict(t=50, b=20))
    st.plotly_chart(time_axes(f), width="stretch")
    c1, c2 = st.columns(2)
    ll = D.lead_lag(flow)
    bars = go.Figure(go.Bar(x=ll.index, y=ll.values, marker_color=np.where(ll.values >= 0, CE_COLOR, PE_COLOR)))
    bars.update_layout(title="Does the flow signal predict spot? Correlation with the next N-bar return",
                       xaxis_title="N bars ahead", yaxis_title="correlation", height=380, margin=dict(t=50, b=20))
    c1.plotly_chart(bars, width="stretch")
    best = int(ll.abs().idxmax()) if ll.notna().any() else 5
    n = c2.slider("Bars ahead for scatter", 1, 30, best)
    fwd = flow["spot"].groupby(flow.index.date).shift(-n) / flow["spot"] - 1
    sc = pd.DataFrame({"signal": flow["signal"], "fwd_return_%": fwd * 100}).dropna()
    c2.plotly_chart(px.scatter(sc, x="signal", y="fwd_return_%", trendline=None, opacity=0.4, height=380,
                               title=f"Flow signal vs next {n}-bar spot return (%)"), width="stretch")
    with st.expander("How to read this"):
        st.markdown(
            "The signal compares **pending buy vs sell quantity** in calls with the same in puts. "
            "+1 means calls are being bid and puts offered (bullish pressure).\n\n"
            "The correlation chart tests whether this pressure **comes before** price moves. A clear peak "
            "(for example +0.2 at 5 bars) is a pattern worth tracking across expiries; values near 0 mean no "
            "edge in this selection. Check that a pattern repeats across several days before trusting it.")

# ---------------------------------------------------------------- single contract
with tabs[6]:
    c1, c2 = st.columns(2)
    k = c1.selectbox("Strike", sorted(opts["strike"].unique()),
                     index=int(np.argmin(np.abs(np.array(sorted(opts["strike"].unique())) - spot.iloc[-1]))),
                     format_func=lambda v: f"{v:,.0f}")
    typ = c2.radio("Type", ["CE", "PE"], horizontal=True)
    d = opts[(opts["strike"] == k) & (opts["type"] == typ)]
    f = make_subplots(rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                      row_heights=[0.36, 0.14, 0.16, 0.17, 0.17],
                      subplot_titles=["Price", "Volume", "Open interest", "IV (left) and delta (right)",
                                      "Total buy / sell quantity (log scale; above 1 = more bids)"],
                      specs=[[{}], [{}], [{}], [{"secondary_y": True}], [{}]])
    f.add_trace(go.Candlestick(x=d["timestamp"], open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                               name="Price"), row=1, col=1)
    f.add_trace(go.Bar(x=d["timestamp"], y=d["volume"], name="Volume", marker_color="#8d99ae"), row=2, col=1)
    f.add_trace(go.Scatter(x=d["timestamp"], y=d["oi"], name="OI", line=dict(color="#6a4c93")), row=3, col=1)
    f.add_trace(go.Scatter(x=d["timestamp"], y=d["iv"], name="IV %", line=dict(color="#bc4749")), row=4, col=1)
    f.add_trace(go.Scatter(x=d["timestamp"], y=d["delta"], name="Delta", line=dict(color="#386641")),
                row=4, col=1, secondary_y=True)
    ratio = d["total_buy_qty"] / d["total_sell_qty"]
    f.add_trace(go.Scatter(x=d["timestamp"], y=ratio, name="Buy/sell qty", line=dict(color="#d4a373")), row=5, col=1)
    f.add_hline(y=1, line=dict(width=1, dash="dash", color="grey"), row=5, col=1)
    f.update_yaxes(type="log", row=5, col=1)
    f.update_layout(height=1000, xaxis_rangeslider_visible=False, margin=dict(t=40, b=20),
                    legend=dict(orientation="h", yanchor="top", y=-0.07))
    st.plotly_chart(time_axes(f), width="stretch")
    st.download_button("Download this contract (CSV)", d.to_csv(index=False), f"{d['symbol'].iloc[0]}_{tf}.csv")

# ---------------------------------------------------------------- data explorer
SQL_EXAMPLES = {
    "Highest OI right now": "SELECT symbol, strike, type, close, oi, iv\nFROM options\n"
                            "WHERE timestamp = (SELECT max(timestamp) FROM options)\nORDER BY oi DESC",
    "Biggest 1-bar OI jumps": "SELECT timestamp, symbol, close, oi,\n"
                              "       oi - lag(oi) OVER (PARTITION BY symbol ORDER BY timestamp) AS oi_change\n"
                              "FROM options\nORDER BY oi_change DESC NULLS LAST\nLIMIT 50",
    "Volume per strike per day": "SELECT date, strike, type, sum(volume) AS volume, max(oi) AS max_oi\n"
                                 "FROM options\nGROUP BY ALL\nORDER BY date, strike, type",
    "Hourly average IV (ATM area)": "SELECT date_trunc('hour', o.timestamp) AS hour, o.type, avg(o.iv) AS avg_iv\n"
                                    "FROM options o JOIN index_data i USING (timestamp)\n"
                                    "WHERE abs(o.strike - i.close) <= 100\nGROUP BY ALL\nORDER BY hour, type",
    "Bid-heavy minutes (buy qty 2x sell)": "SELECT timestamp, symbol, close, total_buy_qty, total_sell_qty,\n"
                                           "       round(total_buy_qty / total_sell_qty, 2) AS ratio\n"
                                           "FROM options\nWHERE total_buy_qty > 2 * total_sell_qty\n"
                                           "ORDER BY ratio DESC",
}


def _explorer_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["date"] = out["timestamp"].dt.normalize()     # real dates (sortable, SQL-friendly)
    return out


def _filter_ui(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Excel-like filters: pick columns, then a range (numbers / times) or values (text) for each."""
    cols = st.multiselect("Filter on columns", list(df.columns), key=f"{key}_fcols",
                          placeholder="e.g. type, strike, oi, timestamp")
    for c in cols:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            lo_t, hi_t = s.min().to_pydatetime(), s.max().to_pydatetime()
            if lo_t < hi_t:
                a, b = st.slider(c, lo_t, hi_t, (lo_t, hi_t), format="DD MMM HH:mm", key=f"{key}_{c}")
                df = df[df[c].between(a, b)]
        elif pd.api.types.is_numeric_dtype(s):
            c1, c2 = st.columns(2)
            lo_v = c1.number_input(f"{c} ≥", value=float(s.min()) if s.notna().any() else 0.0, key=f"{key}_{c}_lo")
            hi_v = c2.number_input(f"{c} ≤", value=float(s.max()) if s.notna().any() else 0.0, key=f"{key}_{c}_hi")
            df = df[df[c].between(lo_v, hi_v)]
        else:
            vals = sorted(s.dropna().astype(str).unique())
            pick = st.multiselect(c, vals, default=vals, key=f"{key}_{c}")
            df = df[s.astype(str).isin(pick)]
    return df


with tabs[7]:
    dsets = {"Options (one row per contract per bar)": _explorer_frame(opts)}
    if not idx.empty:
        dsets["Index & futures"] = _explorer_frame(idx)
    c1, c2 = st.columns([1, 2])
    mode = c1.radio("Mode", ["Filters", "SQL"], horizontal=True)

    if mode == "Filters":
        ds_name = c2.radio("Dataset", list(dsets), horizontal=True)
        df = dsets[ds_name]
        default_cols = [c for c in ["timestamp", "symbol", "strike", "type", "open", "high", "low", "close", "volume",
                                    "oi", "iv", "delta", "total_buy_qty", "total_sell_qty"] if c in df]
        show = st.multiselect("Columns", list(df.columns), default=default_cols or list(df.columns))
        result = _filter_ui(df, ds_name[:5])[show or list(df.columns)]
    else:
        import duckdb
        ex = st.selectbox("Start from an example", ["(write my own)"] + list(SQL_EXAMPLES))
        default_sql = SQL_EXAMPLES.get(ex, "SELECT *\nFROM options\nWHERE type = 'PE'\nORDER BY oi DESC\nLIMIT 100")
        sql = st.text_area("SQL (tables: options, index_data)", default_sql, height=170, key=f"sql_{ex}")
        st.caption("Columns: " + ", ".join(dsets[next(iter(dsets))].columns))
        try:
            con = duckdb.connect()
            con.register("options", dsets[next(iter(dsets))])
            con.register("index_data", dsets.get("Index & futures", pd.DataFrame({"timestamp": []})))
            result = con.execute(sql).df()
        except Exception as e:
            st.error(f"Query failed: {e}")
            result = pd.DataFrame()

    st.caption(f"{len(result):,} rows × {len(result.columns)} columns · click a column header to sort · "
               "use the table's search icon to find text")
    st.dataframe(result, width="stretch", height=560, hide_index=True)
    st.download_button("Download these rows (CSV)", result.to_csv(index=False), "query_result.csv",
                       disabled=result.empty)

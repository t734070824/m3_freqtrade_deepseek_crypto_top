#!/usr/bin/env python3
"""M3-DSH 长时归因守护: 周期性统计各实验的期望值/盈亏比, 达标或异常时写告警.

设计要点(与 monitor 的区别):
    monitor 只是**记录**快照; 本脚本负责**判断**, 并且**只在状态发生变化时报警** ——
    否则每 30 分钟都重复喊「样本不足」会淹没真正重要的信号。

判定规则(唯一判据):
    READY(n) = 已平仓笔数 >= MIN_TRADES(默认 20)
    PASS     = 期望值/笔 > 0 且 盈亏比 > 1
告警类型:
    * READY_XX   : 某实验样本首次达标 -> 可以下结论了(PASS / FAIL)
    * DEGRADE_XX : 原本达标的实验掉回不达标
    * RATE_LIMIT : 某容器近期出现币安 429
    * BOT_DOWN   : 某容器不在运行
    * SUMMARY_*  : 每 STATUS_EVERY 轮输出一次状态摘要(不重复报警)

产出:
    logs/attribution.log       人类可读(带 北京时间 + UTC 双标注)
    logs/attribution.jsonl     机器可读历史(便于看趋势)
    logs/ALERTS.md             仅追加告警(供人工/后续自动化读取)

用法: python3 scripts/dshc_attribution.py [--interval 1800] [--min-trades 20] [--once]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))
LOG = ROOT / "logs" / "attribution.log"
JSONL = ROOT / "logs" / "attribution.jsonl"
ALERTS = ROOT / "logs" / "ALERTS.md"
STATE = ROOT / "logs" / ".attribution_state.json"

# 实验 A(追涨)已按归因结论停用(69 笔负期望), 其档位由 F 接管;
# A 的最终结论保留在 docs/CHANGELOG.md 与 logs/ALERTS.md 中, 不再参与轮询。
BOTS = [
    ("F", "负费率+急跌 M3CarryDip", "m3dsc-freqtrade-f", "DSHC_F_API_PORT", 18081),
    ("B", "反弹 M3DipRevert", "m3dsc-freqtrade-dip", "DSHC_DIP_API_PORT", 18084),
    ("C", "Carry M3CarryLong", "m3dsc-freqtrade-carry", "DSHC_CARRY_API_PORT", 18085),
    # D 已退役(容器删除); 端口 18086 已移交 H。指向不存在端口, 防止串号。
    ("D", "突破 M3VolBreakout(已退役)", "m3dsc-freqtrade-vol", "DSHC_VOL_API_PORT", 18998),
    # E 已退役(容器删除); 其端口 18087 已移交 G。保留历史条目但指向不存在端口, 防止串号。
    ("E", "费率空 M3FundingShort(已退役)", "m3dsc-freqtrade-fshort", "DSHC_FSHORT_API_PORT", 18999),
    ("G", "融合 M3CarryDipTurbo", "m3dsc-freqtrade-turbo", "DSHC_TURBO_API_PORT", 18087),
    ("H", "正费率 M3DipTrend", "m3dsc-freqtrade-htrend", "DSHC_HTREND_API_PORT", 18086),
]


def stamp(ts: float | None = None) -> tuple[str, str]:
    dt = datetime.fromtimestamp(ts or time.time())
    return (dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S"), dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v
    return out


def api(port: int, path: str, e: dict[str, str]):
    u = os.environ.get("DSHC_FT_API_USER") or e.get("DSHC_FT_API_USER", "m3dsc")
    pw = os.environ.get("DSHC_FT_API_PASS") or e.get("DSHC_FT_API_PASS", "")
    tok = base64.b64encode(f"{u}:{pw}".encode()).decode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={"Authorization": f"Basic {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}


def container_state(name: str) -> str:
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", name],
                             capture_output=True, text=True, timeout=10)
        return (out.stdout or "missing").strip() or "missing"
    except Exception:  # noqa: BLE001
        return "unknown"


def count_429(container: str, since_s: int) -> int:
    try:
        out = subprocess.run(["docker", "logs", "--since", f"{since_s}s", container],
                             capture_output=True, text=True, timeout=25)
        return (out.stdout + out.stderr).count("429")
    except Exception:  # noqa: BLE001
        return 0


def stats_of(trades: list[dict]) -> dict:
    closed = [t for t in trades if not t.get("is_open")]
    ps = [(t.get("profit_abs") or 0) for t in closed]
    wins = [x for x in ps if x > 0]
    losses = [x for x in ps if x <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    holds = []
    for t in closed:
        try:
            a = datetime.fromisoformat(t["open_date"]); b = datetime.fromisoformat(t["close_date"])
            holds.append((b - a).total_seconds() / 60.0)
        except Exception:
            pass
    return {
        "closed": len(closed), "open": len(trades) - len(closed),
        "win": len(wins), "loss": len(losses),
        "winrate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "avg_win": statistics.fmean(wins) if wins else 0.0,
        "avg_loss": statistics.fmean(losses) if losses else 0.0,
        "profit_factor": (gp / gl) if gl else (float("inf") if gp else 0.0),
        "expectancy": statistics.fmean(ps) if ps else 0.0,
        "total": sum(ps),
        "funding": sum((t.get("funding_fees") or 0) for t in closed),
        "hold_med": statistics.median(holds) if holds else 0.0,
    }


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(s: dict) -> None:
    try:
        STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def alert(kind: str, text: str) -> None:
    cst, utc = stamp()
    line = f"- **[{kind}]** {cst} 北京时间 / {utc} UTC — {text}\n"
    try:
        if not ALERTS.exists():
            ALERTS.write_text("# M3-DSH 告警(仅追加)\n\n", encoding="utf-8")
        with ALERTS.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        pass


def cycle(args: argparse.Namespace) -> dict:
    e = env()
    cst, utc = stamp()
    rows = []
    alerts_now: list[str] = []
    state = load_state()

    for code, label, container, envkey, default_port in BOTS:
        port = int(os.environ.get(envkey) or e.get(envkey) or default_port)
        prof = api(port, "/api/v1/profit", e)
        tr = api(port, "/api/v1/trades?limit=1000", e)
        st = container_state(container)
        if "__error__" in prof:
            rows.append({"code": code, "label": label, "error": prof["__error__"][:60],
                         "container": st})
            if st != "running":
                key = f"down_{code}"
                if state.get(key) != st:
                    alert("BOT_DOWN", f"实验 {code}({label}) 容器 {container} 状态 = {st}")
                    alerts_now.append(f"BOT_DOWN {code}")
                    state[key] = st
            continue
        trades = tr.get("trades", tr) if isinstance(tr, dict) else tr
        s = stats_of(trades or [])
        s.update({"code": code, "label": label, "container": st,
                  "equity": (prof or {}).get("profit_all_coin", 0) or 0})
        ready = s["closed"] >= args.min_trades
        ratio = (abs(s["avg_win"] / s["avg_loss"]) if s["avg_loss"] else float("inf"))
        passed = s["expectancy"] > 0 and (ratio > 1 if ratio != float("inf") else s["avg_win"] > 0)
        s["ratio"] = ratio
        s["ready"] = ready
        s["passed"] = passed
        rows.append(s)

        # ---- 样本达标 -> 可以下结论 ----
        if ready:
            key = f"ready_{code}_{args.min_trades}"
            verdict = "PASS ✔" if passed else "FAIL ✘"
            if state.get(key) != verdict:
                alert("READY" if passed else "FAIL", (
                    f"实验 {code}({label}) 样本达标: {s['closed']} 笔, 胜率 {s['winrate']:.1f}%, "
                    f"期望 {s['expectancy']:+.3f}/笔, 盈亏比 {ratio:.2f}, PF {s['profit_factor']:.2f} "
                    f"-> 判定 {verdict}"))
                alerts_now.append(f"{'READY' if passed else 'FAIL'} {code}")
                state[key] = verdict
        # ---- 达标后掉回不达标 ----
        elif state.get(f"ready_{code}_{args.min_trades}") == "PASS ✔" and not passed:
            alert("DEGRADE", f"实验 {code}({label}) 期望值转负: 期望 {s['expectancy']:+.3f}/笔")
            alerts_now.append(f"DEGRADE {code}")
            state[f"ready_{code}_{args.min_trades}"] = "DEGRADED"

        # ---- 限流 ----
        n429 = count_429(container, args.interval)
        if n429 >= args.rate_limit_alert:
            key = f"rl_{code}"
            if state.get(key, 0) < n429:
                alert("RATE_LIMIT", f"实验 {code} 近 {args.interval}s 出现 {n429} 次币安 429")
                alerts_now.append(f"RATE_LIMIT {code}")
                state[key] = n429
        else:
            state[f"rl_{code}"] = 0

    save_state(state)

    # ---- 落盘 ----
    ok = [r for r in rows if "error" not in r]
    summary = {
        "cst": cst, "utc": utc,
        "experiments": [{k: (None if v == float("inf") else v) for k, v in r.items()
                         if k in ("code", "label", "closed", "open", "win", "loss", "winrate",
                                  "avg_win", "avg_loss", "ratio", "expectancy", "profit_factor",
                                  "total", "equity", "funding", "hold_med", "ready", "passed",
                                  "container")} for r in ok],
        "alerts": alerts_now,
    }
    try:
        with JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass

    lines = ["", f"===== 归因 @ {cst} 北京时间 / {utc} UTC ====="]
    for r in rows:
        if "error" in r:
            lines.append(f"  {r['code']} {r['label']}: 不可用({r['error']}) 容器={r['container']}")
            continue
        flag = ("READY " + ("PASS ✔" if r["passed"] else "FAIL ✘")) if r["ready"] else "样本不足"
        lines.append(
            f"  {r['code']} {r['label']:<24} 权益{r['equity']:+8.2f} | 平仓{r['closed']:3d}(持仓{r['open']}) "
            f"胜率{r['winrate']:5.1f}% | 均盈{r['avg_win']:+6.2f} 均亏{r['avg_loss']:+7.2f} "
            f"盈亏比{r['ratio']:5.2f} | 期望{r['expectancy']:+7.3f} | PF{r['profit_factor']:5.2f} "
            f"| 资金费{r['funding']:+7.4f} | 持仓中位{r['hold_med']:5.0f}min | {flag}")
    if alerts_now:
        lines.append(f"  >>> 本轮告警: {', '.join(alerts_now)}")
    text = "\n".join(lines)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass
    print(text, flush=True)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=1800, help="统计间隔秒(默认 1800=30 分钟)")
    ap.add_argument("--min-trades", type=int, default=20, help="样本达标门槛(默认 20 笔)")
    ap.add_argument("--rate-limit-alert", type=int, default=5, help="429 告警阈值")
    ap.add_argument("--once", action="store_true", help="只跑一次")
    args = ap.parse_args()
    if args.once:
        cycle(args)
        return 0
    while True:
        try:
            cycle(args)
        except Exception as exc:  # noqa: BLE001
            cst, utc = stamp()
            try:
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n!! 归因异常 {cst} 北京时间 / {utc} UTC: {exc}\n")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

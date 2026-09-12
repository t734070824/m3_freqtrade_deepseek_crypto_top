#!/usr/bin/env python3
"""M3-DSH 全量运行汇报: 一次性汇总所有 dry-run 的运行情况.

产出六个部分:
    1. 容器运行时长与资源占用
    2. 各实验账本(权益/交易/胜率/期望值/盈亏比/利润因子/资金费/持仓时长)
    3. 当前持仓明细
    4. 数据采集与限额健康
    5. 异常扫描(429 / ERROR / Traceback / 数据断档)
    6. 最近交易明细

时间一律双标注: 北京时间(UTC+8) 与 UTC。
用法: python3 scripts/dshc_report_all.py [--trades 8]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))

# 运行中的实验(端口不要复用: 实验 A 停用后, 其档位 18081 已交给实验 F)
BOTS = [
    ("F", "负费率+急跌 M3CarryDip", "m3dsc-freqtrade-f", "DSHC_F_API_PORT", 18081),
    ("B", "反弹 M3DipRevert", "m3dsc-freqtrade-dip", "DSHC_DIP_API_PORT", 18084),
    ("C", "Carry M3CarryLong", "m3dsc-freqtrade-carry", "DSHC_CARRY_API_PORT", 18085),
    ("D", "突破 M3VolBreakout", "m3dsc-freqtrade-vol", "DSHC_VOL_API_PORT", 18086),
    ("E", "费率空 M3FundingShort", "m3dsc-freqtrade-fshort", "DSHC_FSHORT_API_PORT", 18087),
]
INFRA = ["m3dsc-market-collector", "m3dsc-dashboard"]
# 已停用的实验(容器已 stop, 保留用于历史对照; 不参与实时统计)
STOPPED = [
    ("A", "追涨 M3GainersTrend", "m3dsc-freqtrade-dryrun",
     "69 笔样本判定负期望: 期望 -1.143 USDT/笔, 盈亏比 0.30, PF 0.61, 累计 -87.55"),
]


def now_stamp() -> str:
    dt = datetime.now(timezone.utc)
    return (dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间(UTC+8) / "
            + dt.strftime("%Y-%m-%d %H:%M:%S") + " UTC")


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
    except Exception:  # noqa: BLE001
        return None


def sh(cmd: list[str], timeout: int = 25) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:  # noqa: BLE001
        return ""


def container_info(name: str) -> dict:
    # 注意: docker inspect 对多行模板会重复输出, 因此分别取值更稳
    out = sh(["docker", "inspect", "-f",
              "{{.State.Status}}~{{.RestartCount}}~{{.State.StartedAt}}", name])
    parts = (out.strip().splitlines() or [""])[0].split("~")
    status = parts[0] if parts else "missing"
    restarts = parts[1] if len(parts) > 1 else "0"
    started = parts[2] if len(parts) > 2 else ""
    uptime = ""
    try:
        # Docker 的 StartedAt 有 9 位纳秒, Python 3.10 的 fromisoformat 只吃 6 位 -> 截断
        started = re.sub(r"(\.\d{6})\d+", r"\1", started.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(started)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        h, m = divmod(int(secs) // 60, 60)
        uptime = f"{h}h{m:02d}m" if h else f"{m}m"
    except Exception:  # noqa: BLE001
        pass
    stats = sh(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", name],
               timeout=30).strip()
    cpu, mem = (stats.split("|") + ["", ""])[:2] if stats else ("", "")
    return {"status": status, "uptime": uptime, "restarts": restarts,
            "cpu": cpu.strip(), "mem": mem.split("/")[0].strip()}


def scan_logs(container: str, since_s: int) -> dict:
    txt = sh(["docker", "logs", "--since", f"{since_s}s", container], timeout=40)
    return {
        "429": txt.count("429"),
        "error": len(re.findall(r"\bERROR\b", txt)),
        "traceback": txt.count("Traceback"),
        "timeout": len(re.findall(r"Timeout|NetworkError|NewConnectionError", txt)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=int, default=6)
    args = ap.parse_args()
    e = env()

    print("=" * 100)
    print("M3-DSH 全部 dry-run 运行汇报")
    print("  时间: " + now_stamp())
    print("=" * 100)

    # ---------------- 1. 容器 ----------------
    print("\n【1】容器运行情况")
    print("  %-28s %-10s %-8s %-7s %-9s %s" % ("容器", "状态", "运行时长", "重启", "CPU", "内存"))
    for name in INFRA + [b[2] for b in BOTS] + [s[2] for s in STOPPED]:
        ci = container_info(name)
        print("  %-28s %-10s %-8s %-7s %-9s %s" % (name, ci["status"], ci["uptime"],
                                                   ci["restarts"], ci["cpu"], ci["mem"]))

    # ---------------- 2. 账本 ----------------
    print("\n【2】各实验账本")
    print("     判据只用「已平仓(已实现)」: 期望值/笔 > 0 且 盈亏比 > 1, 样本 < 20 笔不下结论")
    print("     ⚠️ 总权益含浮动盈亏, 会与已实现结论反向 —— 必须分开看(2026-09-13 因此差点误判)")
    print("  %-4s %-22s %9s %9s %9s %6s %6s %7s %8s %7s %8s %9s" % (
        "ID", "策略", "总权益", "已实现", "浮动", "平仓", "持仓", "胜率%", "均盈", "均亏",
        "期望/笔", "资金费"))
    results = {}
    for code, label, container, envkey, default_port in BOTS:
        port = int(os.environ.get(envkey) or e.get(envkey) or default_port)
        prof = api(port, "/api/v1/profit", e)
        tr = api(port, "/api/v1/trades?limit=1000", e)
        if prof is None:
            print("  %-4s %-22s   [不可用]" % (code, label))
            continue
        trades = (tr.get("trades", tr) if isinstance(tr, dict) else tr) or []
        closed = [t for t in trades if not t.get("is_open")]
        ps = [(t.get("profit_abs") or 0) for t in closed]
        wins = [x for x in ps if x > 0]
        losses = [x for x in ps if x <= 0]
        gp, gl = sum(wins), abs(sum(losses))
        ratio = (gp / gl) if gl else (float("inf") if gp else 0.0)
        exp = statistics.fmean(ps) if ps else 0.0
        funding = sum((t.get("funding_fees") or 0) for t in closed)
        results[code] = {"label": label, "equity": prof.get("profit_all_coin", 0) or 0,
                         "closed": len(closed), "open": len(trades) - len(closed),
                         "winrate": (len(wins) / len(closed) * 100) if closed else 0.0,
                         "avg_win": statistics.fmean(wins) if wins else 0.0,
                         "avg_loss": statistics.fmean(losses) if losses else 0.0,
                         "gp_gl": ratio, "exp": exp, "funding": funding, "trades": trades}
        rr = (abs(results[code]["avg_win"] / results[code]["avg_loss"])
              if results[code]["avg_loss"] else float("inf"))
        realized = sum(ps)
        results[code]["realized"] = realized
        results[code]["floating"] = results[code]["equity"] - realized
        print("  %-4s %-22s %+9.2f %+9.2f %+9.2f %6d %6d %7.1f %+8.2f %+8.2f %+8.3f %+9.4f" % (
            code, label, results[code]["equity"], realized, results[code]["floating"],
            results[code]["closed"], results[code]["open"], results[code]["winrate"],
            results[code]["avg_win"], results[code]["avg_loss"], exp, funding))

    # ---------------- 3. 持仓 ----------------
    print("\n【2b】已停用的实验(保留历史结论, 不参与实时统计)")
    for code, label, container, note in STOPPED:
        print("  %s %-22s 容器 %s" % (code, label, container_info(container)["status"]))
        print("     %s" % note)

    print("\n【3】当前持仓")
    any_pos = False
    for code, label, container, envkey, default_port in BOTS:
        port = int(os.environ.get(envkey) or e.get(envkey) or default_port)
        st = api(port, "/api/v1/status", e)
        if not st:
            continue
        for t in st:
            any_pos = True
            print("  %s %-20s %-3s 开%+.6g 现%+.6g %+7.2f%% (%+7.2f) 杠杆%-4s 止损权益%+7.2f%%"
                  % (code, t.get("pair", ""), "空" if t.get("is_short") else "多",
                     t.get("open_rate") or 0, t.get("current_rate") or 0,
                     t.get("profit_pct") or 0, t.get("profit_abs") or 0,
                     t.get("leverage"), t.get("stop_loss_pct") or 0))
    if not any_pos:
        print("  (五个实验当前均无持仓 —— B/C/D/E 的空仓属于「门槛未触发」的正常状态)")

    # ---------------- 4. 数据与限额 ----------------
    print("\n【4】数据采集与限额健康")
    cs = ROOT / "data" / "live" / "collector_status.json"
    try:
        c = json.loads(cs.read_text(encoding="utf-8"))
        up = c.get("uptime_min", 0)
        print("  采集器已运行 %.1f 分钟; HTTP 统计: %s" % (up, c.get("http", {})))
        bad = []
        for k, v in sorted(c.get("workers", {}).items()):
            age = (time.time() * 1000 - (v.get("last_ok_ms") or 0)) / 1000
            if age > max(900, v["interval"] * 5):
                bad.append("%s(距今%.0fs)" % (k, age))
        print("  采集任务: %d 个全部正常" % len(c.get("workers", {})) if not bad
              else "  ⚠ 心跳过期: " + ", ".join(bad))
    except Exception as exc:  # noqa: BLE001
        print("  ⚠ 无法读取采集器状态: %s" % exc)
    try:
        wl = json.loads((ROOT / "data" / "live" / "watchlist.json").read_text(encoding="utf-8"))
        age = (time.time() * 1000 - wl.get("generated_ms", 0)) / 1000
        print("  候选池: %d 个标的, 生成于 %s (距今 %.0fs)"
              % (len(wl.get("candidates", [])), wl.get("generated_cst", "?"), age))
        macro = wl.get("macro", {})
        print("  宏观: 恐贪 %s | 新闻情绪 %s" % (macro.get("fear_greed", "n/a"),
                                              macro.get("news_sentiment", "n/a")))
    except Exception as exc:  # noqa: BLE001
        print("  ⚠ 无法读取候选池: %s" % exc)

    # ---------------- 5. 异常扫描 ----------------
    print("\n【5】异常扫描(近 10 分钟)")
    print("  %-28s %6s %7s %10s %8s" % ("容器", "429", "ERROR", "Traceback", "网络超时"))
    for name in INFRA + [b[2] for b in BOTS] + [s[2] for s in STOPPED]:
        s = scan_logs(name, 600)
        flag = "" if (s["429"] == 0 and s["error"] == 0 and s["traceback"] == 0) else "  <-- 需关注"
        print("  %-28s %6d %7d %10d %8d%s" % (name, s["429"], s["error"], s["traceback"],
                                              s["timeout"], flag))

    # ---------------- 6. 最近交易 ----------------
    print("\n【6】最近交易明细")
    for code in [c[0] for c in BOTS]:
        r = results.get(code)
        if not r or not r["trades"]:
            continue
        closed = [t for t in r["trades"] if not t.get("is_open")][-args.trades:]
        if not closed:
            continue
        print("  --- %s %s ---" % (code, r["label"]))
        for t in closed:
            try:
                a = datetime.fromisoformat(t["open_date"]); b = datetime.fromisoformat(t["close_date"])
                hold = (b - a).total_seconds() / 60.0
            except Exception:  # noqa: BLE001
                hold = 0.0
            print("     #%-4s %-20s %-3s %+7.2f%% %+8.2f 资金费%+7.4f 持仓%5.0fmin %-22s %s"
                  % (t.get("trade_id"), t.get("pair", ""),
                     "空" if t.get("is_short") else "多", (t.get("profit_ratio") or 0) * 100,
                     t.get("profit_abs") or 0, t.get("funding_fees") or 0, hold,
                     t.get("exit_reason") or "", t.get("enter_tag") or ""))

    # ---------------- 7. 归因守护 ----------------
    print("\n【7】归因守护")
    alerts = ROOT / "logs" / "ALERTS.md"
    if alerts.exists():
        lines = [l for l in alerts.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
        print("  告警条数: %d" % len(lines))
        for l in lines[-5:]:
            print("   " + l[:150])
    else:
        print("  暂未产生告警")

    print("\n" + "=" * 100)
    print("汇报结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

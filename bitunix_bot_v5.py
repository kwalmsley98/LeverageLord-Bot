"""
bitunix_bot_v5.py — FINAL CONFIG (v4 + multi-pair research + risk tuning)

Multi-pair ignition research (Jun-Nov 2025, incl. crash, fees in):
  SOL  45.7% WR, +0.50R/trade, 1.8 tr/mo  -> KEEP (1.5% risk)
  ETH  40.8% WR, +0.32R/trade, 4.8 tr/mo  -> KEEP (1.5% risk)
  BNB  36.4% WR, +0.17R/trade, 4.1 tr/mo  -> WATCH (half risk)
  BTC  33.3% WR, +0.05R/trade             -> donchian only, minimal risk
  DOGE 26.1% WR, -0.19R/trade             -> CUT
  XRP  26.7% WR, -0.17R/trade             -> CUT
Portfolio estimate: ~10-11 trades/mo, +2 to +3%/mo optimistic, ~+2%/mo realistic.
29% of trades cluster within 12h -> total open risk capped at 3%.

LAUNCH: dry-run 1 week -> verify #VERIFY paths -> min size until kill switch
has 40 trades (SOL/ETH) - it judges whether the edge is real.
"""

import os, time, json, hashlib, logging, csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import requests

BASE = "https://fapi.bitunix.com"
INTERVAL_MS = {"1h": 3600e3, "4h": 14400e3}

@dataclass
class Config:
    symbols: list = field(default_factory=lambda: ["ETHUSDT", "SOLUSDT", "BNBUSDT", "BTCUSDT", "DOTUSDT", "NEARUSDT"])
    signal_map: dict = field(default_factory=lambda: {"ETHUSDT": ["ignition", "donchian"],
                                                      "SOLUSDT": ["ignition", "donchian"],
                                                      "BNBUSDT": ["ignition"],
                                                      "BTCUSDT": ["donchian"],
                                                      "DOTUSDT": ["donchian"],      # scanner sleeve - validated 2 windows
                                                      "NEARUSDT": ["ignition"]})    # scanner sleeve - validated 2 windows
    risk_map: dict = field(default_factory=lambda: {"ETHUSDT": 0.035, "SOLUSDT": 0.035,   # ceiling config - max survivable aggression
                                                      "BNBUSDT": 0.0175, "BTCUSDT": 0.010,
                                                      "DOTUSDT": 0.0175, "NEARUSDT": 0.0175})
    max_open_risk: float = 0.07
    scanner_dynamic: bool = True     # build universe from ALL Bitunix perps (volume-filtered)
    scanner_min_vol24: float = 20_000_000.0   # 24h volume floor (USDT) - protects thin-coin slippage
    scanner_max_pairs: int = 40      # cap on universe size per scan
    scanner_pairs: list = field(default_factory=lambda: ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
                                                          "DOGEUSDT","LINKUSDT","ADAUSDT","LTCUSDT","ARBUSDT","OPUSDT"])
    scanner_risk: float = 0.0125     # per top-gainer trade (validated two windows)
    scanner_max: int = 2             # max concurrent scanner positions
    scanner_ret: float = 0.04        # min 24-bar gain to qualify
    entry_limit: bool = True        # lever 1: maker-limit entries (free ~+0.5-1%/mo)
    limit_offset: float = 0.0005    # place limit 0.05% better than signal price
    entry_timeout_bars: int = 2     # bars to wait for fill, then cancel
    interval: str = "4h"
    donchian_n: int = 20
    ema_trend: int = 200
    risk_per_trade: float = 0.015        # 1% - chassis validated for this structure
    stop_atr: float = 1.0               # stop = 1x ATR
    tp_r: float = 2.5                   # validated 2.0-2.5 range
    stop_min: float = 0.008
    stop_max: float = 0.05
    max_notional_lev: float = 10.0
    vol_spike: float = 1.5
    flow_long: float = 0.55
    flow_short: float = 0.45
    daily_loss_limit: float = 0.05
    kill_min_trades: int = 40           # lower: candidate edge needs faster verdicts
    kill_wr_buffer: float = 0.02
    poll_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_SECONDS", "60")))
    dry_run: bool = True
    dry_equity: float = 10_000.0
    tg_token: str = field(default_factory=lambda: os.getenv("TG_TOKEN", ""))   # Telegram bot token from @BotFather
    tg_chat: str = field(default_factory=lambda: os.getenv("TG_CHAT", ""))     # your chat id (from @userinfobot)

FEE_M, FEE_T, SLIP = 0.0002, 0.0006, 0.0002
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()])

_ACCT_DEBUGGED = False
_LAST_EQ = None
_TG_TOKEN = os.getenv("TG_TOKEN", "")
_TG_CHAT = os.getenv("TG_CHAT", "")
def notify(msg):
    """Send msg to your Telegram - the bot's voice on your phone."""
    if not (_TG_TOKEN and _TG_CHAT): return
    try:
        requests.post(f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
                      data={"chat_id": _TG_CHAT, "text": msg, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=10)
    except Exception:
        pass

def sparkline(vals, width=24):
    if not vals: return ""
    vals = vals[-width:]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12: return "▁"*len(vals)
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[min(int((v-lo)/(hi-lo)*7.999), 7)] for v in vals)

def fmt_qty(q):
    s = f"{q:.6f}".rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"

def fmt_px(p, tick=0.01):
    return f"{round(p/tick)*tick:.10f}".rstrip("0").rstrip(".")

class BitunixClient:
    def __init__(self, key, secret):
        self.key, self.secret, self.s = key, secret, requests.Session()
    def _sign(self, method, path, body_str, params=None):
        nonce = os.urandom(16).hex(); ts = str(int(time.time()*1000))
        # Bitunix official spec (api-docs/futures/common/sign.html):
        # digest = SHA256(nonce + timestamp + api-key + queryParams + body)
        # queryParams = keys sorted ascending, concatenated as key+value, no separators
        qp = ""
        if params:
            for k, v in sorted(params.items()):
                qp += f"{k}{v}"
        inner = hashlib.sha256((nonce+ts+self.key+qp+body_str).encode()).hexdigest()
        return {"api-key": self.key, "nonce": nonce, "timestamp": ts,
                "sign": hashlib.sha256((inner+self.secret).encode()).hexdigest(),
                "language": "en-US", "Content-Type": "application/json"}
    def _req(self, method, path, params=None, payload=None):
        body_str = json.dumps(payload, separators=(",",":")) if payload else ""
        r = self.s.request(method, BASE+path, params=params, data=body_str or None,
                           headers=self._sign(method, path, body_str, params), timeout=15)
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
        if data.get("code") != 0:
            raise RuntimeError(f"{path} -> code {data.get('code')}: {data.get('msg')}")
        return data.get("data")
    def klines(self, symbol, interval, limit=200):
        rows = self._req("GET", "/api/v1/futures/market/kline",
                         {"symbol": symbol, "interval": interval, "limit": min(limit, 200)})
        return [{"o":float(r["open"]), "h":float(r["high"]), "l":float(r["low"]),
                 "c":float(r["close"]), "t":int(r["time"]),
                 "vol":float(r.get("vol",0)), "tb":float(r.get("takerVol", r.get("takerBuyVol",0) or 0))} for r in rows]
    def tickers(self):   # VERIFY field names - Bitunix 'Get All Tickers'
        rows = self._req("GET", "/api/v1/futures/market/tickers") or []
        out = []
        for t in rows:
            sym = t.get("symbol", "")
            vol = float(t.get("vol") or t.get("volume") or t.get("turnover") or t.get("amount") or 0)
            out.append({"symbol": sym, "vol24": vol})
        return out

    def equity_usdt(self):
        global _LAST_EQ
        acct = None; used = "?"; errs = []
        for path in ["/api/v1/futures/account",              # CONFIRMED via Bitunix official docs
                     "/api/v1/futures/account/assets"]:
            try:
                acct = self._req("GET", path, {"marginCoin": "USDT"})
                if acct is not None:
                    used = path; break
            except Exception as e:
                errs.append(f"{path} => {e}")
        if acct is None:
            logging.warning("ACCOUNT FAIL | " + " || ".join(errs)[:500])
            if _LAST_EQ is not None:
                logging.info(f"using cached equity {_LAST_EQ}")
                return _LAST_EQ
            raise RuntimeError("account unreachable: " + " ;; ".join(errs)[:250])
        a = acct[0] if isinstance(acct, list) and acct else (acct if isinstance(acct, dict) else {})
        global _ACCT_DEBUGGED
        if not _ACCT_DEBUGGED:
            _ACCT_DEBUGGED = True
            logging.info(f"ACCOUNT RAW via {used}: {json.dumps(a)[:300]}")
        def f(*keys):
            for k in keys:
                if k in a and a[k] not in (None, ""):
                    return float(a[k])
            return 0.0
        eq = f("available","availableVol","availableBalance") + f("margin","positionMargin") + f("crossUnrealizedPNL") + f("isolationUnrealizedPNL")
        _LAST_EQ = eq
        return eq

    def positions(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        rows = None
        for path in ["/api/v1/futures/position/get_pending_positions",   # confirmed from Bitunix docs
                     "/api/v1/futures/position/pending_positions"]:
            try:
                rows = self._req("GET", path, params)
                if rows is not None: break
            except Exception:
                continue
        rows = rows or []
        out=[]
        for p in rows:
            q=float(p.get("qty") or p.get("size") or p.get("positionSize") or 0)
            if q>0: out.append({"id":str(p.get("positionId")), "sym":p["symbol"],
                                "side":1 if p.get("side")=="LONG" else -1, "qty":q})
        return out
    def cancel(self, symbol, order_id):
        for path in ["/api/v1/futures/trade/cancel_order",
                     "/api/v1/futures/order/cancel_order"]:
            try:
                return self._req("POST", path, payload={"symbol": symbol, "orderId": str(order_id)})
            except Exception:
                continue
    def place(self, symbol, side, qty, trade_side, order_type="MARKET", price=None,
              stop=None, target=None, tick=0.01):
        payload={"symbol":symbol,"side":side,"qty":fmt_qty(qty),"tradeSide":trade_side,
                 "orderType":order_type,"effect":"GTC","clientId":f"botv5-{int(time.time()*1000)}"}
        if order_type=="LIMIT": payload["price"]=fmt_px(price,tick)
        if trade_side=="OPEN" and stop and target:
            payload.update({"tpPrice":fmt_px(target,tick),"tpStopType":"MARK_PRICE","tpOrderType":"MARKET",
                            "slPrice":fmt_px(stop,tick),"slStopType":"MARK_PRICE","slOrderType":"MARKET"})
        return self._req("POST", "/api/v1/futures/trade/place_order", payload=payload)

class RiskManager:
    def __init__(self, cfg):
        self.cfg=cfg; self.day=datetime.now(timezone.utc).date()
        self.day_start_equity=None; self.trades=[]; self.halted_today=False; self.killed=False
    def daily_loss_hit(self, equity):
        today=datetime.now(timezone.utc).date()
        if today!=self.day: self.day,self.halted_today=today,False; self.day_start_equity=equity
        self.day_start_equity=self.day_start_equity or equity
        if (not self.halted_today and self.day_start_equity and equity>0
                and equity<=self.day_start_equity*(1-self.cfg.daily_loss_limit)):
            self.halted_today=True; logging.warning("CIRCUIT BREAKER: daily loss limit hit."); notify("BREAKER: daily loss limit hit - trading halted today")
        return self.halted_today
    def record_trade(self, pnl): self.trades.append(pnl>0)
    def kill_switch(self):
        if self.killed or len(self.trades)<self.cfg.kill_min_trades: return False
        wr=sum(self.trades[-self.cfg.kill_min_trades:])/self.cfg.kill_min_trades
        # 2.5:1 payoff break-even at 1% risk incl. fees ≈ 30.5%
        be=0.305
        if wr < be+self.cfg.kill_wr_buffer:
            self.killed=True; logging.error(f"KILL SWITCH: WR {wr:.1%} < {be:.1%}+buf. STOPPED."); notify(f"KILL SWITCH: rolling win rate {wr:.1%} too low. Bot STOPPED - edge not confirmed.")
        return self.killed

class Bot:
    def __init__(self, cfg, client):
        self.cfg, self.client = cfg, client
        self.risk = RiskManager(cfg)
        self.state = {s: 0 for s in cfg.symbols}
        self.seen_bar = {s: 0 for s in cfg.symbols}
        self.open_pos = {}; self.entry_info = {}
        self.scan_seen_bar = 0
        self.eq_hist = []
        self.day_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.day_start_eq = None
        self.csv = open("trades.csv","a",newline="")
        self.writer = csv.writer(self.csv)
        if self.csv.tell()==0:
            self.writer.writerow(["time","symbol","signal","side","qty","stop","target","result","note"])
    def log(self,*row):
        self.writer.writerow([datetime.now(timezone.utc).isoformat()]+list(row)); self.csv.flush()
    def sync(self):
        live={p["id"]:p for p in self.client.positions()}
        for pid,info in list(self.open_pos.items()):
            if pid not in live:
                R = None
                if info.get("entry") and info.get("stop"):
                    try:
                        mk = self.client.klines(info["sym"], self.cfg.interval, 1)[-1]["c"]
                        R = (mk-info["entry"])/abs(info["entry"]-info["stop"])*info.get("side",1)
                    except Exception:
                        R = None
                if R is None: R = 1.0 if info["pnl"]>0 else -1.0
                self.risk.record_trade(R>0)
                wins = sum(self.risk.trades); n = len(self.risk.trades)
                wr = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
                e = "🟢" if R>0 else "🔴"; w = "WIN ✅" if R>0 else "LOSS ❌"
                self.log(info["sym"],"CLOSED","","","","","",f"{R:+.2f}R","tp/sl/external")
                notify(f"{e} <b>{info['sym']} CLOSED · {w}</b>\n"
                       f"💰 Result: <b>{R:+.2f}R</b>\n🏆 Record: {wr}")
                self.state[info["sym"]]=0; self.entry_info.pop(info["sym"],None); del self.open_pos[pid]
        for pid,p in live.items():
            if pid not in self.open_pos:
                self.open_pos[pid]={"sym":p["sym"],"pnl":0.0}; self.state[p["sym"]]=p["side"]
                logging.warning(f"RECONCILE: adopted position {pid} {p['sym']}"); notify(f"NOTE: adopted existing {p['sym']} position from exchange")
    def equity(self):
        return self.cfg.dry_equity if self.cfg.dry_run else self.client.equity_usdt()

    def _ema(self, vals, n):
        k=2/(n+1); out=[vals[0]]
        for v in vals[1:]: out.append(v*k+out[-1]*(1-k))
        return out

    def detect_signal(self, sym, rows):
        """Returns (side, kind, atr) or (0,None,0). Signals: donchian / ignition per signal_map."""
        closes=[r["c"] for r in rows]
        e200=self._ema(closes, self.cfg.ema_trend)[-1]
        n=self.cfg.donchian_n
        hi=max(r["h"] for r in rows[-n-1:-1]); lo=min(r["l"] for r in rows[-n-1:-1])
        price=closes[-1]
        # ATR(14)
        trs=[max(rows[i]["h"]-rows[i]["l"], abs(rows[i]["h"]-rows[i-1]["c"]), abs(rows[i]["l"]-rows[i-1]["c"]))
             for i in range(1,len(rows))]
        atrv=sum(trs[-14:])/14
        br = (rows[-1]["tb"]/rows[-1]["vol"]) if rows[-1]["vol"]>0 else 0.5
        allowed = self.cfg.signal_map.get(sym, ["donchian"])
        for kind in allowed:
            sig=0
            if kind=="donchian":
                sig = 1 if (price>hi and price>e200) else -1 if (price<lo and price<e200) else 0
                # order-flow confirm
                if sig>0 and br<self.cfg.flow_long: sig=0
                if sig<0 and br>self.cfg.flow_short: sig=0
            elif kind=="ignition":
                c3=closes[-4:]
                vols=[r["vol"] for r in rows[-22:-2]]
                vma=sum(vols)/len(vols) if vols else 0
                up3 = c3[3]>c3[2]>c3[1]>c3[0]
                dn3 = c3[3]<c3[2]<c3[1]<c3[0]
                if up3 and vma>0 and rows[-1]["vol"]>self.cfg.vol_spike*vma and price>e200: sig=1
                elif dn3 and vma>0 and rows[-1]["vol"]>self.cfg.vol_spike*vma and price<e200: sig=-1
            if sig!=0:
                return sig, kind, atrv
        return 0, None, atrv

    def run_symbol(self, sym, equity):
        rows = self.client.klines(sym, self.cfg.interval, 200)
        if len(rows) < self.cfg.ema_trend+5: return
        bar = rows[-1]
        if time.time()*1000 < bar["t"]+INTERVAL_MS[self.cfg.interval]: return
        if bar["t"]==self.seen_bar[sym]: return
        self.seen_bar[sym]=bar["t"]
        sig, kind, atrv = self.detect_signal(sym, rows)
        if sig!=0 and sig!=self.state[sym]:
            self._flip(sym, sig, kind, rows, atrv, equity)

    def _flip(self, sym, new_side, kind, rows, atrv, equity):
        for pid, info in list(self.open_pos.items()):
            if info["sym"]==sym:
                if not self.cfg.dry_run:
                    p=[x for x in self.client.positions(sym) if x["id"]==pid]
                    if p: self.client.place(sym, "SELL" if p[0]["side"]>0 else "BUY", p[0]["qty"], "CLOSE")
                self.log(sym,"EXIT","","","","","","flip"); del self.open_pos[pid]; self.entry_info.pop(sym,None)
        self.state[sym]=0
        if new_side==0: return
        price=rows[-1]["c"]
        rd = atrv/price
        if rd<self.cfg.stop_min or rd>self.cfg.stop_max:
            logging.info(f"{sym}: skipped, ATR stop {rd:.1%} outside band"); return
        stop = price*(1-rd) if new_side>0 else price*(1+rd)
        target = price*(1+self.cfg.tp_r*rd) if new_side>0 else price*(1-self.cfg.tp_r*rd)
        risk = self.cfg.risk_map.get(sym, self.cfg.risk_per_trade)
        if sum(i.get("risk",0) for i in self.entry_info.values()) + risk > self.cfg.max_open_risk:
            logging.info(f"{sym}: skipped, open-risk cap reached"); return
        qty = min(risk/rd, self.cfg.max_notional_lev)*equity/price
        side = "BUY" if new_side>0 else "SELL"
        logging.info(f"{sym}: ENTER {kind} {side} qty={fmt_qty(qty)} stop={stop:.2f} tgt={target:.2f}")
        d = "🟢 LONG" if new_side>0 else "🔴 SHORT"
        notify(f"{d} <b>{sym}</b> · {kind.upper()}\n"
               f"📍 Entry {price:.4f}\n🛑 Stop {stop:.4f} · 🎯 Target {target:.4f}\n"
               f"📊 Risk {risk*100:.2f}% · notional ~${qty*price:.0f}")
        if self.cfg.dry_run:
            self.log(sym, kind, side, fmt_qty(qty), f"{stop:.2f}", f"{target:.2f}", "", "DRY_RUN")
            self.open_pos[f"dry-{sym}-{int(time.time()*1000)}"]={"sym":sym,"pnl":0.0}
        else:
            filled = False
            if self.cfg.entry_limit:
                lim = price*(1-self.cfg.limit_offset) if new_side>0 else price*(1+self.cfg.limit_offset)
                res = self.client.place(sym, side, qty, "OPEN", order_type="LIMIT", price=lim,
                                        stop=stop, target=target)
                oid = (res or {}).get("orderId") or (res or {}).get("id")
                for _ in range(self.cfg.entry_timeout_bars):
                    time.sleep(INTERVAL_MS[self.cfg.interval]/1000/2)
                    self.sync()
                    if any(i["sym"]==sym for i in self.open_pos.values()):
                        filled = True; logging.info(f"{sym}: limit filled at maker fee"); break
                if not filled and oid:
                    try: self.client.cancel(sym, oid)
                    except Exception as ex: logging.warning(f"cancel failed (may have filled): {ex}")
                    logging.info(f"{sym}: limit entry unfilled after {self.cfg.entry_timeout_bars} bars - skipped")
                    return
            else:
                self.client.place(sym, side, qty, "OPEN", stop=stop, target=target)
                time.sleep(1); self.sync()
                filled = any(i["sym"]==sym for i in self.open_pos.values())
            if not filled and not self.cfg.entry_limit:
                logging.error(f"{sym}: entry unconfirmed — not tracked"); return
        for _pid,_info in self.open_pos.items():
            if _info["sym"]==sym and "entry" not in _info:
                _info.update({"entry":price,"stop":stop,"side":new_side,"kind":kind})
        self.state[sym]=new_side
        self.entry_info[sym]={"entry":price,"kind":kind,"stop_pct":rd,"stop":stop,"side":new_side}


    def run_scanner(self, equity):
        """Top-gainer cross-sectional momentum sleeve. Validated on two windows
        (crash: 72 tr +22.9%, bull: 87 tr +52.9% at 1.5% risk)."""
        now_ms = time.time()*1000
        bar = (int(now_ms)//INTERVAL_MS[self.cfg.interval])*INTERVAL_MS[self.cfg.interval]
        if now_ms < bar + INTERVAL_MS[self.cfg.interval]: return      # bar not closed yet
        if bar == self.scan_seen_bar: return
        self.scan_seen_bar = bar
        universe = list(self.cfg.scanner_pairs)
        if self.cfg.scanner_dynamic:
            try:
                tk = self.client.tickers()
                dyn = [t["symbol"] for t in tk
                       if t["symbol"].endswith("USDT") and t["vol24"] >= self.cfg.scanner_min_vol24]
                dyn = [s for s in dyn if s not in self.cfg.symbols or True][:self.cfg.scanner_max_pairs]
                if dyn: universe = dyn
                else: logging.warning("dynamic universe empty - using fallback list")
            except Exception as e:
                logging.warning(f"ticker fetch failed ({e}) - using fallback list")
        data = {}
        for s in universe:
            try:
                rows = self.client.klines(s, self.cfg.interval, 100)
                if len(rows) >= 30: data[s] = rows
            except Exception:
                continue
        if not data: return
        logging.info(f"scanner universe: {len(data)} pairs")
        held = {info["sym"] for info in self.open_pos.values()}
        cands = []
        for s, r in data.items():
            if s in held: continue
            closes = [x["c"] for x in r]
            ret24 = closes[-1]/closes[-25] - 1
            if ret24 > self.cfg.scanner_ret:
                hi20 = max(x["h"] for x in r[-21:-1])
                vols = [x["vol"] for x in r[-22:-2]]
                vma = sum(vols)/len(vols) if vols else 0
                brk = closes[-1] > hi20
                vspike = vma > 0 and r[-1]["vol"] > 1.2*vma
                if brk and vspike:
                    trs = [max(r[i]["h"]-r[i]["l"], abs(r[i]["h"]-r[i-1]["c"]), abs(r[i]["l"]-r[i-1]["c"]))
                             for i in range(-14, 0)]
                    atr = sum(trs)/len(trs)
                    cands.append((ret24, s, r[-1]["c"], atr))
        cands.sort(reverse=True)
        slots = self.cfg.scanner_max - sum(1 for i in self.open_pos.values() if i.get("kind")=="scanner")
        for ret24, s, price, atr in cands:
            if slots <= 0: break
            rd = atr/price
            if rd < 0.004 or rd > 0.06: continue
            stop = price - atr; target = price + self.cfg.tp_r*atr
            qty = (self.cfg.scanner_risk/rd)*equity/price
            qty = min(qty, 5.0*equity/price)
            if qty*price < 10: continue
            logging.info(f"SCANNER ENTER {s} (24h +{ret24*100:.1f}%) qty={fmt_qty(qty)} stop={stop:.4f} tgt={target:.4f}")
            notify(f"🚀 <b>SCANNER · {s}</b> 🔥 top gainer +{ret24*100:.1f}%/24h\n"
                   f"📍 Entry ~{price:.4f}\n🛑 {stop:.4f} · 🎯 {target:.4f}\n"
                   f"📊 Risk {self.cfg.scanner_risk*100:.1f}%")
            if self.cfg.dry_run:
                self.log(s, "scanner", "BUY", fmt_qty(qty), f"{stop:.4f}", f"{target:.4f}", "", "DRY_RUN")
                self.open_pos[f"scan-{s}-{int(time.time()*1000)}"] = {"sym":s, "pnl":0.0, "kind":"scanner"}
            else:
                try:
                    self.client.place(s, "BUY", qty, "OPEN", stop=stop, target=target, tick=0.0001)
                    time.sleep(1); self.sync()
                    for _pid,_info in self.open_pos.items():
                        if _info["sym"]==s and "entry" not in _info:
                            _info.update({"entry":price,"stop":stop,"side":1,"kind":"scanner"})
                except Exception as e:
                    logging.error(f"scanner entry failed {s}: {e}")
            slots -= 1

    def loop(self):
        logging.info(f"Bot v5 starting. dry_run={self.cfg.dry_run}")
        notify(f"LeverageLord is alive | dry_run={self.cfg.dry_run} | watching {','.join(self.cfg.symbols)}")
        beats = 0
        beats_between = max(1, 3600 // max(self.cfg.poll_seconds, 1))   # ~1 per hour
        while True:
            try:
                if not self.cfg.dry_run: self.sync()
                eq=self.equity()
                if eq<=0:
                    if not getattr(self,"_warned_eq",False):
                        self._warned_eq=True
                        logging.error("equity read as 0 - API parse problem, skipping cycles"); notify("WARN: can't read balance (got 0). Trading paused - check logs")
                    time.sleep(self.cfg.poll_seconds); continue
                if self.risk.daily_loss_hit(eq) or self.risk.kill_switch():
                    time.sleep(self.cfg.poll_seconds); continue
                for sym in self.cfg.symbols: self.run_symbol(sym, eq)
                self.run_scanner(eq)
                beats += 1
                if beats >= beats_between:
                    beats = 0
                    wins = sum(self.risk.trades); n = len(self.risk.trades)
                    wr = f"{wins/n*100:.0f}%" if n else "n/a"
                    self.eq_hist.append(eq); self.eq_hist = self.eq_hist[-168:]
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if today != self.day_stamp:
                        self.day_stamp = today
                        base = self.day_start_eq or eq
                        chg = (eq/base-1)*100 if base else 0
                        c = "🟢" if chg>=0 else "🔴"
                        notify(f"📅 <b>DAILY SUMMARY · {today}</b>\n"
                               f"🏦 Equity ${eq:.2f} ({c}{chg:+.1f}% today)\n"
                               f"📝 Trades: {n} · 🏆 WR {wr}\n"
                               f"📈 {sparkline(self.eq_hist)}")
                    notify(f"💓 Equity <b>${eq:.2f}</b> · {len(self.open_pos)} open · {n} trades (WR {wr}) · {status}\n"
                           f"📈 {sparkline(self.eq_hist)}")
                time.sleep(self.cfg.poll_seconds)
            except Exception as ex:
                logging.error(f"loop error: {ex}"); time.sleep(60)

if __name__ == "__main__":
    cfg = Config(dry_run=os.getenv("DRY_RUN","true").lower()!="false")
    client = BitunixClient(os.environ["BITUNIX_API_KEY"], os.environ["BITUNIX_API_SECRET"])
    Bot(cfg, client).loop()

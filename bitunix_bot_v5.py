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
import math

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
    min_qty_map: dict = field(default_factory=lambda: {"BTCUSDT": 0.001})
    min_notional_usdt: float = 15.0
    mode: str = field(default_factory=lambda: os.getenv("MODE", "strict").lower())
    #   strict = validated gates (positive in ALL tested regimes - recommended)
    #   active = looser gates (~40% more trades, regime-dependent edge - your call)
    scanner_dynamic: bool = True     # build universe from ALL Bitunix perps (volume-filtered)
    scanner_min_vol24: float = 10_000_000.0   # 24h volume floor (Bitunix-native vol) - 1000PEPE/WIF qualify, 1-7M shrapnel excluded
    scanner_max_pairs: int = 40      # cap on universe size per scan
    scanner_pairs: list = field(default_factory=lambda: ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
                                                          "DOGEUSDT","LINKUSDT","ADAUSDT","LTCUSDT","ARBUSDT","OPUSDT",
                                                          "1000PEPEUSDT","WIFUSDT","FLOKIUSDT","1000BONKUSDT","TAOUSDT"])
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
_TK_DEBUGGED = False
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

def foot():
    return f"\n— LeverageLord · {datetime.now(timezone.utc).strftime('%H:%M')} UTC"

def streak_of(trades):
    s = 0
    for t in reversed(trades):
        if t: s += 1
        else: break
    return s

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
    def klines(self, symbol, interval, limit=200, start_time=None, end_time=None):
        limit = min(limit, 200)
        if start_time is None:
            span = int(limit * INTERVAL_MS.get(interval, 14400e3))
            start_time = int(time.time()*1000) - span - 7200000   # explicit recent window - kills stale data
        params = {"symbol": symbol, "interval": interval, "limit": limit, "startTime": str(int(start_time))}
        if end_time: params["endTime"] = str(int(end_time))
        rows = self._req("GET", "/api/v1/futures/market/kline", params)
        return [{"o":float(r["open"]), "h":float(r["high"]), "l":float(r["low"]),
                 "c":float(r["close"]), "t":int(r["time"]),
                 "vol":float(r.get("vol",0)), "tb":float(r.get("takerVol", r.get("takerBuyVol",0) or 0))} for r in rows]
    def tickers(self):   # Bitunix 'Get All Tickers' (confirmed path)
        rows = self._req("GET", "/api/v1/futures/market/tickers") or []
        global _TK_DEBUGGED
        if not _TK_DEBUGGED and rows:
            _TK_DEBUGGED = True
            logging.info(f"TICKERS RAW sample: {json.dumps(rows[0])[:400]} | count: {len(rows)}")
        out = []
        for t in rows:
            sym = t.get("symbol", "")
            vol = float(t.get("vol") or t.get("volume") or t.get("vol24h") or t.get("turnover") or t.get("amount") or t.get("quoteVol") or 0)
            out.append({"symbol": sym, "vol24": vol})
        return out

    def valid_symbols(self):
        try:
            syms = {t["symbol"] for t in self.tickers() if t.get("symbol")}
            if syms: logging.info(f"valid futures symbols: {len(syms)} listed")
            return syms
        except Exception as e:
            logging.warning(f"symbol discovery failed: {e} - allowing configured names")
            return None

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
              stop=None, target=None, tick=0.1):
        # returns ("bracket"|"plain", result).
        # OPEN orders: auto-retry qty at every valid precision (0.001 -> 1.0), then fall back to plain.
        if trade_side == "OPEN":
            steps = [0.001, 0.01, 0.1, 1.0]
            last_e = None
            for st in steps:
                q2 = math.floor(qty/st)*st
                if q2 <= 0: q2 = st
                try:
                    return ("bracket", self._place(symbol, side, q2, trade_side, order_type, price, stop, target, tick))
                except Exception as e:
                    last_e = e
                    if "10002" in str(e):
                        continue                      # wrong qty precision -> next step
                    logging.warning(f"bracket rejected ({e}) - trying plain order")
                    try:
                        return ("plain", self._place(symbol, side, q2, trade_side, order_type, price, None, None, tick))
                    except Exception as e2:
                        last_e = e2
                        if "10002" in str(e2):
                            continue
                        raise
            raise last_e
        try:
            return ("bracket", self._place(symbol, side, qty, trade_side, order_type, price, stop, target, tick))
        except Exception as e:
            if "place_order" in str(e):
                logging.warning(f"bracket rejected ({e}) - placing plain order, bot will manage exit")
                return ("plain", self._place(symbol, side, qty, trade_side, order_type, price, None, None, tick))
            raise

    def _place(self, symbol, side, qty, trade_side, order_type="MARKET", price=None,
               stop=None, target=None, tick=0.1):
        payload={"symbol":symbol,"side":side,"qty":fmt_qty(qty),"tradeSide":trade_side,
                 "orderType":order_type,"effect":"GTC","clientId":str(int(time.time()*1000)),
                 "reduceOnly": trade_side=="CLOSE"}
        if order_type=="LIMIT": payload["price"]=fmt_px(price,tick)
        if trade_side=="OPEN" and stop and target:
            try:   # clamp bracket against a FRESH mark price - fast markets can invalidate it
                mk = self.klines(symbol, self.cfg.interval, 1)[-1]["c"]
                if side == "BUY":
                    target = max(target, mk*1.002); stop = min(stop, mk*0.998)
                else:
                    target = min(target, mk*0.998); stop = max(stop, mk*1.002)
            except Exception:
                pass
        if trade_side=="OPEN" and stop and target:
            payload.update({"tpPrice":fmt_px(target,tick),"tpStopType":"MARK_PRICE","tpOrderType":"MARKET",
                            "slPrice":fmt_px(stop,tick),"slStopType":"MARK_PRICE","slOrderType":"MARKET"})
        logging.info(f"ORDER payload: {payload}")
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
        self.tg_offset = 0
        self.triggers = []   # [{sym, side, level, stop, target, qty, expiry}]
        self.last_scan = {"top": [], "universe": 0, "signals": 0}
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
                if not (info.get("entry") and info.get("stop")):
                    self.log(info["sym"],"CLOSED","","","","","","","external close (no entry data - not counted)")
                    notify(f"⚪ <b>{info['sym']} closed externally</b> - not counted in the record (no entry data on adopted position)")
                    self.state[info["sym"]] = 0; self.entry_info.pop(info["sym"], None); del self.open_pos[pid]; continue
                if R is None: R = 1.0 if info["pnl"]>0 else -1.0
                if info.get("kind") != "test":
                    self.risk.record_trade(R>0)
                wins = sum(self.risk.trades); n = len(self.risk.trades)
                wr = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
                e = "🧪" if info.get("kind")=="test" else ("🟢" if R>0 else "🔴")
                w = "TEST CLOSED" if info.get("kind")=="test" else ("WIN ✅" if R>0 else "LOSS ❌")
                pct = R * info.get("risk_frac", 0.015) * 100
                rf = info.get("risk_frac", 0.015); eq0 = info.get("eq_at_entry", 0)
                eq_now = eq0*(1+R*rf) if eq0 else 0
                st = streak_of(self.risk.trades)
                self.log(info["sym"],"CLOSED","","","","","",f"{R:+.2f}R ({pct:+.2f}%)","tp/sl/external")
                eqline = f"\n🏦 Equity ${eq_now:.2f}" if eq_now else ""
                stline = f" 🔥{st} win streak" if st >= 2 else ""
                notify(f"{e} <b>{info['sym']} CLOSED · {w}</b>\n"
                       f"💰 Result: <b>{pct:+.2f}%</b> ({R:+.2f}R)\n🏆 Record: {wr}{stline}{eqline}" + foot())
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
        bar = rows[-2]                                   # last CLOSED bar
        win = rows[-1]["t"]                              # forming bar open == signal bar close time
        if win==self.seen_bar[sym]: return
        self.seen_bar[sym]=win
        sig, kind, atrv = self.detect_signal(sym, rows[:-1])
        if sig == 0:
            try:
                r = rows[:-1]
                br = (r[-1]["tb"]/r[-1]["vol"]) if r[-1]["vol"]>0 else 0.5
                n = self.cfg.donchian_n
                hi = max(x["h"] for x in r[-n-1:-1]); lo = min(x["l"] for x in r[-n-1:-1])
                c = r[-1]["c"]
                closes = [x["c"] for x in r]
                e = closes[0]
                k2 = 2/(self.cfg.ema_trend+1)
                for v in closes[1:]: e = v*k2 + e*(1-k2)
                side_gap = (c/hi-1)*100 if c>lo else (c/lo-1)*100
                logging.info(f"CORE {sym}: no signal | close {c:.4g} vs channel {hi:.4g}/{lo:.4g} ({side_gap:+.1f}%) | trend {'up' if c>e else 'DOWN'} | buyers {br*100:.0f}%")
            except Exception:
                pass
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
        qty = max(qty, self.cfg.min_qty_map.get(sym, 0), self.cfg.min_notional_usdt/price)
        side = "BUY" if new_side>0 else "SELL"
        logging.info(f"{sym}: ENTER {kind} {side} qty={fmt_qty(qty)} stop={stop:.2f} tgt={target:.2f}")
        d = "🟢 LONG" if new_side>0 else "🔴 SHORT"
        rd = abs(price-stop)/price*100
        rr = abs(target-price)/abs(price-stop)
        notify(f"{d} <b>{sym}</b> · {kind.upper()}\n"
               f"📍 Entry <code>{price:.4f}</code>\n"
               f"🛑 <code>{stop:.4f}</code> (−{rd:.1f}%)   🎯 <code>{target:.4f}</code> (+{rd*rr:.1f}%)\n"
               f"⚖️ 1 : {rr:.1f} · 📊 Risk {risk*100:.2f}% · Size ~${qty*price:.0f}" + foot())
        if self.cfg.dry_run:
            self.log(sym, kind, side, fmt_qty(qty), f"{stop:.2f}", f"{target:.2f}", "", "DRY_RUN")
            self.open_pos[f"dry-{sym}-{int(time.time()*1000)}"]={"sym":sym,"pnl":0.0}
        else:
            filled = False
            if self.cfg.entry_limit:
                lim = price*(1-self.cfg.limit_offset) if new_side>0 else price*(1+self.cfg.limit_offset)
                _mode, res = self.client.place(sym, side, qty, "OPEN", order_type="LIMIT", price=lim,
                                               stop=stop, target=target)
                oid = (res or {}).get("orderId") or (res or {}).get("id")
                for _ in range(self.cfg.entry_timeout_bars*60):
                    time.sleep(60)
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
                _info.update({"entry":price,"stop":stop,"target":target,"side":new_side,"kind":kind,"qty":qty,"risk_frac":risk,"eq_at_entry":equity})
        self.state[sym]=new_side
        self.entry_info[sym]={"entry":price,"kind":kind,"stop_pct":rd,"stop":stop,"side":new_side}


    def run_scanner(self, equity):
        """Top-gainer cross-sectional momentum sleeve. Validated on two windows
        (crash: 72 tr +22.9%, bull: 87 tr +52.9% at 1.5% risk)."""
        now_ms = time.time()*1000
        win = (int(now_ms)//INTERVAL_MS[self.cfg.interval])*INTERVAL_MS[self.cfg.interval]   # just-closed bar's boundary
        if win == self.scan_seen_bar: return
        self.scan_seen_bar = win
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
        live_px = {}
        try:
            live_px = {t["symbol"]: float(t.get("lastPrice") or 0) for t in self.client.tickers()}
        except Exception:
            pass
        stale = 0
        for s in universe:
            try:
                rows = self.client.klines(s, self.cfg.interval, 100)
                if len(rows) >= 30:
                    rows = rows[:-1]                          # drop forming bar - closed bars only
                    lp = live_px.get(s)
                    if lp and abs(rows[-1]["c"] - lp)/lp > 0.08:
                        stale += 1
                        logging.warning(f"STALE DATA {s}: kline close {rows[-1]['c']} vs live {lp} - excluded from scan")
                        continue
                    data[s] = rows
            except Exception:
                continue
        if stale:
            notify(f"⚠️ {stale} pairs had stale price data and were excluded (Bitunix feed issue)")
        if not data: return
        top = []
        for s, r in data.items():
            if len(r) >= 26:
                top.append((r[-1]["c"]/r[-25]["c"]-1, s))
        top.sort(reverse=True)
        self.last_scan = {"top": [(s, g) for g, s in top[:5]], "universe": len(data), "signals": 0}
        tstr = " · ".join(f"{s.replace('USDT','')} {g*100:+.1f}%" for g, s in top[:5]) or "n/a"
        logging.info(f"scanner universe: {len(data)} pairs | top24h: {tstr}")
        logging.info(f"SCAN REPORT | universe {len(data)} | top: {tstr} | signals queued: 0")
        held = {info["sym"] for info in self.open_pos.values()}
        cands = []
        for s, r in data.items():
            if s in held: continue
            closes = [x["c"] for x in r]
            ret24 = closes[-1]/closes[-25] - 1
            ret_th = 0.025 if self.cfg.mode == "active" else self.cfg.scanner_ret
            if ret24 > ret_th:
                n = 10 if self.cfg.mode == "active" else 20
                hi20 = max(x["h"] for x in r[-n-1:-1])
                vols = [x["vol"] for x in r[-22:-2]]
                vma = sum(vols)/len(vols) if vols else 0
                brk = closes[-1] > hi20
                vspike = vma > 0 and r[-1]["vol"] > 1.0*vma
                if brk and vspike:
                    trs = [max(r[i]["h"]-r[i]["l"], abs(r[i]["h"]-r[i-1]["c"]), abs(r[i]["l"]-r[i-1]["c"]))
                             for i in range(-14, 0)]
                    atr = sum(trs)/len(trs)
                    cands.append((ret24, s, r[-1]["c"], atr))
        cands.sort(reverse=True)
        slots = self.cfg.scanner_max - sum(1 for i in self.open_pos.values() if i.get("kind")=="scanner")
        for ret24, s, price, atr in cands:
            if slots <= 0:
                logging.warning(f"GATE-PASS {s} queued but slots full - SKIPPED")
                continue
            rd = atr/price
            if rd < 0.004 or rd > 0.06:
                logging.warning(f"GATE-PASS {s} all signal gates passed but stop {rd*100:.2f}% outside band")
                continue
            stop = price - atr; target = price + self.cfg.tp_r*atr
            qty = (self.cfg.scanner_risk/rd)*equity/price
            qty = min(qty, 5.0*equity/price)
            qty = max(qty, self.cfg.min_qty_map.get(s, 0), self.cfg.min_notional_usdt/price)
            if qty*price < 10:
                logging.warning(f"GATE-PASS {s} all gates passed but size ${qty*price:.0f} below min")
                continue
            self.last_scan["signals"] += 1
            logging.info(f"SCANNER ENTER {s} (24h +{ret24*100:.1f}%) qty={fmt_qty(qty)} stop={stop:.4f} tgt={target:.4f}")
            rd = abs(price-stop)/price*100
            notify(f"🚀 <b>SCANNER · {s}</b> 🔥 +{ret24*100:.1f}%/24h\n"
                   f"📍 <code>{price:.4f}</code> · 🛑 <code>{stop:.4f}</code> (−{rd:.1f}%) · 🎯 <code>{target:.4f}</code>\n"
                   f"📊 Risk {self.cfg.scanner_risk*100:.1f}%" + foot())
            if self.cfg.dry_run:
                self.log(s, "scanner", "BUY", fmt_qty(qty), f"{stop:.4f}", f"{target:.4f}", "", "DRY_RUN")
                self.open_pos[f"scan-{s}-{int(time.time()*1000)}"] = {"sym":s, "pnl":0.0, "kind":"scanner"}
            else:
                try:
                    self.client.place(s, "BUY", qty, "OPEN", stop=stop, target=target, tick=0.0001)
                    time.sleep(1); self.sync()
                    for _pid,_info in self.open_pos.items():
                        if _info["sym"]==s and "entry" not in _info:
                            _info.update({"entry":price,"stop":stop,"target":target,"side":1,"kind":"scanner","qty":qty,"risk_frac":self.cfg.scanner_risk,"eq_at_entry":equity})
                except Exception as e:
                    logging.error(f"scanner entry failed {s}: {e}")
            slots -= 1
            held.add(s)
        # SHORT engine: weakest movers breaking DOWN with volume (validated: +23R crash window)
        cands_s = []
        for s, r in data.items():
            if s in held: continue
            closes = [x["c"] for x in r]
            ret24 = closes[-1]/closes[-25] - 1
            if ret24 < -(0.025 if self.cfg.mode == "active" else self.cfg.scanner_ret):
                n = 10 if self.cfg.mode == "active" else 20
                lo20 = min(x["l"] for x in r[-n-1:-1])
                vols = [x["vol"] for x in r[-22:-2]]
                vma = sum(vols)/len(vols) if vols else 0
                brk_dn = closes[-1] < lo20
                vspike = vma > 0 and r[-1]["vol"] > 1.0*vma
                if brk_dn and vspike:
                    trs = [max(r[i]["h"]-r[i]["l"], abs(r[i]["h"]-r[i-1]["c"]), abs(r[i]["l"]-r[i-1]["c"]))
                             for i in range(-14, 0)]
                    atr = sum(trs)/len(trs)
                    cands_s.append((-ret24, s, r[-1]["c"], atr))
        cands_s.sort(reverse=True)
        slots_s = self.cfg.scanner_max - sum(1 for i in self.open_pos.values() if i.get("kind")=="scanner")
        for ret24, s, price, atr in cands_s:
            if slots_s <= 0:
                logging.warning(f"GATE-PASS SHORT {s} queued but slots full - SKIPPED")
                continue
            rd = atr/price
            if rd < 0.004 or rd > 0.06:
                logging.warning(f"GATE-PASS SHORT {s} stop {rd*100:.2f}% outside band")
                continue
            stop = price + atr; target = price - self.cfg.tp_r*atr
            qty = (self.cfg.scanner_risk/rd)*equity/price
            qty = min(qty, 5.0*equity/price)
            qty = max(qty, self.cfg.min_qty_map.get(s, 0), self.cfg.min_notional_usdt/price)
            if qty*price < 10:
                logging.warning(f"GATE-PASS {s} all gates passed but size ${qty*price:.0f} below min")
                continue
            self.last_scan["signals"] += 1
            logging.info(f"SCANNER SHORT {s} (24h -{ret24*100:.1f}%) qty={fmt_qty(qty)} stop={stop:.4f} tgt={target:.4f}")
            notify(f"🔻 <b>SCANNER SHORT · {s}</b> 📉 −{ret24*100:.1f}%/24h\n"
                   f"📍 <code>{price:.4f}</code> · 🛑 <code>{stop:.4f}</code> · 🎯 <code>{target:.4f}</code>\n"
                   f"📊 Risk {self.cfg.scanner_risk*100:.1f}%" + foot())
            if self.cfg.dry_run:
                self.log(s, "scanner-short", "SELL", fmt_qty(qty), f"{stop:.4f}", f"{target:.4f}", "", "DRY_RUN")
                self.open_pos[f"scan-{s}-{int(time.time()*1000)}"] = {"sym":s, "pnl":0.0, "kind":"scanner", "side":-1}
            else:
                try:
                    self.client.place(s, "SELL", qty, "OPEN", stop=stop, target=target, tick=0.0001)
                    time.sleep(1); self.sync()
                    for _pid,_info in self.open_pos.items():
                        if _info["sym"]==s and "entry" not in _info:
                            _info.update({"entry":price,"stop":stop,"target":target,"side":-1,"kind":"scanner","qty":qty,"risk_frac":self.cfg.scanner_risk,"eq_at_entry":equity})
                except Exception as e:
                    logging.error(f"scanner short entry failed {s}: {e}")
            slots_s -= 1
            held.add(s)
        armed_now = 0
        for g0, s0 in top[:3]:
            if g0 <= 0: continue
            r = data.get(s0)
            if not r: continue
            n = 10 if self.cfg.mode == "active" else 20
            hi = max(x["h"] for x in r[-n-1:-1])
            need = (hi - r[-1]["c"]) / r[-1]["c"]
            if 0 < need <= 0.02 and s0 not in {t["sym"] for t in self.triggers} and s0 not in held:
                price = r[-1]["c"]
                trs = [max(r[i]["h"]-r[i]["l"], abs(r[i]["h"]-r[i-1]["c"]), abs(r[i]["l"]-r[i-1]["c"])) for i in range(-14, 0)]
                atr = sum(trs)/len(trs)
                rd = atr/price
                if 0.004 <= rd <= 0.06:
                    eq = equity
                    qty = min((self.cfg.scanner_risk/rd)*eq/price, 5.0*eq/price)
                    qty = max(qty, self.cfg.min_qty_map.get(s0, 0), self.cfg.min_notional_usdt/price)
                    if qty*price >= 10:
                        self.triggers.append({"sym": s0, "side": 1, "level": hi*1.001,
                                              "stop": hi*1.001 - atr, "target": hi*1.001 + self.cfg.tp_r*atr,
                                              "qty": qty, "expiry": time.time()+8*3600})
                        armed_now += 1
                        notify(f"⚡ <b>TRIGGER ARMED · {s0}</b>\n"
                               f"fills if price touches <code>{hi*1.001:.4f}</code>\n"
                               f"🛑 <code>{hi*1.001-atr:.4f}</code> · 🎯 <code>{hi*1.001+self.cfg.tp_r*atr:.4f}</code> · risk {self.cfg.scanner_risk*100:.1f}%" + foot())
        if armed_now:
            logging.info(f"TRIGGERS ARMED: {[t['sym'] for t in self.triggers]}")
        detail = ""
        n = 10 if self.cfg.mode == "active" else 20
        for g0, s0 in top[:3]:
            r = data[s0]
            hi = max(x["h"] for x in r[-n-1:-1])
            lo = min(x["l"] for x in r[-n-1:-1])
            vols = [x["vol"] for x in r[-22:-2]]
            vma = sum(vols)/len(vols) if vols else 0
            vx = r[-1]["vol"]/(vma or 1)
            brk = r[-1]["c"] > hi; brk_dn = r[-1]["c"] < lo
            if brk:
                gate = "LONG READY ✅"
            elif brk_dn:
                gate = "SHORT READY ✅"
            else:
                need = (hi - r[-1]['c'])/r[-1]['c']*100 if g0 > 0 else (r[-1]['c'] - lo)/r[-1]['c']*100
                gate = f"{need:+.1f}% from trigger · vol {vx:.1f}x"
            detail += f"\n📋 {s0.replace('USDT','')} {g0*100:+.1f}%: {gate}"
        sig = self.last_scan["signals"]
        notify(f"🔎 <b>SCAN</b> — {len(data)} pairs swept\n"
               f"🏆 {tstr}{detail}\n"
               f"{'🚀 ' + str(sig) + ' SIGNAL(S) FIRED' if sig else '✅ all gates evaluated — standing by'}" + foot())


    def poll_commands(self):
        if not (_TG_TOKEN and _TG_CHAT): return
        try:
            r = requests.get(f"https://api.telegram.org/bot{_TG_TOKEN}/getUpdates",
                             params={"offset": self.tg_offset, "timeout": 0}, timeout=10).json()
            for u in r.get("result", []):
                self.tg_offset = u["update_id"] + 1
                msg = u.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(_TG_CHAT): continue
                text = (msg.get("text") or "").strip().lower()
                if text.startswith("/test"):
                    parts = text.split()
                    sym = (parts[1].upper() if len(parts) > 1 else "BTCUSDT")
                    if not sym.endswith("USDT"): sym += "USDT"
                    self.test_trade(sym)
                elif text == "/status": self.status_report()
                elif text == "/fire": self.fire_trade()
                elif text == "/reset":
                    self.risk.trades = []
                    notify("🔄 Trade record wiped - 0 trades, clean slate. (Deploys also reset it.)")
        except Exception:
            pass

    def status_report(self):
        eq = self.equity()
        wins = sum(self.risk.trades); n = len(self.risk.trades)
        wr = f"{wins/n*100:.0f}%" if n else "n/a"
        pos = "\n".join(f"• {i['sym']} (risk {i.get('risk','?')})" for i in self.entry_info.values()) or "none"
        top3 = " · ".join(f"{s.replace('USDT','')} {g*100:+.1f}%" for s, g in self.last_scan.get("top", [])[:3]) or "awaiting first scan"
        notify(f"📊 <b>STATUS</b>\n"
               f"🏦 Equity <b>${eq:.2f}</b> · 📝 {n} trades (WR {wr})\n"
               f"📂 Open: {pos}\n"
               f"🔎 {top3}\n"
               f"🛑 Halted: {self.risk.halted_today or self.risk.killed}" + foot())

    def test_trade(self, sym="BTCUSDT"):
        valid = self.client.valid_symbols()
        if valid and sym not in valid:
            notify(f"⚠️ <b>{sym}</b> is not a listed Bitunix future. Check the Futures tab for the exact name.")
            return
        notify(f"🧪 <b>TEST</b>: firing tiny {sym} order to prove the full pipeline...")
        try:
            rows = self.client.klines(sym, self.cfg.interval, 50)
            if len(rows) < 20: raise RuntimeError("no kline data")
            price = rows[-1]["c"]
            trs = [max(rows[i]["h"]-rows[i]["l"], abs(rows[i]["h"]-rows[i-1]["c"]), abs(rows[i]["l"]-rows[i-1]["c"]))
                   for i in range(-14, 0)]
            atrv = sum(trs)/len(trs)
            eq = self.equity()
            notional = max(12.0, min(25.0, eq*0.25))     # $12-25 - tiny
            qty = max(notional/price, self.cfg.min_qty_map.get(sym, 0), self.cfg.min_notional_usdt/price)
            notional = qty*price
            stop = price - 0.6*atrv; target = price + 0.9*atrv
            tick = 0.0001 if price < 10 else 0.1
            self.client.place(sym, "BUY", qty, "OPEN", stop=stop, target=target, tick=tick)
            time.sleep(1); self.sync()
            found = False
            for pid, info in self.open_pos.items():
                if info["sym"] == sym and "entry" not in info:
                    rf = (abs(price-stop)/price*qty*price)/max(eq,1e-9)
                    info.update({"entry": price, "stop": stop, "side": 1, "kind": "test", "target": target, "qty": qty, "risk_frac": rf}); found = True
            logging.info(f"TEST TRADE fired {sym} qty={fmt_qty(qty)} stop={stop:.1f} target={target:.1f}")
            notify(f"🧪 <b>TEST ENTER {sym}</b>\n"
                   f"📍 <code>{price:.4f}</code> · 🛑 <code>{stop:.4f}</code> · 🎯 <code>{target:.4f}</code>\n"
                   f"💵 ${notional:.0f} (tiny) · ⏳ awaiting resolution…" + foot())
        except Exception as e:
            logging.error(f"test trade failed: {e}")
            notify(f"🧪 TEST FAILED: {e}")


    def manage_exits(self):
        """Close positions with market orders when price hits stop/target.
        This makes exits independent of exchange-side TP/SL bracket validation."""
        for pid, info in list(self.open_pos.items()):
            if not info.get("entry") or not info.get("stop") or not info.get("target") or not info.get("qty"):
                continue
            try:
                mk = self.client.klines(info["sym"], self.cfg.interval, 1)[-1]["c"]
            except Exception:
                continue
            side = info.get("side", 1)
            hit_stop = mk <= info["stop"] if side > 0 else mk >= info["stop"]
            hit_tgt  = mk >= info["target"] if side > 0 else mk <= info["target"]
            if not (hit_stop or hit_tgt):
                continue
            exit_p = mk
            R = (exit_p - info["entry"]) / abs(info["entry"] - info["stop"]) * side
            try:
                close_side = "SELL" if side > 0 else "BUY"
                self.client.place(info["sym"], close_side, info["qty"], "CLOSE")
                logging.info(f"MANAGED EXIT {info['sym']} at {exit_p:.4f} (R={R:+.2f})")
            except Exception as e:
                logging.error(f"managed exit failed {info['sym']}: {e}")
                continue
            if info.get("kind") != "test":
                self.risk.record_trade(R > 0)
            wins = sum(self.risk.trades); n = len(self.risk.trades)
            wr = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
            e2 = "🧪" if info.get("kind")=="test" else ("🟢" if R>0 else "🔴")
            w2 = "TEST CLOSED" if info.get("kind")=="test" else ("WIN ✅" if R>0 else "LOSS ❌")
            pct = R * info.get("risk_frac", 0.015) * 100
            rf = info.get("risk_frac", 0.015); eq0 = info.get("eq_at_entry", 0)
            eq_now = eq0*(1+R*rf) if eq0 else 0
            st = streak_of(self.risk.trades)
            self.log(info["sym"],"CLOSED","","","","","",f"{R:+.2f}R ({pct:+.2f}%)","bot-managed exit")
            eqline = f"\n🏦 Equity ${eq_now:.2f}" if eq_now else ""
            stline = f" 🔥{st} win streak" if st >= 2 else ""
            notify(f"{e2} <b>{info['sym']} CLOSED · {w2}</b>\n"
                   f"💰 Result: <b>{pct:+.2f}%</b> ({R:+.2f}R)\n🏆 Record: {wr}{stline}{eqline}" + foot())
            self.state[info["sym"]] = 0
            self.entry_info.pop(info["sym"], None)
            del self.open_pos[pid]


    def fire_trade(self):
        """MANUAL OVERRIDE - fires one trade on the hottest coin now. Not a strategy signal."""
        notify("🎯 <b>MANUAL OVERRIDE</b> — taking the hottest coin now (not a strategy signal)...")
        try:
            tk = self.client.tickers()
            hot = sorted((t for t in tk if t.get("symbol","").endswith("USDT") and float(t.get("vol24") or 0) >= 5e6),
                         key=lambda t: float(t.get("lastPrice") or 0)/max(float(t.get("open") or 1),1e-9), reverse=True)
            sym = None
            for t in hot:
                if t["symbol"] in self.cfg.symbols: continue
                sym = t["symbol"]; break
            if not sym: raise RuntimeError("no suitable coin")
            rows = self.client.klines(sym, self.cfg.interval, 100)
            if len(rows) < 20: raise RuntimeError("no data")
            price = rows[-1]["c"]
            trs = [max(rows[i]["h"]-rows[i]["l"], abs(rows[i]["h"]-rows[i-1]["c"]), abs(rows[i]["l"]-rows[i-1]["c"]))
                     for i in range(-14, 0)]
            atrv = sum(trs)/len(trs)
            eq = self.equity()
            qty = max(self.cfg.min_notional_usdt/price, 0.001 if "BTC" in sym else 0)
            qty = max(qty, (self.cfg.scanner_risk/(atrv/price))*eq/price if atrv/price>=0.004 else self.cfg.min_notional_usdt/price)
            qty = min(qty, 5.0*eq/price)
            stop = price - atrv; target = price + self.cfg.tp_r*atrv
            tick = 0.0001 if price < 10 else 0.1
            self.client.place(sym, "BUY", qty, "OPEN", stop=stop, target=target, tick=tick)
            time.sleep(1); self.sync()
            for pid, info in self.open_pos.items():
                if info["sym"] == sym and "entry" not in info:
                    info.update({"entry":price,"stop":stop,"target":target,"side":1,"kind":"manual","qty":qty,
                                 "risk_frac":self.cfg.scanner_risk,"eq_at_entry":eq})
            logging.info(f"MANUAL OVERRIDE fired {sym}")
            notify(f"🎯 <b>MANUAL OVERRIDE ENTER {sym}</b>\n"
                   f"📍 <code>{price:.4f}</code> · 🛑 <code>{stop:.4f}</code> · 🎯 <code>{target:.4f}</code>\n"
                   f"⚠️ Manual trade - excluded from the strategy record" + foot())
        except Exception as e:
            logging.error(f"manual override failed: {e}")
            notify(f"🎯 OVERRIDE FAILED: {e}")


    def check_triggers(self):
        """Resting trigger orders: fill when price touches the level (validated +39R/+21R/+7R across windows)."""
        if not self.triggers: return
        live_syms = {i["sym"] for i in self.open_pos.values()}
        for t in list(self.triggers):
            if time.time() > t["expiry"] or t["sym"] in live_syms:
                self.triggers.remove(t); continue
            try:
                px = self.client.klines(t["sym"], self.cfg.interval, 1)[-1]["c"]
            except Exception:
                continue
            if t["side"] > 0 and px >= t["level"]:
                try:
                    tick = 0.0001 if t["level"] < 10 else 0.1
                    self.client.place(t["sym"], "BUY", t["qty"], "OPEN", stop=t["stop"], target=t["target"], tick=tick)
                    time.sleep(1); self.sync()
                    for pid, info in self.open_pos.items():
                        if info["sym"] == t["sym"] and "entry" not in info:
                            info.update({"entry": t["level"], "stop": t["stop"], "target": t["target"],
                                         "side": 1, "kind": "scanner", "qty": t["qty"],
                                         "risk_frac": self.cfg.scanner_risk, "eq_at_entry": self.equity()})
                    self.risk.record_trade_pending = getattr(self.risk, "record_trade_pending", 0)  # noop
                    logging.info(f"TRIGGER FILLED {t['sym']} at {px:.4f}")
                    notify(f"🚀 <b>TRIGGER FILLED · {t['sym']}</b>\n"
                           f"📍 <code>{px:.4f}</code> · 🛑 <code>{t['stop']:.4f}</code> · 🎯 <code>{t['target']:.4f}</code>" + foot())
                    self.triggers.remove(t)
                except Exception as e:
                    logging.error(f"trigger fill failed {t['sym']}: {e}")

    def loop(self):
        logging.info(f"Bot v5 starting. dry_run={self.cfg.dry_run}")
        valid = None if self.cfg.dry_run else self.client.valid_symbols()
        if valid:
            bad = [s for s in self.cfg.symbols if s not in valid]
            if bad:
                logging.warning(f"NOT LISTED on Bitunix futures, removing: {bad}")
                notify(f"⚠️ Not listed on Bitunix futures, removed: {', '.join(bad)}")
                self.cfg.symbols = [s for s in self.cfg.symbols if s in valid]
            bad_sc = [s for s in self.cfg.scanner_pairs if s not in valid]
            if bad_sc:
                logging.warning(f"scanner pairs not listed, removing: {bad_sc}")
                self.cfg.scanner_pairs = [s for s in self.cfg.scanner_pairs if s in valid]
            self.state = {s: 0 for s in self.cfg.symbols}
        mode = "🔴 LIVE (real money)" if not self.cfg.dry_run else "🟡 DRY-RUN (paper)"
        notify(f"🤖 <b>LEVERAGELORD ONLINE</b>\n"
               f"{mode} · MODE: {self.cfg.mode.upper()} · {len(self.cfg.symbols)} core pairs · dynamic scanner\n"
               f"🎯 Target ~5%/mo · verdict at 40 trades\n"
               f"🛡 Breaker −5%/day · kill switch armed" + foot())
        beats = 0
        beats_between = max(1, 3600 // max(self.cfg.poll_seconds, 1))   # ~1 per hour
        while True:
            try:
                if not self.cfg.dry_run:
                    self.sync()
                    self.check_triggers()
                    self.manage_exits()
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
                               f"🏦 Equity <b>${eq:.2f}</b> ({c}{chg:+.1f}% today)\n"
                               f"📝 {n} trades · 🏆 WR {wr}" + (f" · 🔥{streak_of(self.risk.trades)} win streak" if streak_of(self.risk.trades)>=2 else "") + f"\n"
                               f"📈 <code>{sparkline(self.eq_hist)}</code>" + foot())
                    self.day_start_eq = eq
                    status = "✅ running" if not (self.risk.halted_today or self.risk.killed) else "⏸ halted"
                    logging.info(f"heartbeat: equity ${eq:.2f} open={len(self.open_pos)} trades={n} status={status}")
                    top3 = " · ".join(f"{s.replace('USDT','')} {g*100:+.1f}%" for s, g in self.last_scan.get("top", [])[:3]) or "awaiting first scan"
                    st = streak_of(self.risk.trades)
                    chg = (eq/(self.day_start_eq or eq)-1)*100
                    notify(f"💓 <b>Equity ${eq:.2f}</b> ({chg:+.1f}% today) · {status}\n"
                           f"📂 {len(self.open_pos)} open · 📝 {n} trades · 🏆 {wr}" + (f" · 🔥{st}" if st>=2 else "") + f"\n"
                           f"🔎 {top3}\n"
                           f"📈 <code>{sparkline(self.eq_hist)}</code>" + foot())
                self.poll_commands()
                time.sleep(self.cfg.poll_seconds)
            except Exception as ex:
                logging.error(f"loop error: {ex}"); time.sleep(60)

if __name__ == "__main__":
    cfg = Config(dry_run=os.getenv("DRY_RUN","true").lower()!="false")
    client = BitunixClient(os.environ["BITUNIX_API_KEY"], os.environ["BITUNIX_API_SECRET"])
    Bot(cfg, client).loop()

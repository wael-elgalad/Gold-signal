#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EASY MONEY PRO - محرك الاختبار التاريخي (نسخة الأوامر المعلّقة)
حدث-بحدث على شموع M15 بترتيب زمني صحيح لكل شمعة:
  (1) افتتاح الشمعة → إغلاق نهاية الأسبوع إذا لزم
  (2) الأوامر المعلّقة: تنفيذ/انتهاء/إلغاء + الإشارة السوقية عند الافتتاح
  (3) إدارة الصفقة داخل الشمعة: وقف أولاً (متحفظ) ثم الأهداف بالترتيب
  (4) إغلاق الشمعة → خروج الصفقة الخاملة + تحديث التعادل/الوقف المتحرك
  (5) توليد إشارة جديدة من إغلاق الشمعة (سوقية أو معلّقة عند إعادة الاختبار)

قواعد متحفظة معلنة:
- إذا لمست الشمعة الوقف وأي هدف معاً → يُحتسب الوقف أولاً.
- نقل الوقف للتعادل/التحريك يسري من الشمعة التالية لتحقق الشرط.
- الأوامر المعلّقة: تنتهي بعد LIMIT_EXPIRE_BARS، وتُلغى إذا أغلق السعر
  عكسياً بعيداً عنها (فشل الهيكل)، وتُرفض التعبئة خارج نافذة الجلسة.
"""
import numpy as np
import pandas as pd
from collections import deque

import config as C


class Trade:
    __slots__ = ("sig_time", "entry_time", "long", "grade", "setup", "regime",
                 "entry", "size", "sl", "sl_init", "risk_dist", "tps", "tp_hits",
                 "fractions", "exit_time", "exit_reasons", "realized", "fees",
                 "bars_held", "score", "entry_equity", "trail_active", "be_active",
                 "highest_close", "lowest_close", "partial_pnls", "sl_moves",
                 "entry_style")

    def __init__(self, sig, entry_time, entry_price, size, equity):
        self.sig_time = sig["time"]
        self.entry_time = entry_time
        self.long = sig["long"]
        self.grade = sig["grade"]
        self.setup = sig["setup"]
        self.regime = sig["regime"]
        self.score = sig["score"]
        self.entry_style = sig.get("style", "market")
        self.entry = entry_price
        self.size = size
        self.risk_dist = abs(entry_price - sig["sl_ref"]) or 1e-9
        self.sl = sig["sl_ref"]
        self.sl_init = sig["sl_ref"]
        self.tps = [entry_price + (self.risk_dist * r if self.long else -self.risk_dist * r)
                    for r in sig["tp_r"]]
        self.tp_hits = [False] * 5
        self.fractions = C.TP_FRACTIONS[:]
        self.exit_time = None
        self.exit_reasons = []
        self.realized = 0.0
        self.fees = 0.0
        self.bars_held = 0
        self.entry_equity = equity
        self.trail_active = False
        self.be_active = False
        self.highest_close = entry_price
        self.lowest_close = entry_price
        self.partial_pnls = []
        self.sl_moves = []

    def remaining(self):
        return 1.0 - sum(self.fractions[i] for i in range(5) if self.tp_hits[i])

    def pnl(self, price):
        d = (price - self.entry) if self.long else (self.entry - price)
        return d * self.size * self.remaining()

    def close_part(self, t, price, frac, reason, fee):
        pnl = ((price - self.entry) if self.long else (self.entry - price)) * self.size * frac
        self.realized += pnl - fee
        self.fees += fee
        self.partial_pnls.append((t, price, frac, reason))
        return pnl - fee


class Backtester:
    def __init__(self, ctx: pd.DataFrame, costs=None, risk_aplus=None, risk_b=None):
        self.ctx = ctx
        self.costs = costs if costs is not None else dict(
            spread=C.COST_SPREAD, commission=C.COST_COMMISSION, slippage=C.COST_SLIPPAGE)
        self.risk_aplus = risk_aplus if risk_aplus is not None else C.RISK_APLUS
        self.risk_b = risk_b if risk_b is not None else C.RISK_B

    def _fee(self, kind):
        c = self.costs
        if kind == "entry":
            return c["spread"] + c["commission"] / 2 + c["slippage"]
        if kind == "tp":
            return c["commission"] / 2
        return c["commission"] / 2 + c["slippage"]

    def run(self, start=None, end=None):
        from signals import evaluate_signal

        df = self.ctx
        tz = df.index.tz
        if start is not None:
            df = df[df.index >= pd.Timestamp(start, tz=tz)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end, tz=tz)]
        if len(df) < 300:
            raise ValueError("بيانات غير كافية للاختبار")

        o = df["open"].values
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        times = df.index
        atr = df["atr"].values
        h1_close = df["h1_close"].values
        h1_ema21 = df["h1_ema21"].values

        cash = C.INITIAL_CAPITAL
        cash_ref = [cash]   # مرجع قابل للتعديل داخل الدوال المساعدة
        state = {"trade": None, "pending": None}
        # ── خانق المخاطرة التكيفي: يراقب أداء النظام نفسه ──
        recent_r = deque(maxlen=C.THROTTLE_WINDOW)

        def risk_multiplier():
            """1.0 عند الأداء الجيد، أقل عند التراجع، وصفقات استطلاع عند النزيف الحاد."""
            if not C.THROTTLE_ENABLED or len(recent_r) < C.THROTTLE_WINDOW:
                return 1.0
            m = sum(recent_r) / len(recent_r)
            if m <= C.THROTTLE_PAUSE_R:
                return C.THROTTLE_PAUSE_MULT
            if m <= C.THROTTLE_LOW_R:
                return C.THROTTLE_LOW_MULT
            if m <= C.THROTTLE_MID_R:
                return C.THROTTLE_MID_MULT
            return 1.0
        trades = []
        equity_curve = []
        day_pnl = {}
        day_trades = {}
        week_trades = {}
        last_loss_dir_bar = {}
        last_exit_bar = {}
        stats = dict(signals=0, blocked_session=0, blocked_cooldown=0,
                     blocked_daily_stop=0, blocked_limits=0, blocked_sl_wide=0,
                     blocked_in_trade=0, reversals=0, weekend_exits=0,
                     stale_exits=0, time_exits=0, be_moves=0, trail_moves=0, tp_fills=[0] * 5,
                     limit_placed=0, limit_filled=0, limit_expired=0,
                     limit_cancelled=0, limit_no_session=0, market_entries=0,
                     throttle_blocks=0, throttle_reductions=0)

        def finalize(tr, i, t, day):
            trades.append(tr)
            day_pnl[day] = day_pnl.get(day, 0.0) + tr.realized
            # تغذية خانق المخاطرة بأداء الصفقة (بوحدات R)
            risk_usd = tr.size * tr.risk_dist
            recent_r.append(tr.realized / risk_usd if risk_usd > 0 else 0.0)
            d = 1 if tr.long else -1
            if tr.realized < 0:
                last_loss_dir_bar[d] = i
            last_exit_bar[d] = i

        def gates_ok(sig, i, t, hour, weekday, day, week):
            """بوابات الانضباط عند التعبئة/التنفيذ."""
            in_session = (C.ENTRY_HOUR_START <= hour <= C.ENTRY_HOUR_END and weekday <= 4)
            weekend_block = C.NO_ENTRIES_WEEKEND and (
                weekday >= 5 or (weekday == 4 and hour >= C.FLAT_FRIDAY_HOUR))
            d = 1 if sig["long"] else -1
            if weekend_block or not in_session:
                stats["blocked_session"] += 1
                return False
            if day_trades.get(day, 0) >= C.MAX_TRADES_PER_DAY or \
                    week_trades.get(week, 0) >= C.MAX_TRADES_PER_WEEK:
                stats["blocked_limits"] += 1
                return False
            if day_pnl.get(day, 0.0) <= -C.DAILY_STOP_PCT * cash_ref[0]:
                stats["blocked_daily_stop"] += 1
                return False
            if (d in last_loss_dir_bar and i - last_loss_dir_bar[d] < C.COOLDOWN_AFTER_LOSS_BARS) or \
                    (d in last_exit_bar and i - last_exit_bar[d] < C.COOLDOWN_AFTER_EXIT_BARS):
                stats["blocked_cooldown"] += 1
                return False
            return True

        def try_enter(sig, fill_price, i, t, day, week):
            """إنشاء الصفقة بعد اجتياز البوابات وفحص مسافة الوقف عند سعر التعبئة."""
            mult = risk_multiplier()
            if mult < 1.0:
                stats["throttle_reductions"] += 1
            sl_dist = abs(fill_price - sig["sl_ref"])
            if sl_dist < C.SL_MIN_ATR * sig["atr"] or sl_dist > C.SL_MAX_ATR * sig["atr"]:
                stats["blocked_sl_wide"] += 1
                return
            risk_pct = min(self.risk_aplus if sig["grade"] == "A+" else self.risk_b, C.MAX_RISK)
            risk_pct = risk_pct * mult
            size = (cash_ref[0] * risk_pct) / sl_dist
            size = np.floor(size * 100) / 100.0
            size = min(size, cash_ref[0] * C.MAX_LEVERAGE / fill_price)
            if size < C.MIN_UNIT:
                return
            tr = Trade(sig, t, fill_price, size, cash)
            fee = size * self._fee("entry")
            cash_ref[0] -= fee
            tr.fees += fee
            state["trade"] = tr
            day_trades[day] = day_trades.get(day, 0) + 1
            week_trades[week] = week_trades.get(week, 0) + 1
            if sig.get("style") == "limit":
                stats["limit_filled"] += 1
            else:
                stats["market_entries"] += 1

        def manage_in_bar(i, t, day):
            """إدارة الصفقة داخل الشمعة: وقف ثم أهداف. تعيد True إذا خرجت الصفقة."""
            tr = state["trade"]
            tr.bars_held += 1
            # أ) وقف الخسارة أولاً (قاعدة متحفظة)
            if (tr.long and l[i] <= tr.sl) or (not tr.long and h[i] >= tr.sl):
                rem = tr.remaining()
                fee = tr.size * rem * self._fee("stop")
                cash_ref[0] += tr.close_part(t, tr.sl, rem, "SL", fee)
                tr.exit_time = t
                reason = "SL_TRAIL" if tr.trail_active else ("SL_BE" if tr.be_active else "SL")
                tr.exit_reasons.append(reason)
                finalize(tr, i, t, day)
                state["trade"] = None
                return True
            # ب) الأهداف الجزئية بالترتيب
            for k in range(5):
                if tr.tp_hits[k]:
                    continue
                tp = tr.tps[k]
                if (tr.long and h[i] >= tp) or (not tr.long and l[i] <= tp):
                    frac = tr.fractions[k]
                    fee = tr.size * frac * self._fee("tp")
                    cash_ref[0] += tr.close_part(t, tp, frac, f"TP{k+1}", fee)
                    tr.tp_hits[k] = True
                    stats["tp_fills"][k] += 1
                    if k + 1 == C.BE_TRIGGER_TP and not tr.be_active:
                        tr.be_active = True
                        stats["be_moves"] += 1
                    if k + 1 == C.TRAIL_TRIGGER_TP and not tr.trail_active:
                        tr.trail_active = True
                        stats["trail_moves"] += 1
            if tr.remaining() < 1e-9:
                tr.exit_time = t
                tr.exit_reasons.append("ALL_TP")
                finalize(tr, i, t, day)
                state["trade"] = None
                return True
            return False

        def manage_on_close(i, t, day):
            """الصفقة الخاملة + تحديث الوقف بعد الإغلاق."""
            tr = state["trade"]
            # وقف زمني صريح (وضع السكالبينج): خروج سوقي بعد TIME_STOP_BARS شمعة مهما كانت الحالة
            if C.TIME_STOP_BARS > 0 and tr.bars_held >= C.TIME_STOP_BARS:
                rem = tr.remaining()
                fee = tr.size * rem * self._fee("stale")
                cash_ref[0] += tr.close_part(t, c[i], rem, "TIME", fee)
                tr.exit_time = t
                tr.exit_reasons.append("TIME")
                stats["time_exits"] += 1
                finalize(tr, i, t, day)
                state["trade"] = None
                return
            if tr.bars_held > C.STALE_BARS and not np.isnan(h1_close[i]):
                unreal_r = ((c[i] - tr.entry) if tr.long else (tr.entry - c[i])) / tr.risk_dist
                mom_against = (tr.long and h1_close[i] < h1_ema21[i]) or \
                              (not tr.long and h1_close[i] > h1_ema21[i])
                if unreal_r < C.STALE_MIN_R and mom_against:
                    rem = tr.remaining()
                    fee = tr.size * rem * self._fee("stale")
                    cash_ref[0] += tr.close_part(t, c[i], rem, "STALE", fee)
                    tr.exit_time = t
                    tr.exit_reasons.append("STALE")
                    stats["stale_exits"] += 1
                    finalize(tr, i, t, day)
                    state["trade"] = None
                    return
            if tr.long:
                tr.highest_close = max(tr.highest_close, c[i])
            else:
                tr.lowest_close = min(tr.lowest_close, c[i])
            if tr.be_active:
                new_sl = tr.entry + (C.BE_OFFSET_ATR * atr[i] if tr.long else -C.BE_OFFSET_ATR * atr[i])
                if (tr.long and new_sl > tr.sl) or (not tr.long and new_sl < tr.sl):
                    tr.sl = new_sl
                    tr.sl_moves.append((t, new_sl, "BE"))
            if tr.trail_active:
                if tr.long:
                    new_sl = tr.highest_close - C.TRAIL_ATR_MULT * atr[i]
                    if new_sl > tr.sl:
                        tr.sl = new_sl
                        tr.sl_moves.append((t, new_sl, "TRAIL"))
                else:
                    new_sl = tr.lowest_close + C.TRAIL_ATR_MULT * atr[i]
                    if new_sl < tr.sl:
                        tr.sl = new_sl
                        tr.sl_moves.append((t, new_sl, "TRAIL"))

        n = len(df)
        for i in range(n):
            t = times[i]
            hour, weekday = t.hour, t.weekday()
            day = t.date()
            iso = t.isocalendar()
            week = (iso.year, iso.week)

            # ═══ (1) إغلاق نهاية الأسبوع عند الافتتاح ═══
            if state["trade"] is not None and weekday == 4 and hour >= C.FLAT_FRIDAY_HOUR:
                tr = state["trade"]
                rem = tr.remaining()
                fee = tr.size * rem * self._fee("stop")
                cash_ref[0] += tr.close_part(t, o[i], rem, "WEEKEND", fee)
                tr.exit_time = t
                tr.exit_reasons.append("WEEKEND")
                stats["weekend_exits"] += 1
                finalize(tr, i, t, day)
                state["trade"] = None
            if state["pending"] is not None and (
                    weekday >= 5 or (weekday == 4 and hour >= C.FLAT_FRIDAY_HOUR)):
                state["pending"] = None   # إلغاء الأوامر المعلقة نهاية الأسبوع

            # ═══ (2) تنفيذ الأوامر المعلّقة والإشارات السوقية ═══
            sig = state["pending"]
            if sig is not None:
                state["pending"] = None
                if sig.get("style") == "limit":
                    # انتهاء الصلاحية
                    if i - sig["_bar"] > C.LIMIT_EXPIRE_BARS:
                        stats["limit_expired"] += 1
                        sig = None
                    # إلغاء عند فشل الهيكل (إغلاق عكسي بعيد)
                    elif (sig["long"] and c[i - 1] < sig["limit"] - C.LIMIT_CANCEL_ATR * sig["atr"]) or \
                            (not sig["long"] and c[i - 1] > sig["limit"] + C.LIMIT_CANCEL_ATR * sig["atr"]):
                        stats["limit_cancelled"] += 1
                        sig = None
                if sig is not None:
                    if sig.get("style") == "limit":
                        if state["trade"] is not None:
                            state["pending"] = sig   # ننتظر مع وجود صفقة (قد تعكس لاحقاً)
                        else:
                            fill = None
                            if sig["long"]:
                                if o[i] <= sig["limit"]:
                                    fill = o[i]
                                elif l[i] <= sig["limit"]:
                                    fill = sig["limit"]
                            else:
                                if o[i] >= sig["limit"]:
                                    fill = o[i]
                                elif h[i] >= sig["limit"]:
                                    fill = sig["limit"]
                            if fill is not None and gates_ok(sig, i, t, hour, weekday, day, week):
                                try_enter(sig, fill, i, t, day, week)
                                if state["trade"] is not None:
                                    # إدارة شمعة التعبئة (وقف أولاً — متحفظ)
                                    manage_in_bar(i, t, day)
                            elif fill is not None:
                                stats["limit_no_session"] += 1  # لمس الطلب خارج بوابات الانضباط
                            if state["trade"] is None:
                                state["pending"] = sig  # الطلب يبقى راقداً حتى انتهاء صلاحيته أو إلغائه أو تعبئته
                    else:
                        # إشارة سوقية: تنفيذ عند الافتتاح
                        if state["trade"] is not None:
                            if sig["grade"] == "A+" and sig["long"] != state["trade"].long:
                                tr = state["trade"]
                                rem = tr.remaining()
                                fee = tr.size * rem * self._fee("stop")
                                cash_ref[0] += tr.close_part(t, o[i], rem, "REVERSE", fee)
                                tr.exit_time = t
                                tr.exit_reasons.append("REVERSE")
                                stats["reversals"] += 1
                                finalize(tr, i, t, day)
                                state["trade"] = None
                            else:
                                stats["blocked_in_trade"] += 1
                                sig = None
                        if sig is not None and gates_ok(sig, i, t, hour, weekday, day, week):
                            try_enter(sig, o[i], i, t, day, week)
                            if state["trade"] is not None:
                                manage_in_bar(i, t, day)

            # ═══ (3) إدارة الصفقة داخل الشمعة ═══
            if state["trade"] is not None:
                if not manage_in_bar(i, t, day):
                    manage_on_close(i, t, day)

            # ═══ (4) توليد إشارة من إغلاق هذه الشمعة ═══
            if state["pending"] is None and i < n - 1:
                row = df.iloc[i]
                prev = df.iloc[i - 1] if i > 0 else row
                # أثناء صفقة مفتوحة نهتم فقط بإشارات العكس A+
                new_sig = evaluate_signal(row, prev, row)
                if new_sig is not None:
                    stats["signals"] += 1
                    new_sig["_bar"] = i
                    if new_sig.get("style") == "limit":
                        stats["limit_placed"] += 1
                    if state["trade"] is not None:
                        if new_sig["grade"] == "A+" and new_sig["long"] != state["trade"].long:
                            state["pending"] = new_sig
                    else:
                        cur = state["pending"]
                        # استبدال الأمر المعلق فقط بإشارة معاكسة (لا نفس الاتجاه)
                        if cur is None or new_sig["long"] != cur["long"]:
                            state["pending"] = new_sig

            # ═══ (5) قيمة الحساب لحظياً ═══
            tr = state["trade"]
            eq = cash_ref[0] + (tr.pnl(c[i]) if tr is not None else 0.0)
            equity_curve.append((t, eq))

        if state["trade"] is not None:
            i, t = n - 1, times[-1]
            tr = state["trade"]
            rem = tr.remaining()
            fee = tr.size * rem * self._fee("stop")
            cash_ref[0] += tr.close_part(t, c[i], rem, "EOD", fee)
            tr.exit_time = t
            tr.exit_reasons.append("EOD")
            finalize(tr, i, t, t.date())
            equity_curve[-1] = (t, cash_ref[0])

        eq_df = pd.DataFrame(equity_curve, columns=["time", "equity"]).set_index("time")
        return {"trades": trades, "equity": eq_df, "stats": stats,
                "final_cash": cash_ref[0], "ctx": df}

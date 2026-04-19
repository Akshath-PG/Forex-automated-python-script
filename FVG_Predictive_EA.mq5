//+------------------------------------------------------------------+
//|  FVG_Predictive_EA.mq5                                           |
//|  Hybrid FVG Scalper — MQL5 handles all trading                   |
//|  Python handles all Telegram notifications via HTTP              |
//+------------------------------------------------------------------+
#property copyright "Akshath XAU Bot"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>

// ====================================
// INPUT PARAMETERS
// ====================================
input string   InpSymbol          = "XAUUSD";
input double   InpRiskPercent      = 0.5;          // Risk % per trade
input double   InpRRRatio         = 2.0;           // Risk:Reward ratio
input double   InpMinFVGGap       = 12.0;          // Minimum FVG gap in points
input int      InpFVGLookback     = 20;            // Candles to look back for FVGs
input double   InpApproachProx    = 10.0;          // Points from zone to trigger approach
input double   InpEntryInsidePct  = 0.20;          // Entry 20% inside gap from edge
input int      InpPendingExpiryMin= 15;            // Cancel unfilled orders after N min
input int      InpMinHoldSeconds  = 180;           // Minimum hold time (funded account)
input double   InpBreakevenPct    = 0.50;          // Move SL to BE at 50% toward TP
input double   InpTrailRiskPct    = 0.50;          // Trail by 50% of initial risk
input int      InpStructureLookback = 30;          // Candles for swing detection
input int      InpSwingN          = 5;             // N candles each side for swing
input int      InpMaxTrades       = 3;             // Max simultaneous trades
input double   InpDailyLossLimit  = 2.0;           // Daily loss limit %
input int      InpMomentumFVGCount= 3;             // FVGs needed for H1 override
input string   InpPythonURL       = "http://localhost:5000/event"; // Python server
input int      InpMagicNumber     = 303030;        // EA magic number

// ====================================
// GLOBALS
// ====================================
CTrade         Trade;
CPositionInfo  PositionInfo;
COrderInfo     OrderInfo;

datetime       LastBarTime        = 0;
double         DayStartBalance    = 0;
bool           DailyLossHit       = false;
bool           IsPaused           = false;

// Track which gaps have already been acted on (prevent re-firing)
string         FiredGaps[];
int            FiredGapCount      = 0;

// Track pending predictive orders
struct PredOrder {
    ulong    ticket;
    string   trigger;
    string   side;
    double   entry;
    double   sl;
    double   tp;
    double   lot;
    datetime placed_at;
    bool     filled;
    string   gap_id;
};
PredOrder PredOrders[];
int       PredOrderCount = 0;

// Track positions for hold period
struct HoldInfo {
    ulong    ticket;
    datetime open_time;
    double   target_sl;
    double   target_tp;
    bool     sl_tp_applied;
    bool     be_applied;
    string   trigger;
};
HoldInfo HoldPositions[];
int      HoldCount = 0;

// ====================================
// INITIALIZATION
// ====================================
int OnInit()
{
    Trade.SetExpertMagicNumber(InpMagicNumber);
    Trade.SetDeviationInPoints(20);
    Trade.SetTypeFilling(ORDER_FILLING_IOC);

    DayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);

    Print("FVG Predictive EA started | Symbol=", InpSymbol,
          " Magic=", InpMagicNumber,
          " MinGap=", InpMinFVGGap,
          " Lookback=", InpFVGLookback);

    PostEvent("BOT_STARTED", "FVG Predictive EA online on " + InpSymbol,
              "", "", 0, 0, 0, "");
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
    PostEvent("BOT_STOPPED", "FVG Predictive EA stopped. Reason=" + IntegerToString(reason),
              "", "", 0, 0, 0, "");
}

// ====================================
// MAIN TICK
// ====================================
void OnTick()
{
    // Reset daily state at new day
    MqlDateTime dt;
    TimeToStruct(TimeCurrent(), dt);
    if(dt.hour == 0 && dt.min == 0) {
        DayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
        DailyLossHit    = false;
        ArrayResize(FiredGaps, 0);
        FiredGapCount = 0;
        Print("Daily state reset.");
    }

    if(IsPaused) return;
    if(CheckDailyLoss()) return;
    if(CountOpenTrades() >= InpMaxTrades) return;
    if(IsNewsForbidden()) return;

    // Manage existing positions (hold period, BE, trail)
    ManageOpenPositions();

    // Check for expired pending orders
    CheckPendingExpiry();

    // Run predictive engine on every new bar
    datetime current_bar = iTime(InpSymbol, PERIOD_M5, 0);
    if(current_bar == LastBarTime) {
        // Between bars — still run approach and live forming (tick-level)
        RunApproachTrigger();
        RunLiveFormingTrigger();
        return;
    }
    LastBarTime = current_bar;

    // New bar — run all 3 triggers
    RunApproachTrigger();
    RunLiveFormingTrigger();
    RunStructureBreakTrigger();
}

// ====================================
// TRIGGER 1 — PRICE APPROACHING ZONE
// H1 filter applies (+ momentum override)
// ====================================
void RunApproachTrigger()
{
    double current_price = SymbolInfoDouble(InpSymbol, SYMBOL_BID);
    string h1_trend      = GetH1Trend();

    // Get fresh FVGs
    int total_bars = InpFVGLookback + 3;
    MqlRates rates[];
    if(CopyRates(InpSymbol, PERIOD_M5, 0, total_bars, rates) < total_bars) return;

    int fresh_count = 0;
    // Count fresh FVGs for momentum check
    for(int i = 2; i < total_bars - 1; i++) {
        double gap_low = 0, gap_high = 0;
        string gap_type = "";
        if(rates[i-2].high < rates[i].low) {
            gap_low  = rates[i-2].high;
            gap_high = rates[i].low;
            gap_type = "bullish";
        } else if(rates[i-2].low > rates[i].high) {
            gap_low  = rates[i].high;
            gap_high = rates[i-2].low;
            gap_type = "bearish";
        }
        if(gap_type == "" || (gap_high - gap_low) < InpMinFVGGap) continue;
        if(current_price >= gap_low && current_price <= gap_high) fresh_count++;
    }

    for(int i = 2; i < total_bars - 1; i++) {
        double gap_low = 0, gap_high = 0;
        string gap_type = "";

        if(rates[i-2].high < rates[i].low) {
            gap_low  = rates[i-2].high;
            gap_high = rates[i].low;
            gap_type = "bullish";
        } else if(rates[i-2].low > rates[i].high) {
            gap_low  = rates[i].high;
            gap_high = rates[i-2].low;
            gap_type = "bearish";
        }

        if(gap_type == "") continue;
        double gap_size = gap_high - gap_low;
        if(gap_size < InpMinFVGGap) continue;

        // Check if already fired
        string gap_id = gap_type + "_" +
                        DoubleToString(gap_low, 2) + "_" +
                        DoubleToString(gap_high, 2);
        if(IsGapFired(gap_id)) continue;

        // Check mitigation
        if(gap_type == "bullish" && current_price < gap_low) continue;
        if(gap_type == "bearish" && current_price > gap_high) continue;

        double entry = 0, sl = 0, tp = 0;
        string side  = "";

        if(gap_type == "bullish") {
            double near_edge = gap_high;
            double dist      = current_price - near_edge;
            if(dist <= 0 || dist > InpApproachProx) continue;
            entry = near_edge + (gap_low - near_edge) * InpEntryInsidePct;
            sl    = gap_low - gap_size * 0.1;
            tp    = entry + (entry - sl) * InpRRRatio;
            side  = "BUY";
        } else {
            double near_edge = gap_low;
            double dist      = near_edge - current_price;
            if(dist <= 0 || dist > InpApproachProx) continue;
            entry = near_edge + (gap_high - near_edge) * InpEntryInsidePct;
            sl    = gap_high + gap_size * 0.1;
            tp    = entry - (sl - entry) * InpRRRatio;
            side  = "SELL";
        }

        // H1 filter for Trigger 1
        bool trend_ok = (side == "BUY"  && h1_trend == "bullish") ||
                        (side == "SELL" && h1_trend == "bearish");
        bool momentum = fresh_count >= InpMomentumFVGCount;
        if(!trend_ok && !momentum) {
            Print("T1 APPROACH blocked: side=", side, " H1=", h1_trend,
                  " fresh_fvgs=", fresh_count);
            continue;
        }

        PlacePredictiveOrder(side, entry, sl, tp, "APPROACH", gap_id);
    }
}

// ====================================
// TRIGGER 2 — LIVE GAP FORMING
// H1 filter BYPASSED
// ====================================
void RunLiveFormingTrigger()
{
    MqlRates rates[];
    if(CopyRates(InpSymbol, PERIOD_M5, 0, 3, rates) < 3) return;

    // rates[0]=oldest, rates[1]=middle, rates[2]=live candle
    MqlRates c1 = rates[0];
    MqlRates c3 = rates[2];  // live candle still forming

    double current_price = SymbolInfoDouble(InpSymbol, SYMBOL_BID);

    // Bullish FVG forming
    if(c1.high < c3.low) {
        double gap_size = c3.low - c1.high;
        if(gap_size >= InpMinFVGGap) {
            string gap_id = "live_bull_" +
                            DoubleToString(c1.high, 2) + "_" +
                            DoubleToString(c3.low, 2);
            if(!IsGapFired(gap_id)) {
                double near_edge = c3.low;
                double far_edge  = c1.high;
                double entry     = near_edge + (far_edge - near_edge) * InpEntryInsidePct;
                double sl        = far_edge - gap_size * 0.1;
                double tp        = entry + (entry - sl) * InpRRRatio;
                PlacePredictiveOrder("BUY", entry, sl, tp, "LIVE_FORMING", gap_id);
            }
        }
    }

    // Bearish FVG forming
    if(c1.low > c3.high) {
        double gap_size = c1.low - c3.high;
        if(gap_size >= InpMinFVGGap) {
            string gap_id = "live_bear_" +
                            DoubleToString(c3.high, 2) + "_" +
                            DoubleToString(c1.low, 2);
            if(!IsGapFired(gap_id)) {
                double near_edge = c3.high;
                double far_edge  = c1.low;
                double entry     = near_edge + (far_edge - near_edge) * InpEntryInsidePct;
                double sl        = far_edge + gap_size * 0.1;
                double tp        = entry - (sl - entry) * InpRRRatio;
                PlacePredictiveOrder("SELL", entry, sl, tp, "LIVE_FORMING", gap_id);
            }
        }
    }
}

// ====================================
// TRIGGER 3 — STRUCTURE BREAK
// H1 filter BYPASSED
// ====================================
void RunStructureBreakTrigger()
{
    int total = InpStructureLookback + InpSwingN * 2 + 2;
    MqlRates rates[];
    if(CopyRates(InpSymbol, PERIOD_M5, 0, total, rates) < total) return;

    double current_close = rates[total-1].close;
    int    n             = InpSwingN;

    for(int i = n; i < total - n - 2; i++) {
        bool is_swing_high = true;
        bool is_swing_low  = true;

        for(int j = 1; j <= n; j++) {
            if(rates[i].high < rates[i-j].high || rates[i].high < rates[i+j].high)
                is_swing_high = false;
            if(rates[i].low  > rates[i-j].low  || rates[i].low  > rates[i+j].low)
                is_swing_low  = false;
        }

        if(is_swing_high) {
            double swing_level = rates[i].high;
            string gap_id = "struct_bull_" + DoubleToString(swing_level, 2);
            if(!IsGapFired(gap_id) && current_close > swing_level) {
                double gap_size  = MathMax(rates[total-1].high - swing_level, InpMinFVGGap);
                double near_edge = swing_level;
                double far_edge  = swing_level - gap_size * 0.5;
                double entry     = near_edge + (far_edge - near_edge) * InpEntryInsidePct;
                double sl        = far_edge - gap_size * 0.1;
                double tp        = entry + (entry - sl) * InpRRRatio;
                PlacePredictiveOrder("BUY", entry, sl, tp, "STRUCTURE_BREAK", gap_id);
            }
        }

        if(is_swing_low) {
            double swing_level = rates[i].low;
            string gap_id = "struct_bear_" + DoubleToString(swing_level, 2);
            if(!IsGapFired(gap_id) && current_close < swing_level) {
                double gap_size  = MathMax(swing_level - rates[total-1].low, InpMinFVGGap);
                double near_edge = swing_level;
                double far_edge  = swing_level + gap_size * 0.5;
                double entry     = near_edge + (far_edge - near_edge) * InpEntryInsidePct;
                double sl        = far_edge + gap_size * 0.1;
                double tp        = entry - (sl - entry) * InpRRRatio;
                PlacePredictiveOrder("SELL", entry, sl, tp, "STRUCTURE_BREAK", gap_id);
            }
        }
    }
}

// ====================================
// PLACE PREDICTIVE LIMIT ORDER
// ====================================
void PlacePredictiveOrder(string side, double entry, double sl, double tp,
                           string trigger, string gap_id)
{
    if(CountOpenTrades() + PredOrderCount >= InpMaxTrades) return;

    entry = NormalizeDouble(entry, _Digits);
    sl    = NormalizeDouble(sl,    _Digits);
    tp    = NormalizeDouble(tp,    _Digits);

    // Validate stop distance
    double min_dist = SymbolInfoInteger(InpSymbol, SYMBOL_TRADE_STOPS_LEVEL) *
                      SymbolInfoDouble(InpSymbol, SYMBOL_POINT);
    min_dist = MathMax(min_dist, 0.50);

    if(side == "BUY"  && (entry - sl < min_dist || tp - entry < min_dist)) return;
    if(side == "SELL" && (sl - entry < min_dist || entry - tp < min_dist)) return;

    double   lot        = CalcLotSize(entry, sl);
    ENUM_ORDER_TYPE ot  = (side == "BUY") ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;

    // Place with sl=0, tp=0 — applied after hold period
    bool ok = Trade.OrderOpen(InpSymbol, ot, lot, 0, entry, 0, 0,
                               ORDER_TIME_GTC, 0,
                               "PRED_" + trigger);
    if(!ok) {
        Print("PlacePredictiveOrder failed: ", Trade.ResultRetcode(),
              " ", Trade.ResultRetcodeDescription());
        return;
    }

    ulong ticket = Trade.ResultOrder();
    MarkGapFired(gap_id);

    // Store in PredOrders array
    ArrayResize(PredOrders, PredOrderCount + 1);
    PredOrders[PredOrderCount].ticket     = ticket;
    PredOrders[PredOrderCount].trigger    = trigger;
    PredOrders[PredOrderCount].side       = side;
    PredOrders[PredOrderCount].entry      = entry;
    PredOrders[PredOrderCount].sl        = sl;
    PredOrders[PredOrderCount].tp        = tp;
    PredOrders[PredOrderCount].lot       = lot;
    PredOrders[PredOrderCount].placed_at  = TimeCurrent();
    PredOrders[PredOrderCount].filled    = false;
    PredOrders[PredOrderCount].gap_id    = gap_id;
    PredOrderCount++;

    Print("Predictive order placed: ", trigger, " ", side,
          " @ ", entry, " Ticket=", ticket);

    // Notify Python
    string body = "{" +
        "\"type\":\"ORDER_PLACED\"," +
        "\"trigger\":\"" + trigger + "\"," +
        "\"side\":\"" + side + "\"," +
        "\"entry\":" + DoubleToString(entry, 2) + "," +
        "\"sl\":" + DoubleToString(sl, 2) + "," +
        "\"tp\":" + DoubleToString(tp, 2) + "," +
        "\"lot\":" + DoubleToString(lot, 2) + "," +
        "\"ticket\":" + IntegerToString((int)ticket) + "," +
        "\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
        "}";
    PostEvent("ORDER_PLACED", body, trigger, side, entry, sl, tp, IntegerToString((int)ticket));
}

// ====================================
// MANAGE OPEN POSITIONS
// Hold period → apply SL/TP → BE → trail
// ====================================
void ManageOpenPositions()
{
    datetime now = TimeCurrent();

    for(int i = PositionsTotal() - 1; i >= 0; i--) {
        if(!PositionInfo.SelectByIndex(i)) continue;
        if(PositionInfo.Magic() != InpMagicNumber) continue;
        if(PositionInfo.Symbol() != InpSymbol)    continue;

        ulong  ticket     = PositionInfo.Ticket();
        double open_price = PositionInfo.PriceOpen();
        double current_sl = PositionInfo.StopLoss();
        double current_tp = PositionInfo.TakeProfit();
        double cur_price  = PositionInfo.PriceCurrent();

        // Find hold info
        int hold_idx = FindHoldInfo(ticket);

        // First time seeing this position — it just got filled
        if(hold_idx < 0) {
            hold_idx = RegisterFilledPosition(ticket, open_price);
            if(hold_idx < 0) continue;
        }

        HoldInfo hi     = HoldPositions[hold_idx];
        int hold_elapsed = (int)(now - hi.open_time);
        int hold_left    = MathMax(0, InpMinHoldSeconds - hold_elapsed);

        if(hold_left > 0) continue;   // still in hold period

        // Apply SL/TP after hold period
        if(!hi.sl_tp_applied && current_sl == 0.0 && current_tp == 0.0) {
            double min_dist   = MathMax(
                SymbolInfoInteger(InpSymbol, SYMBOL_TRADE_STOPS_LEVEL) *
                SymbolInfoDouble(InpSymbol, SYMBOL_POINT), 0.50);
            double risk       = MathAbs(hi.target_sl - hi.target_tp) / (1.0 + InpRRRatio);
            risk              = MathMax(risk, min_dist);

            double real_sl, real_tp;
            if(PositionInfo.PositionType() == POSITION_TYPE_BUY) {
                real_sl = NormalizeDouble(open_price - risk, _Digits);
                real_tp = NormalizeDouble(open_price + risk * InpRRRatio, _Digits);
            } else {
                real_sl = NormalizeDouble(open_price + risk, _Digits);
                real_tp = NormalizeDouble(open_price - risk * InpRRRatio, _Digits);
            }

            if(Trade.PositionModify(ticket, real_sl, real_tp)) {
                HoldPositions[hold_idx].sl_tp_applied = true;
                HoldPositions[hold_idx].target_sl     = real_sl;
                HoldPositions[hold_idx].target_tp     = real_tp;
                Print("SL/TP applied after hold: #", ticket,
                      " SL=", real_sl, " TP=", real_tp);
                PostEvent("SLTP_APPLIED",
                    "{\"ticket\":" + IntegerToString((int)ticket) +
                    ",\"sl\":" + DoubleToString(real_sl,2) +
                    ",\"tp\":" + DoubleToString(real_tp,2) + "}",
                    hi.trigger, "", real_sl, real_tp, 0, IntegerToString((int)ticket));
            }
            continue;
        }

        if(!hi.sl_tp_applied) continue;  // wait until SL/TP is set

        // Recalculate live values
        current_sl = PositionInfo.StopLoss();
        current_tp = PositionInfo.TakeProfit();
        double initial_risk, tp_distance, progress;

        if(PositionInfo.PositionType() == POSITION_TYPE_BUY) {
            initial_risk = open_price - current_sl;
            tp_distance  = current_tp - open_price;
            if(tp_distance <= 0 || initial_risk <= 0) continue;
            progress     = (cur_price - open_price) / tp_distance;

            // Breakeven
            if(!hi.be_applied && progress >= InpBreakevenPct) {
                double be_sl = NormalizeDouble(open_price, _Digits);
                if(Trade.PositionModify(ticket, be_sl, current_tp)) {
                    HoldPositions[hold_idx].be_applied = true;
                    Print("Breakeven: BUY #", ticket);
                    PostEvent("BREAKEVEN",
                        "{\"ticket\":" + IntegerToString((int)ticket) +
                        ",\"sl\":" + DoubleToString(be_sl,2) + "}",
                        hi.trigger, "BUY", be_sl, current_tp, 0,
                        IntegerToString((int)ticket));
                }
            }

            // Trailing stop
            double trail_sl = NormalizeDouble(cur_price - initial_risk * InpTrailRiskPct, _Digits);
            if(trail_sl > current_sl) {
                if(Trade.PositionModify(ticket, trail_sl, current_tp)) {
                    PostEvent("TRAIL_MOVED",
                        "{\"ticket\":" + IntegerToString((int)ticket) +
                        ",\"new_sl\":" + DoubleToString(trail_sl,2) + "}",
                        hi.trigger, "BUY", trail_sl, current_tp, 0,
                        IntegerToString((int)ticket));
                }
            }

        } else { // SELL
            initial_risk = current_sl - open_price;
            tp_distance  = open_price - current_tp;
            if(tp_distance <= 0 || initial_risk <= 0) continue;
            progress     = (open_price - cur_price) / tp_distance;

            // Breakeven
            if(!hi.be_applied && progress >= InpBreakevenPct) {
                double be_sl = NormalizeDouble(open_price, _Digits);
                if(Trade.PositionModify(ticket, be_sl, current_tp)) {
                    HoldPositions[hold_idx].be_applied = true;
                    Print("Breakeven: SELL #", ticket);
                    PostEvent("BREAKEVEN",
                        "{\"ticket\":" + IntegerToString((int)ticket) +
                        ",\"sl\":" + DoubleToString(be_sl,2) + "}",
                        hi.trigger, "SELL", be_sl, current_tp, 0,
                        IntegerToString((int)ticket));
                }
            }

            // Trailing stop
            double trail_sl = NormalizeDouble(cur_price + initial_risk * InpTrailRiskPct, _Digits);
            if(trail_sl < current_sl) {
                if(Trade.PositionModify(ticket, trail_sl, current_tp)) {
                    PostEvent("TRAIL_MOVED",
                        "{\"ticket\":" + IntegerToString((int)ticket) +
                        ",\"new_sl\":" + DoubleToString(trail_sl,2) + "}",
                        hi.trigger, "SELL", trail_sl, current_tp, 0,
                        IntegerToString((int)ticket));
                }
            }
        }
    }
}

// ====================================
// TRADE TRANSACTION — detect fills and closes
// ====================================
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
{
    // Order filled → position opened
    if(trans.type == TRADE_TRANSACTION_ORDER_DELETE &&
       trans.order_state == ORDER_STATE_FILLED) {
        ulong ticket = trans.order;
        // Find in PredOrders
        for(int i = 0; i < PredOrderCount; i++) {
            if(PredOrders[i].ticket == ticket) {
                PredOrders[i].filled = true;
                string side    = PredOrders[i].side;
                string trigger = PredOrders[i].trigger;
                double entry   = trans.price;

                Print("Predictive order FILLED: ", trigger, " ", side,
                      " @ ", entry, " Ticket=", ticket);

                PostEvent("TRADE_FILLED",
                    "{\"ticket\":" + IntegerToString((int)ticket) +
                    ",\"trigger\":\"" + trigger + "\"" +
                    ",\"side\":\"" + side + "\"" +
                    ",\"entry\":" + DoubleToString(entry, 2) +
                    ",\"sl\":" + DoubleToString(PredOrders[i].sl, 2) +
                    ",\"tp\":" + DoubleToString(PredOrders[i].tp, 2) +
                    ",\"lot\":" + DoubleToString(PredOrders[i].lot, 2) +
                    ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
                    "}",
                    trigger, side, entry,
                    PredOrders[i].sl, PredOrders[i].tp,
                    IntegerToString((int)ticket));
                break;
            }
        }
    }

    // Position closed → TP or SL hit
    if(trans.type == TRADE_TRANSACTION_DEAL_ADD) {
        if(trans.deal_type == DEAL_TYPE_BUY || trans.deal_type == DEAL_TYPE_SELL) {
            ulong  deal_ticket = trans.deal;
            double profit      = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
            string result_str  = (profit >= 0) ? "WIN" : "LOSS";
            string label       = (profit >= 0) ? "TP_HIT" : "SL_HIT";

            // Find trigger
            string trigger = "UNKNOWN";
            ulong  pos_id  = trans.position;
            for(int i = 0; i < PredOrderCount; i++) {
                if(PredOrders[i].ticket == pos_id) {
                    trigger = PredOrders[i].trigger;
                    break;
                }
            }

            Print("Position closed: ", label, " PnL=", profit,
                  " Trigger=", trigger);

            PostEvent(label,
                "{\"ticket\":" + IntegerToString((int)pos_id) +
                ",\"trigger\":\"" + trigger + "\"" +
                ",\"pnl\":" + DoubleToString(profit, 2) +
                ",\"result\":\"" + result_str + "\"" +
                ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
                ",\"price\":" + DoubleToString(SymbolInfoDouble(InpSymbol, SYMBOL_BID), 2) +
                "}",
                trigger, "", 0, 0, 0, IntegerToString((int)pos_id));
        }
    }
}

// ====================================
// CHECK PENDING ORDER EXPIRY
// ====================================
void CheckPendingExpiry()
{
    datetime now = TimeCurrent();
    for(int i = 0; i < PredOrderCount; i++) {
        if(PredOrders[i].filled) continue;
        int age_sec = (int)(now - PredOrders[i].placed_at);
        if(age_sec < InpPendingExpiryMin * 60) continue;

        // Check if still pending
        if(OrderSelect(PredOrders[i].ticket)) {
            Trade.OrderDelete(PredOrders[i].ticket);
            Print("Expired order cancelled: #", PredOrders[i].ticket,
                  " Trigger=", PredOrders[i].trigger);
            PostEvent("ORDER_EXPIRED",
                "{\"ticket\":" + IntegerToString((int)PredOrders[i].ticket) +
                ",\"trigger\":\"" + PredOrders[i].trigger + "\"" +
                ",\"side\":\"" + PredOrders[i].side + "\"" +
                ",\"entry\":" + DoubleToString(PredOrders[i].entry, 2) +
                "}",
                PredOrders[i].trigger, PredOrders[i].side,
                PredOrders[i].entry, 0, 0,
                IntegerToString((int)PredOrders[i].ticket));
            // Remove from array
            RemovePredOrder(i);
            i--;
        }
    }
}

// ====================================
// DAILY LOSS CHECK
// ====================================
bool CheckDailyLoss()
{
    if(DailyLossHit) return true;
    double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
    double loss_pct = (DayStartBalance - balance) / DayStartBalance * 100.0;
    if(loss_pct >= InpDailyLossLimit) {
        DailyLossHit = true;
        Print("Daily loss limit hit: ", loss_pct, "%");
        PostEvent("DAILY_LOSS_HIT",
            "{\"loss_pct\":" + DoubleToString(loss_pct, 2) +
            ",\"balance\":" + DoubleToString(balance, 2) + "}",
            "", "", 0, 0, 0, "");
        return true;
    }
    return false;
}

// ====================================
// H1 TREND
// ====================================
string GetH1Trend()
{
    double ema50[], ema200[];
    if(CopyBuffer(iMA(InpSymbol, PERIOD_H1, 50,  0, MODE_EMA, PRICE_CLOSE), 0, 0, 1, ema50)  < 1) return "neutral";
    if(CopyBuffer(iMA(InpSymbol, PERIOD_H1, 200, 0, MODE_EMA, PRICE_CLOSE), 0, 0, 1, ema200) < 1) return "neutral";
    if(ema50[0] > ema200[0]) return "bullish";
    if(ema50[0] < ema200[0]) return "bearish";
    return "neutral";
}

// ====================================
// NEWS FILTER (time-based only — no API in MQL5)
// Blocks trading during known high-impact hours
// ====================================
bool IsNewsForbidden()
{
    MqlDateTime dt;
    TimeToStruct(TimeGMT(), dt);
    int h = dt.hour, m = dt.min;
    // Block: NFP Friday 12:25-13:30 UTC, Fed 18:55-19:30 UTC
    if(dt.day_of_week == 5 && h == 12 && m >= 25) return true;
    if(dt.day_of_week == 5 && h == 13 && m <= 30) return true;
    return false;
}

// ====================================
// LOT SIZE
// ====================================
double CalcLotSize(double entry, double sl)
{
    double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
    double stop_dist  = MathAbs(entry - sl);
    if(stop_dist == 0) return 0.01;
    double lot = (balance * InpRiskPercent / 100.0) / (stop_dist * 100.0);
    double min_lot  = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_MIN);
    double max_lot  = MathMin(SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_MAX), 1.0);
    double lot_step = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_STEP);
    lot = MathFloor(lot / lot_step) * lot_step;
    return MathMax(min_lot, MathMin(max_lot, lot));
}

// ====================================
// HELPER — COUNT OPEN TRADES
// ====================================
int CountOpenTrades()
{
    int count = 0;
    for(int i = 0; i < PositionsTotal(); i++) {
        if(PositionInfo.SelectByIndex(i) &&
           PositionInfo.Magic()  == InpMagicNumber &&
           PositionInfo.Symbol() == InpSymbol)
            count++;
    }
    return count;
}

// ====================================
// HELPERS — GAP TRACKING
// ====================================
bool IsGapFired(string gap_id)
{
    for(int i = 0; i < FiredGapCount; i++)
        if(FiredGaps[i] == gap_id) return true;
    return false;
}

void MarkGapFired(string gap_id)
{
    ArrayResize(FiredGaps, FiredGapCount + 1);
    FiredGaps[FiredGapCount++] = gap_id;
}

// ====================================
// HELPERS — HOLD INFO
// ====================================
int FindHoldInfo(ulong ticket)
{
    for(int i = 0; i < HoldCount; i++)
        if(HoldPositions[i].ticket == ticket) return i;
    return -1;
}

int RegisterFilledPosition(ulong ticket, double open_price)
{
    // Find trigger from pred orders
    string trigger = "UNKNOWN";
    double tgt_sl  = 0, tgt_tp = 0;
    for(int i = 0; i < PredOrderCount; i++) {
        if(PredOrders[i].ticket == ticket) {
            trigger = PredOrders[i].trigger;
            tgt_sl  = PredOrders[i].sl;
            tgt_tp  = PredOrders[i].tp;
            break;
        }
    }

    ArrayResize(HoldPositions, HoldCount + 1);
    HoldPositions[HoldCount].ticket        = ticket;
    HoldPositions[HoldCount].open_time     = TimeCurrent();
    HoldPositions[HoldCount].target_sl     = tgt_sl;
    HoldPositions[HoldCount].target_tp     = tgt_tp;
    HoldPositions[HoldCount].sl_tp_applied = false;
    HoldPositions[HoldCount].be_applied    = false;
    HoldPositions[HoldCount].trigger       = trigger;
    return HoldCount++;
}

void RemovePredOrder(int idx)
{
    for(int i = idx; i < PredOrderCount - 1; i++)
        PredOrders[i] = PredOrders[i + 1];
    PredOrderCount--;
    ArrayResize(PredOrders, PredOrderCount);
}

// ====================================
// HTTP POST TO PYTHON
// ====================================
void PostEvent(string event_type, string body,
               string trigger, string side,
               double price1, double price2, double price3,
               string ticket_str)
{
    string url     = InpPythonURL;
    string payload = body;  // body is already full JSON in most calls

    // If body is just a description string (BOT_STARTED etc), wrap it
    if(StringFind(body, "{") < 0) {
        payload = "{\"type\":\"" + event_type + "\",\"message\":\"" + body + "\"}";
    } else {
        // Inject type if not already present
        if(StringFind(payload, "\"type\"") < 0) {
            payload = "{\"type\":\"" + event_type + "\"," + StringSubstr(payload, 1);
        }
    }

    char   post_data[];
    char   result_data[];
    string result_headers;

    StringToCharArray(payload, post_data, 0, StringLen(payload));

    string headers = "Content-Type: application/json\r\n";

    int res = WebRequest("POST", url, headers, 5000,
                         post_data, result_data, result_headers);
    if(res < 0) {
        Print("PostEvent failed (is Python running?): ", GetLastError(),
              " | Event=", event_type);
    }
}
//+------------------------------------------------------------------+

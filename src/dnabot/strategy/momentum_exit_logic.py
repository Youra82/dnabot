# src/dnabot/strategy/momentum_exit_logic.py
# Live-Signal fuer die "gentechnische" Risiko-/Exit-Strategie aus der
# Recherche-Session vom 2026-08-24 (Fund AQ, siehe Memory
# research_dnabot_direction_calibration.md).
#
# Grundidee, bewusst ANDERS als genome_logic.py: nach 44 Funden (A-AO) ist
# belegt, dass reine Richtungs-Vorhersage aus OHLCV bei diesem Bot keinen
# robusten Edge liefert -- egal ob kategorischer Gen-Sequenz-Lookup,
# kontinuierliche Features, Ensembles, genetische Suche oder Motiv/PWM-
# Modelle. Der EINZIGE Fund, der auf einem zweiten, unabhaengigen Fenster
# reproduziert hat (Fund AQ), kommt NICHT aus besserer Richtungs-Vorhersage,
# sondern aus gezielt entworfenen Risiko-/Exit-Parametern bei einem
# bewusst NICHT-praediktiven Einstieg (Richtung = eigene Kerzenrichtung,
# reine Momentum-Fortsetzung). Der Edge steckt im engen strukturellen SL
# + engen Trailing-Stop, der Gewinner laufen laesst und Verlierer schnell
# abschneidet -- NICHT in der Auswahl WELCHE Kerze gehandelt wird.
#
# Validierte Parameter (6h, siehe Fund AQ): seq_len=5, rr_ratio=1.5,
# trailing_callback_rate_pct=0.5, risk_per_entry_pct=1.0. NUR 6h ist
# bisher auf zwei unabhaengigen Fenstern bestaetigt -- 4h/2h/1h zeigten
# das Muster NICHT (2h/4h leicht negativ, 1h explodierte bei zu hohem
# Risiko). Deshalb: eigener, expliziter Signal-Pfad statt Erweiterung von
# genome_logic.py, und standardmaessig deaktiviert (enabled=false) bis
# der Nutzer es bewusst pro Pair/Timeframe aktiviert.
#
# Kein eigenes Signifikanz-Tracking (wie order_block_logic.py) -- die
# Strategie behauptet keinen Vorhersage-Edge pro Signal, deshalb kein
# Score/Winrate-Gate wie bei echten Genomen. Signal-Dict in DERSELBEN Form
# wie genome_logic.py::_build_signal(), damit es unveraendert durch
# trade_manager.py::place_entry_orders() laeuft.

import logging

logger = logging.getLogger(__name__)

MIN_CANDLES_REQUIRED = 35


def get_momentum_exit_signal(df, params) -> dict | None:
    """
    Baut IMMER ein Signal (kein Score-Gate, KEIN Regime-Filter), wenn genug
    Kerzen vorhanden sind -- Richtung = eigene Kerzenrichtung der letzten
    Kerze (Momentum-Fortsetzung, kein Anspruch auf Vorhersage-Edge). Der Edge
    (Fund AQ) steckt in SL-Fenster (seq_len) + Trailing-Callback + RR, nicht
    in der Signal-Selektion selbst.

    BEWUSST kein Regime-/HIGH_VOL-Filter hier: der validierte Research-Code
    (recherche/risk_exit_genetic_test.py::build_candidates()) hat JEDE Kerze
    gehandelt, ohne Regime-Filterung. Ein Filter hier wuerde ein Verhalten
    live schalten, das nie getestet wurde (siehe feedback_live_backtest_
    must_match) -- falls ein Regime-Filter je gewuenscht ist, muss er zuerst
    im Research-Code mitgetestet werden, nicht nur live hinzugefuegt.
    """
    cfg = params.get('momentum_exit', {})
    if not cfg.get('enabled', False):
        return None

    seq_len = int(cfg.get('seq_len', 5))
    if len(df) < max(seq_len, MIN_CANDLES_REQUIRED):
        return None

    rr_ratio = params.get('risk', {}).get('rr_ratio', 1.5)
    last = df.iloc[-1]
    entry_price = float(last['close'])
    side = 'long' if float(last['close']) >= float(last['open']) else 'short'

    seq_candles = df.iloc[-seq_len:]
    if side == 'long':
        sl_price = float(seq_candles['low'].min())
        sl_distance = entry_price - sl_price
        if sl_distance <= 0:
            sl_price = entry_price * 0.98
            sl_distance = entry_price - sl_price
        tp_price = entry_price + rr_ratio * sl_distance
    else:
        sl_price = float(seq_candles['high'].max())
        sl_distance = sl_price - entry_price
        if sl_distance <= 0:
            sl_price = entry_price * 1.02
            sl_distance = sl_price - entry_price
        tp_price = entry_price - rr_ratio * sl_distance

    sl_pct = (sl_distance / entry_price) * 100.0

    signal = {
        "side": side,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "sl_pct": sl_pct,
        "tp_price": tp_price,
        "genome_id": f"MOM:{side.upper()}:{df.index[-1].isoformat()}",
        "sequence": f"MOM:{side.upper()}:seq{seq_len}",
        # Kein Signifikanz-Tracking -- Score/Winrate sind neutrale Platzhalter,
        # der Edge kommt aus der Exit-Mechanik, nicht aus dieser "Vorhersage".
        "score": 1.0,
        "winrate": 0.0,
        "total_occurrences": 0,
        "seq_length": seq_len,
        "avg_move_pct": abs(tp_price - entry_price) / entry_price * 100.0,
        "regime": None,
        # Reuse des bestehenden Skip-Genome-DB-Update-Mechanismus (siehe
        # trade_manager.py::self_learn_from_closed_trade) -- Momentum-Exit
        # hat wie Order Blocks keine Genome-DB-Zeile.
        "is_order_block": True,
        "is_momentum_exit": True,
    }

    logger.info(
        f"[Momentum-Exit Signal] {side.upper()} | Entry: {entry_price:.4f} | "
        f"SL: {sl_price:.4f} ({sl_pct:.2f}%) | TP(Trail-Aktivierung): {tp_price:.4f} | "
        f"seq_len={seq_len} | RR={rr_ratio}"
    )
    return signal

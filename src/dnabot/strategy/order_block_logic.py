# src/dnabot/strategy/order_block_logic.py
# Live-Wrapper fuer genome/order_blocks.py -- gleiche Struktur wie
# genome_logic.py::get_genome_signal(), aber fuer Order-Block-Zonen statt
# Gen-Sequenzen. Siehe order_blocks.py-Modul-Docstring fuer die Begruendung,
# warum das ein eigener Mechanismus ist statt einer Erweiterung des
# Genome-Systems.

import logging

from dnabot.genome.order_blocks import find_order_blocks, check_retest
from dnabot.genome.regime import detect_regime, REGIME_HIGH_VOL

logger = logging.getLogger(__name__)


def get_order_block_signal(df, params) -> dict | None:
    """
    Analysiert die letzte Kerze von df gegen alle noch gueltigen Order-Block-
    Zonen im sichtbaren Verlauf. None wenn deaktiviert, zu wenig Kerzen, kein
    Retest, oder aktuelles Regime HIGH_VOL (gleiche Grundsicherheit wie
    genome_logic.py::_regime_active() -- in einer Volatilitaetsspitze wird
    hier genauso wenig gehandelt wie bei echten Genome-Signalen).
    """
    ob_cfg = params.get('order_block', {})
    if not ob_cfg.get('enabled', False):
        return None

    impulse_length = ob_cfg.get('impulse_length', 3)
    if len(df) < impulse_length + 2:
        return None

    current_regime = detect_regime(df)
    if current_regime == REGIME_HIGH_VOL:
        return None

    market = params['market']['symbol']
    timeframe = params['market']['timeframe']
    rr_ratio = params.get('risk', {}).get('rr_ratio', 2.0)
    alphabet = params.get('genome', {}).get('alphabet')
    max_age_candles = ob_cfg.get('zone_max_age_candles', 100)
    assumed_winrate = ob_cfg.get('assumed_winrate', 0.5)

    zones = find_order_blocks(df, alphabet or {}, impulse_length=impulse_length)
    if not zones:
        return None

    signal = check_retest(
        df, len(df) - 1, zones, market, timeframe,
        rr_ratio=rr_ratio, max_age_candles=max_age_candles,
        assumed_winrate=assumed_winrate,
    )
    if signal:
        signal['regime'] = current_regime
        logger.info(
            f"[Order Block Signal] {signal['side'].upper()} | "
            f"Entry: {signal['entry_price']:.4f} | SL: {signal['sl_price']:.4f} "
            f"({signal['sl_pct']:.2f}%) | TP: {signal['tp_price']:.4f} | "
            f"Zone: {signal['sequence']}"
        )
    return signal

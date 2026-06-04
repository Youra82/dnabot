# dnabot — Adaptive Market Genome System

Ein selbstlernender Trading-Bot, der Marktbewegungen wie genetische Sequenzen analysiert.
Keine neuronalen Netze, keine Black-Box — deterministisches statistisches Pattern Discovery.

> **Disclaimer:** Diese Software ist experimentell und dient ausschließlich Forschungszwecken.
> Der Handel mit Kryptowährungen birgt erhebliche finanzielle Risiken. Nutzung auf eigene Gefahr.

---

## Grundidee

Jede Kerze wird zu einem **Gen-Code** komprimiert:

```
B3H-UH
│││ ││
│││ │└── Volumen:    H = hoch (über 20er-MA), L = niedrig
│││ └─── Wick:       U = oben, D = unten, B = beide, N = keiner
││└───── Volatilität: H = hoch (Range ≥ ATR), L = niedrig
│└────── Körpergröße: 1 = klein (<30% ATR), 2 = mittel, 3 = groß
└─────── Richtung:   B = Bullish, S = Bearish
```

**96 mögliche Gene** — kombinatorisch, vollständig deterministisch.

Sequenzen aus 4–6 aufeinanderfolgenden Genen bilden ein **Genome**:

```
"B2H-NL | B3H-UH | S1L-DL | B2H-NH"
   ↓
Dieses Muster erschien 47x in der Vergangenheit.
30x davon stieg der Kurs danach > 1%.
→ Winrate: 63.8% | Score: 0.34 | Status: AKTIV
```

Der Bot handelt nur, wenn ein solches Genome im Live-Markt erkannt wird.

---

## Architektur

```
dnabot/
├── scan_and_learn.py              # Haupt-Lernprozess (Discovery + Evolver)
├── master_runner.py               # Cronjob-Orchestrator für Live-Trading
├── run_pipeline.sh                # Vollständige Pipeline (Discovery → Report)
├── show_results.sh                # Interaktive Analyse & Backtest-Menü
├── auto_optimizer_scheduler.py    # Automatischer Wochentimer: Discovery + Portfolio-Opt.
├── run_backtest.py                # Einzel-Backtest pro Pair
├── run_portfolio_optimizer.py     # Automatische Portfolio-Optimierung (exhaustive)
├── run_manual_portfolio.py        # Manuelle Portfolio-Simulation (Pair-Auswahl)
├── install.sh                     # Erstinstallation auf VPS
├── update.sh                      # Git-Update (sichert secret.json)
├── settings.json                  # Konfiguration
├── secret.json                    # API-Keys (nicht in Git)
│
└── src/dnabot/
    ├── genome/
    │   ├── encoder.py             # Kerze → Gen-String
    │   ├── database.py            # SQLite-Interface (Genome-Library)
    │   ├── discovery.py           # Pattern-Mining aus Historien-Daten
    │   └── evolver.py             # Scoring + Aktivierung/Deaktivierung
    │
    ├── strategy/
    │   ├── genome_logic.py        # Aktuelle Kerzen vs. DB → Signal
    │   └── run.py                 # Entry Point für eine Strategie
    │
    ├── analysis/
    │   ├── backtester.py          # Historische Simulation
    │   ├── interactive_chart.py   # Plotly Candlestick + Trade-Marker + Equity
    │   └── show_results.py        # Report: Genome-Library + Backtest
    │
    └── utils/
        ├── exchange.py            # Bitget CCXT Wrapper
        ├── trade_manager.py       # Entry/TP/SL + Self-Learning
        ├── telegram.py            # Telegram-Benachrichtigungen
        └── guardian.py            # Crash-Schutz Decorator
```

---

## Wie das System lernt

### Phase 1 — Discovery (`scan_and_learn.py`)

```
Historische Daten (2 Jahre OHLCV)
    ↓
Alle Kerzen → Gene codieren
    ↓
Sliding Window (seq_len = 4, 5, 6)
    ↓
Für jedes Fenster: Was passierte danach? (strikt NACH dem Sequenz-Close)
  max_up > 1% UND max_up > max_down → LONG-Outcome
  max_down > 1% UND max_down > max_up → SHORT-Outcome
    ↓
Genome in SQLite speichern / aktualisieren
```

> Zukunfts-Kerzen werden ausschließlich nach dem Close der letzten Sequenz-Kerze bewertet
> (kein Lookahead-Bias). Discovery und Backtester nutzen dieselbe Indexlogik.

### Phase 2 — Evolution (`evolver.py`)

Der Evolver bewertet jedes Genome **pro Markt-Regime** separat:

```
Für jedes Regime (TREND / RANGE / NEUTRAL):
  Score_regime = winrate_regime × avg_move_pct × log(1 + occ_regime)

Ein Regime wird aktiviert wenn:
  - occ_regime  ≥ min_samples (statistisch belastbar)
  - winrate     ≥ 45%
  - score       ≥ 0.08

active_regimes = Liste der qualifizierenden Regime
  → z.B. ["RANGE", "NEUTRAL"]  (TREND zu unzuverlässig → nicht gehandelt)

Genome ist aktiv (active=1) wenn mindestens ein Regime qualifiziert.

Decay-Weighting (Occurrence-Decay, volatilitätsadjustiert):
  effective_occ = occ_regime × decay
  score_regime  = winrate × avg_move × log(1 + effective_occ)

  decay = e^(−age_days / effective_half_life)
  effective_half_life = half_life_days / vol_factor

  vol_factor = ATR / ATR_MA (aktuelle Marktvolatilität):
    vol_factor = 1.0 → half_life = 180d  (normal)
    vol_factor = 2.0 → half_life = 90d   (hohe Vol → schnellerer Decay)
    vol_factor = 0.5 → half_life = 360d  (niedrige Vol → langsamerer Decay)
```

**Beispiel:** Ein Genome mit 3 Regime-Profilen:

| Regime  | Samples | Winrate | Score  | Status   |
|---------|---------|---------|--------|----------|
| TREND   | 120     | 38%     | 0.06   | inaktiv  |
| RANGE   | 210     | 64%     | 0.41   | **aktiv** |
| NEUTRAL | 180     | 52%     | 0.19   | **aktiv** |

→ `active_regimes = ["RANGE", "NEUTRAL"]` — wird nur in diesen Phasen gehandelt.

### Phase 3 — Live-Trading

```
Jeder Cronjob-Lauf:
  1. Letzte 6 Kerzen codieren
  2. Sequenzen der Länge 4/5/6 gegen DB prüfen
  3. Bestes aktives Genome (höchster Score) → Signal
  4. Entry: Market-Order (sofort bei Sequenz-Abschluss)
  5. SL: Low/High der Sequenz-Kerzen (fester Trigger)
  6. Trailing Stop: aktiviert bei 2:1 R:R, Callback 1% (Bitget nativ)

Nach Trade-Abschluss:
  → Self-Learning: Trade-Ergebnis in Genome-DB schreiben
  → Winrate + Score werden für nächsten Evolver-Lauf aktualisiert
```

### Beispiel-Output (Live-Signal)

```
[Genome Signal]
  Sequenz:   B2H-NL | B3H-UH | S1L-DL | B2H-NH
  Richtung:  LONG
  Regime:    RANGE
  Score:     0.41
  Winrate:   64.3%  (RANGE: 134/210)
  Samples:   210    (RANGE-Regime)
  Entry:     ~43.250 USDT (Trigger-Limit)
  SL:         42.800 USDT (Sequenz-Low)
  TP:         44.150 USDT (2:1 R:R)
  → Platziere Trigger-Limit-Order...
```

---

## Markt-Regime

Das System erkennt vier Marktphasen und handelt nur in den erlaubten:

```
TREND    — ADX > 25            Klare Richtung, Momentum-Genome profitieren
RANGE    — ADX < 20            Seitwärtsmarkt, Reversal-Genome profitieren
HIGH_VOL — ATR > ATR_MA × 1.5  Unkontrollierte Volatilität → immer blockiert
NEUTRAL  — sonst               Übergangsphase, vorsichtiger Handel möglich
```

**Warum das wichtig ist:** Ein Genome das im Range-Markt 64% Winrate hat,
kann im Trend 38% verlieren — und umgekehrt. Der Regime-Filter ist die
wirksamste Einzelmaßnahme gegen Fehlsignale.

---

## Genome-Datenbank

SQLite unter `artifacts/db/genome.db`.
Eine Zeile pro Genome (eindeutig durch Sequenz + Markt + Timeframe + Richtung):

| Feld | Beispiel | Bedeutung |
|---|---|---|
| `genome_id` | `a3f2b9c1...` | MD5-Hash (eindeutiger Schlüssel) |
| `sequence` | `B2H-NL\|B3H-UH\|S1L-DL\|B2H-NH` | Gen-Sequenz |
| `market` | `BTC/USDT:USDT` | Handelspaar |
| `timeframe` | `4h` | Zeitrahmen |
| `direction` | `LONG` | Erwartete Richtung |
| `total_occurrences` | `47` | Wie oft dieses Muster in der History auftrat |
| `wins` | `30` | Wie oft danach die erwartete Bewegung kam |
| `avg_move_pct` | `1.84` | Durchschnittliche Preisbewegung in % |
| `score` | `0.34` | Bester Regime-Score |
| `active` | `1` | Vom Evolver freigegeben |
| `occ_trend` / `wins_trend` | `120` / `46` | Vorkommen + Wins im TREND-Regime |
| `occ_range` / `wins_range` | `210` / `134` | Vorkommen + Wins im RANGE-Regime |
| `occ_neutral` / `wins_neutral` | `180` / `94` | Vorkommen + Wins im NEUTRAL-Regime |
| `active_regimes` | `["RANGE","NEUTRAL"]` | Regime, in denen das Genome gehandelt wird |

---

## Konfiguration (`settings.json`)

```json
{
    "live_trading_settings": {
        "active_strategies": [
            { "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": false },
            { "symbol": "ETH/USDT:USDT", "timeframe": "1h", "active": false }
        ]
    },
    "scan_settings": {
        "discovery_horizon": 5,
        "move_threshold_pct": 1.0,
        "min_samples_to_activate": 80
    },
    "genome_settings": {
        "sequence_lengths": [4, 5, 6],
        "min_score": 0.08,
        "min_winrate": 0.45,
        "half_life_days": 180
    },
    "risk_settings": {
        "risk_per_entry_pct": 1.0,
        "leverage": 5,
        "margin_mode": "isolated",
        "rr_ratio": 2.0,
        "trailing_callback_rate_pct": 1.0
    },
    "optimization_settings": {
        "enabled": true,
        "schedule": {
            "day_of_week": 6,
            "hour": 3,
            "minute": 0,
            "interval": { "value": 7, "unit": "days" }
        },
        "start_capital": 1000,
        "risk_pct": 1.0,
        "max_drawdown_pct": 30,
        "send_telegram_on_completion": true
    }
}
```

> **Automatische Ableitung:** `scan_settings`-Felder werden automatisch nach Timeframe gewählt — nichts muss gesetzt werden:
>
> | Parameter | 1h | 4h | 1d |
> |---|---|---|---|
> | `history_days` | 365d | 730d | 1095d |
> | `discovery_horizon` | 24 Kerzen | 6 Kerzen | 3 Kerzen |
> | `move_threshold_pct` | 0.5% | 1.0% | 2.0% |
> | `min_samples_to_activate` | 8 | 5 | 3 |
>
> Die (Symbol, Timeframe)-Paare werden direkt aus `active_strategies` übernommen.

| Parameter | Erklärung |
|---|---|
| `history_days` | Auto nach Timeframe (4h→730d, 1h→365d, 1d→1095d). Explizit setzen für festen Wert. |
| `discovery_horizon` | Auto nach Timeframe (~1 Tag Lookahead: 4h→6, 1h→24, 1d→3). |
| `move_threshold_pct` | Auto nach Timeframe (4h→1.0%, 1h→0.5%, 1d→2.0%). |
| `min_samples_to_activate` | Auto nach Timeframe (4h→5, 1h→8, 1d→3). |
| `min_score` | Mindest-Score nach Decay (0.08 = guter Startpunkt). |
| `min_winrate` | Mindest-Winrate (0.45 = 45%). |
| `half_life_days` | Halbwertszeit für Score-Decay (180 = 6 Monate). |
| `risk_per_entry_pct` | % des Guthabens als Risiko pro Trade. |
| `rr_ratio` | Risk-Reward-Ratio (2.0 = 1:2) — bestimmt Aktivierungspreis des Trailing Stops. |
| `trailing_callback_rate_pct` | Trailing Stop Callback in % (1.0 = 1% Rückzug vom Peak löst aus). |
| `optimization_settings.enabled` | Automatische wöchentliche Neu-Optimierung ein/aus. |
| `optimization_settings.schedule` | Wochentag + Uhrzeit + Intervall für den Auto-Optimizer. |
| `optimization_settings.max_drawdown_pct` | Maximaler erlaubter Drawdown für Portfolio-Auswahl. |

---

## Installation 🚀

#### 1. Projekt klonen

```bash
git clone https://github.com/Youra82/dnabot.git
cd dnabot
```

#### 2. Installations-Skript ausführen

```bash
chmod +x install.sh
bash ./install.sh
```

Das Skript erstellt die virtuelle Python-Umgebung, installiert alle Abhängigkeiten und legt die Verzeichnisstruktur an.

#### 3. API-Keys eintragen

```bash
cp secret.json.example secret.json
nano secret.json
```

```json
{
    "dnabot": [
        {
            "name": "Main-Account",
            "apiKey": "DEIN_API_KEY",
            "secret": "DEIN_SECRET",
            "password": "DEIN_PASSPHRASE",
            "telegram_bot_token": "DEIN_BOT_TOKEN",
            "telegram_chat_id": "DEINE_CHAT_ID"
        }
    ]
}
```

---

## Workflow

#### 1. Coins und Timeframes einstellen

```bash
nano settings.json
```

```json
"active_strategies": [
    { "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": false },
    { "symbol": "ETH/USDT:USDT", "timeframe": "1h", "active": false }
]
```

#### 2. Genome-Discovery starten (Pipeline)

```bash
./run_pipeline.sh
```

Die Pipeline lädt historische Daten, entdeckt Muster, bewertet sie und zeigt eine Zusammenfassung. Dauert je nach Anzahl der Märkte 10–30 Minuten.

#### 3. Ergebnisse analysieren & Portfolio optimieren

```bash
./show_results.sh
```

| Modus | Funktion |
|---|---|
| **1) Einzel-Backtest** | Simuliert jedes Pair einzeln — zeigt WR, PnL, Drawdown pro Pair. |
| **2) Manuelle Portfolio-Simulation** | Du wählst Pairs aus einer Liste (Nummern oder `alle`), der Bot simuliert das kombinierte Portfolio mit gemeinsamem Kapital-Pool und optionalem Telegram-Versand. |
| **3) Automatische Portfolio-Opt.** | Exhaustive Suche über alle Pair-Kombinationen — der Bot wählt das Team mit maximalem PnL bei gegebenem Max-Drawdown. Schreibt Ergebnis in `settings.json`. Optional: kombinierten Portfolio-Equity-Chart via Telegram senden. |
| **4) Genome Bibliothek** | Top-Patterns, Score-Verteilung und Statistiken aus der Genome-DB. |
| **5) Interaktive Charts** | Candlestick + Entry/Exit-Marker + Equity-Kurve als HTML (Plotly). |

**Portfolio-Simulation (Modus 2 & 3):**
- Alle Trades aller gewählten Pairs werden **chronologisch zusammengeführt**
- Jeder Trade riskiert `risk_pct%` des **aktuellen** Equity (Kompoundierung)
- Constraint: max. 1 Timeframe pro Coin (Bitget erlaubt nur 1 offene Position pro Symbol)

#### 4. Strategien live schalten

Nach der Portfolio-Optimierung (Modus 3) werden die optimalen Strategien automatisch in `settings.json` eingetragen. Alternativ manuell:

```bash
nano settings.json
```

```json
{ "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": true }
```

#### 5. Cronjob einrichten

```bash
crontab -e
```

```cron
# dnabot -> offset 60s (startet auch den Telegram-Listener falls nicht aktiv)
*/15 * * * * /usr/bin/flock -n /home/matola/dnabot/dnabot.lock /bin/sh -c "sleep 60; OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 TF_NUM_INTRAOP_THREADS=1 TF_NUM_INTEROP_THREADS=1 cd /home/matola/dnabot && pgrep -f 'telegram_listener.py' > /dev/null || nohup /home/matola/dnabot/.venv/bin/python3 /home/matola/dnabot/telegram_listener.py >> /home/matola/dnabot/logs/telegram_listener.log 2>&1 & /home/matola/dnabot/.venv/bin/python3 master_runner.py >> /home/matola/dnabot/logs/cron.log 2>&1"
```

> Der `master_runner.py` ruft beim Start automatisch den `auto_optimizer_scheduler.py` auf.
> Dieser prüft ob eine Neu-Optimierung fällig ist und führt sie dann automatisch aus.
> Ein separater Cronjob für wöchentliches Re-Learning ist **nicht nötig**.

---

## Automatische Wochentimer-Optimierung

Der `auto_optimizer_scheduler.py` läuft non-blocking bei jedem `master_runner.py`-Aufruf:

```
master_runner.py startet
    ↓
auto_optimizer_scheduler.py prüft: Ist Optimierung fällig?
    ├── Nein → sofort beendet (kein Overhead)
    └── Ja →
           scan_and_learn.py           (neue Genome discovern + evolven)
               ↓
           run_portfolio_optimizer.py --auto-write
               (bestes Team ermitteln → settings.json aktualisieren)
               ↓
           Telegram: Start + Ende Benachrichtigung
```

Konfiguration in `settings.json` unter `optimization_settings`:

```json
"optimization_settings": {
    "enabled": true,
    "schedule": {
        "day_of_week": 6,
        "hour": 3,
        "minute": 0,
        "interval": { "value": 7, "unit": "days" }
    },
    "start_capital": 1000,
    "risk_pct": 1.0,
    "max_drawdown_pct": 30,
    "send_telegram_on_completion": true
}
```

Manuell erzwingen:

```bash
.venv/bin/python3 auto_optimizer_scheduler.py --force
```

---

## Wissenschaftliche Analysen

Alle 19 Analysen sind unter **einem einzigen interaktiven Befehl** zusammengefasst.
Jede Analyse sendet automatisch einen **Chart + Zusammenfassung via Telegram**.

```bash
./run_analysis.sh                 # interaktives Menü
./run_analysis.sh --no-telegram   # nur lokale Charts, kein Telegram
```

```
=======================================================
  dnabot — Wissenschaftliche Analysen
=======================================================

  ── Priorität 1: Fundament ──────────────────────────
   1) Walk-Forward Lookback-Analyse
   2) Slippage & Fee Impact
   3) Monte Carlo Simulation
   4) Bootstrap Signifikanztest

  ── Priorität 2: Direkte Gewinnoptimierung ──────────
   5) RR-Ratio Optimierung          (Walk-Forward)
   6) Score Threshold Sweep         (Walk-Forward)
   7) Trailing Callback Optimierung (Walk-Forward)
   8) Parameter Sensitivity         (Tornado-Diagramm)

  ── Priorität 3: Systemverbesserung ─────────────────
   9) Multi-Timeframe Confirmation
  10) Genome Decay Analysis
  11) Anti-Korrelations-Portfolio
  12) Kelly Position Sizing

  ── Priorität 4–6: Feintuning & Portfolio ───────────
  13) Regime Performance Analysis
  14) Sequenzlängen-Analyse
  15) Confluence Score
  16) Volatilitäts-Filter Optimierung
  17) Tageszeit-Analyse
  18) Regime-adaptive Parameter
  19) Drawdown Duration Analysis

   0) Alle 19 Analysen nacheinander ausführen
```

Charts werden unter `docs/` gespeichert und via Telegram gesendet.

> **Voraussetzung für Analysen 2–8, 11–12, 14–19:** Backtest-Daten müssen vorhanden sein.
> Erst mit einem langen Zeitraum generieren:
> ```bash
> ./show_results.sh  # → Mode 1 → Startdatum: 2025-01-01
> ```

---

### 1) Walk-Forward Lookback-Analyse

**Frage:** Wie viele Wochen zurück soll der Auto-Optimizer schauen um das beste Portfolio zu wählen?

Der wöchentliche Auto-Optimizer läuft einmal pro Woche und wählt Pairs anhand ihrer Performance im Lookback-Fenster. Zu kurz = zu wenig Daten, reaktiv. Zu lang = blind für aktuelle Marktlage.

**Methode:** Rolling Walk-Forward ohne Lookahead. Für jeden Lookback (1, 2, 4, 8, 12, 26 Wochen) wird jede Woche simuliert: In-Sample → Portfolio wählen → Out-of-Sample → Equity akkumulieren. Alle Lookbacks auf demselben OOS-Zeitraum (fairer Vergleich).

**Ergebnis:** Equity-Kurven aller Lookbacks + Calmar-Tabelle via Telegram.

#### Beispiel-Ergebnis (167 Wochen Daten, 141 Test-Wochen OOS)

![Walk-Forward Lookback-Vergleich](docs/walkforward_latest.png)

```
Lookback  1W   DD=12.4% | Calmar= 6559 | Leerwochen=53  ← zu wenig Daten
Lookback  2W   DD=19.5% | Calmar=23885 | Leerwochen=11  ← ★ bestes Calmar
Lookback  4W   DD=25.0% | Calmar=21937 | Leerwochen= 7
Lookback  8W   DD=33.9% | Calmar= 9220 | Leerwochen= 4
Lookback 12W   DD=21.4% | Calmar=21898 | Leerwochen= 4
Lookback 26W   DD=24.8% | Calmar=18118 | Leerwochen= 4
```

**Leerwochen** = Wochen ohne qualifizierendes Pair (zu wenig Trades im Fenster).

Ergebnis in `settings.json` übernehmen:
```json
"optimization_settings": { "backtest_lookback_weeks": 2 }
```

---

### 2) Slippage & Fee Impact

**Frage:** Ist der Bot nach realen Gebühren und Slippage noch profitabel?

**Methode:** Zwei Sweeps auf allen Backtest-Trades:
- **Gebühren-Sweep:** 0% bis 0.20% pro Seite (Bitget Taker = 0.06%) — jeweils Round-Trip berechnet
- **Slippage-Sweep:** 0% bis 0.20% zusätzlicher Verlust bei SL-Execution (fixer Gebührensatz 0.06%)

**Ergebnis:** Balkendiagramm (PnL% je Gebühr/Slippage) + **Break-Even-Gebühr** via Telegram.

**Was man lernt:** Wenn der Break-Even bei 0.04%/Seite liegt, ist das System sehr empfindlich — schon 0.06% Taker-Gebühr macht es unrentabel. Liegt er bei 0.15%+, hat das System robuste Gewinnmargen.

---

### 3) Monte Carlo Simulation

**Frage:** Was ist das realistisch schlechteste Ergebnis? Wie hoch ist das Ruin-Risiko?

**Methode:** 10.000 zufällige Permutationen der echten Trade-Sequenz. Jede Permutation simuliert dieselben Trade-Ergebnisse in anderer Reihenfolge. Der Median und die Perzentile zeigen die Verteilung möglicher Ergebnisse.

**Ergebnis:** Zwei Histogramme (Final-PnL-Verteilung + MaxDD-Verteilung) mit Perzentil-Markierungen via Telegram.

| Kennzahl | Bedeutung |
|---|---|
| 5. Perzentil | Das schlechteste Ergebnis in 95% der Fälle |
| Median | Erwartetes mittleres Ergebnis |
| 95. Perzentil | Das beste Ergebnis in 95% der Fälle |
| Ruin-Wahrsch. | Anteil Pfade mit Equity < 50% des Startkapitals |

**Interpretation:** Ruin-Wahrscheinlichkeit < 1% = robust. > 10% = Risk% reduzieren.

---

### 4) Bootstrap Signifikanztest

**Frage:** Sind die Genome-Win-Raten statistisch signifikant oder nur Zufall?

**Methode:** Binomialtest pro aktivem Genome. Nullhypothese: Win-Rate = 50% (Zufall). Einseitiger Test: Ist die gemessene WR signifikant **über** 50%? Ergebnis: p-Wert pro Genome. Signifikant = p < 0.05.

**Ergebnis:** Scatter (WR vs. Sample-Größe, grün = signifikant) + p-Wert-Histogramm via Telegram.

**Was man lernt:** Wenn 80% der aktiven Genome statistisch signifikant sind → das System erkennt echte Muster. Wenn nur 30% signifikant sind → viele Genome sind Zufall und sollten durch strengere `min_score`/`min_winrate` gefiltert werden.

**Voraussetzung:** `pip install scipy` (einmalig auf dem VPS).

---

### 5) RR-Ratio Optimierung (Walk-Forward)

**Frage:** Ist 2:1 R:R wirklich optimal oder ist 1.5:1 oder 3:1 besser?

**Methode:** Walk-Forward für RR-Werte 1.0, 1.5, 2.0, 2.5, 3.0 — identisch zu Analyse 1, aber der Parameter ist das RR. Kein Lookahead: jede Woche wird mit dem in der Vorwoche gewählten RR simuliert.

**Ergebnis:** Equity-Kurven aller RR-Werte + Calmar-Tabelle via Telegram. Optimaler Wert direkt in `settings.json` übernehmbar.

**Direkte Auswirkung:** Das RR bestimmt den Aktivierungspreis des Trailing Stops. Zu hoch = Trailing Stop wird selten aktiviert (viele SL-Hits). Zu niedrig = früher in Gewinn aber kleiner Gewinn pro Win.

---

### 6) Score Threshold Sweep (Walk-Forward)

**Frage:** Welcher `min_score` liefert das beste Verhältnis aus Trade-Anzahl und Win-Rate?

**Methode:** Walk-Forward für min_score-Werte 0.01, 0.05, 0.08, 0.12, 0.15, 0.20, 0.30.
- Niedrig = mehr Trades, niedrigere Win-Rate (mehr Rauschen erlaubt)
- Hoch = weniger Trades, höhere Win-Rate (nur starke Signale)

**Ergebnis:** Equity-Kurven aller Schwellwerte + Calmar-Tabelle via Telegram. Optimaler Wert direkt in `settings.json` übernehmbar (`genome_settings.min_score`).

---

### 7) Trailing Callback Optimierung (Walk-Forward)

**Frage:** Ist 1% Callback optimal oder verlässt man Gewinner zu früh / zu spät?

**Methode:** Walk-Forward für Callback-Werte 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0%.
- Klein = enger Trailing Stop → wird früher ausgelöst, weniger Gewinn pro Trade
- Groß = weiter Trailing Stop → mehr Gewinn bei starken Bewegungen, mehr Gegenbewegung toleriert

**Ergebnis:** Equity-Kurven + Calmar-Tabelle via Telegram. Optimaler Wert direkt in `settings.json` übernehmbar (`risk_settings.trailing_callback_rate_pct`).

---

### 8) Parameter Sensitivity Analysis

**Frage:** Wie robust ist das System? Machen kleine Parameteränderungen alles kaputt?

**Methode:** Jeder der 5 Haupt-Parameter wird ±30% variiert (in 7 Stufen: −30%, −20%, −10%, 0%, +10%, +20%, +30%). Calmar-Änderung gegenüber Basis wird gemessen.

| Parameter | Basis | Testbereich |
|---|---|---|
| `rr_ratio` | 2.0 | 1.4 – 2.6 |
| `min_score` | 0.08 | 0.056 – 0.104 |
| `min_winrate` | 0.45 | 0.315 – 0.585 |
| `half_life_days` | 180 | 126 – 234 |
| `trailing_callback_pct` | 1.0 | 0.7 – 1.3 |

**Ergebnis:** Tornado-Diagramm via Telegram.
- Breiter Balken = sensitiver Parameter = Overfitting-Risiko
- Schmaler Balken = robuster Parameter = System ist stabil

**Was man lernt:** Wenn `min_score` einen sehr breiten Balken hat → der Bot ist stark vom Schwellwert abhängig → Vorsicht mit manuellen Anpassungen.

---

### 9) Multi-Timeframe Confirmation

**Frage:** Performen Signale besser wenn sie gleichzeitig auf mehreren Coins/Pairs auftreten?

**Methode:** Trades werden nach Anzahl gleichzeitiger Signale im Zeitfenster (Standard: 2h) gruppiert. `n=1` = Einzelsignal, `n=2` = zwei Signals gleichzeitig, `n=3+` = starkes Confluence.

**Ergebnis:** Win-Rate und Calmar nach Confluence-Stufe via Telegram.

**Was man lernt:** Wenn `n=2+` signifikant besser ist → zukünftig nur bei Mehrfach-Bestätigung handeln (→ Confluence Score, Analyse 15). Wenn kein Unterschied → Einzelsignale sind ausreichend.

---

### 10) Genome Decay Analysis

**Frage:** Verlieren Genome mit der Zeit ihre Vorhersagekraft? Ist `half_life_days=180` korrekt?

**Methode:** Aktive Genome werden nach Alter (Tage seit `last_seen`) in Altersgruppen eingeteilt: 0–30d, 30–60d, 60–90d, 90–180d, 180–365d, >365d. Ø Win-Rate und Ø Score pro Gruppe.

**Ergebnis:** Balkendiagramm (WR nach Alter) + Score-Verlauf vs. theoretischem Decay-Verlauf via Telegram.

**Was man lernt:**
- Win-Rate fällt mit dem Alter → Decay ist real → `half_life_days` korrekt gesetzt
- Win-Rate konstant über alle Alter → Decay zu aggressiv → `half_life_days` erhöhen
- Stark fallende Win-Rate nach 60d → Decay zu langsam → `half_life_days` reduzieren

---

### 11) Anti-Korrelations-Portfolio

**Frage:** Welche Pairs verlieren und gewinnen selten gleichzeitig?

**Methode:** Aus den Backtest-Trades wird pro Pair eine wöchentliche PnL-Zeitreihe erstellt. Pearson-Korrelationsmatrix aller Pairs. Negativ korrelierte Pairs kompensieren sich gegenseitig → geringerer Portfolio-Drawdown.

**Ergebnis:** Heatmap der Korrelationsmatrix + Liste der besten (anti-korrelierten) Pair-Kombinationen via Telegram.

**Was man lernt:** Pairs die stark positiv korrelieren (>0.7) sollten nicht gleichzeitig im Portfolio sein — sie fallen und steigen gemeinsam, ohne Diversifikationsvorteil.

---

### 12) Kelly Position Sizing

**Frage:** Wie viel sollte man pro Genome riskieren — mathematisch optimal?

**Methode:** Kelly-Kriterium pro aktivem Genome:
```
Kelly% = (WR × RR − (1−WR)) / RR
Half-Kelly (empfohlen) = Kelly% / 2
```

Genome mit hoher Win-Rate und gutem RR dürfen mehr riskiert werden. Schwache Genome weniger.

**Ergebnis:** Ranking aller aktiven Genome nach optimalem Kelly-Einsatz + Vergleich aktueller vs. Kelly-Risk via Telegram.

**Was man lernt:** Wenn das aktuelle `risk_per_entry_pct=5%` für die meisten Genome deutlich über Half-Kelly liegt → das Risiko ist zu hoch und sollte reduziert werden. Kelly gibt die mathematisch optimale Wachstumsrate des Kapitals.

---

### 13) Regime Performance Analysis

**Frage:** In welchen Marktphasen funktioniert welches Genome am besten?

**Methode:** Direkt aus der Genome-Datenbank — die per-Regime-Spalten (`occ_trend/wins_trend`, `occ_range/wins_range`, `occ_neutral/wins_neutral`) werden aggregiert und verglichen.

**Ergebnis:** Balkendiagramm (Win-Rate pro Regime pro Coin/Pair) + Tabelle via Telegram.

**Was man lernt:** Wenn TREND-Regime durchgängig schlechte Win-Raten zeigt → TREND aus `allowed_regimes` in `settings.json` entfernen. Das kann die Gesamtperformance erheblich verbessern.

```json
"genome_settings": { "allowed_regimes": ["RANGE", "NEUTRAL"] }
```

---

### 14) Sequenzlängen-Analyse

**Frage:** Sind 4er, 5er oder 6er Sequenzen profitabler? Erzeugen 5er mehr Rauschen?

**Methode:** Walk-Forward getrennt für jede Sequenzlänge: einmal nur 4er Sequenzen, einmal nur 5er, einmal nur 6er, einmal alle kombiniert. Calmar-Vergleich.

**Ergebnis:** Equity-Kurven nach Sequenzlänge via Telegram.

**Was man lernt:** Wenn 5er Sequenzen deutlich schlechter abschneiden → aus `sequence_lengths` entfernen. Das vereinfacht das System und reduziert Rauschen in der Genome-DB.

```json
"genome_settings": { "sequence_lengths": [4, 6] }
```

---

### 15) Confluence Score

**Frage:** Was passiert wenn man nur handelt wenn mehrere Genome gleichzeitig dasselbe signalisieren?

**Methode:** Trades aus dem Backtest werden nach Anzahl gleichzeitiger gleichgerichteter Signale im Zeitfenster gefiltert. Simuliert: `min_confluence=1` (alles), `=2` (2+ Genome), `=3` (starkes Signal).

**Ergebnis:** Vergleich Win-Rate, Trade-Anzahl und Calmar je Confluence-Schwellwert via Telegram.

**Was man lernt:** Höhere Confluence = weniger Trades aber tendenziell bessere Qualität. Der Trade-off zwischen Trade-Anzahl und WR zeigt den optimalen Schwellwert.

---

### 16) Volatilitäts-Filter Optimierung

**Frage:** Der HIGH_VOL-Filter blockiert bei ATR > 1.5× ATR-MA — ist das der richtige Schwellwert?

**Methode:** Simuliert das Portfolio mit verschiedenen ATR-Schwellwerten: 1.0×, 1.5×, 2.0×, 2.5×, 3.0×.
- Niedrig = mehr Trades geblockt (konservativ)
- Hoch = mehr Trades erlaubt (aggressiv, auch bei hoher Volatilität)

**Ergebnis:** Calmar-Vergleich aller Schwellwerte via Telegram.

**Was man lernt:** Wenn 2.0× besser ist als 1.5× → der aktuelle Filter ist zu aggressiv und blendet profitable Trades aus. Direkter Handlungshinweis für den Code.

---

### 17) Tageszeit-Analyse

**Frage:** Performen Genome-Signale zu bestimmten Tageszeiten besser?

**Methode:** Alle Backtest-Trades werden nach Einstiegs-Uhrzeit (UTC) in Sessions gruppiert:
- **Asia:** 01:00–09:00 UTC
- **Europe:** 09:00–17:00 UTC
- **US:** 17:00–01:00 UTC

Win-Rate, PnL und Calmar pro Session.

**Ergebnis:** Balkendiagramm + Heatmap (Stunde vs. Win-Rate) via Telegram.

**Was man lernt:** Krypto handelt 24/7 — wenn bestimmte Stunden konstant negative Calmar zeigen, kann man Entry-Zeiten einschränken. Oft zeigt sich: London/NY-Overlap (13–17 UTC) ist liquider und Signale sind zuverlässiger.

---

### 18) Regime-adaptive Parameter

**Frage:** Sollte man in TREND anderen RR verwenden als in RANGE?

**Methode:** Simuliert das Portfolio mit verschiedenen RR-Gruppen je nach Timeframe:
- **Kurzfristig (1h/2h):** RR 1.5, 2.0, 2.5
- **Mittelfristig (4h/6h):** RR 2.0, 2.5, 3.0
- Alle Kombinationen werden verglichen.

**Ergebnis:** Heatmap (Timeframe-Gruppe vs. RR-Wert → Calmar) via Telegram.

**Was man lernt:** Wenn 1h-Pairs mit RR=1.5 besser performen als mit RR=2.0 → für kurzfristige Pairs sollte ein niedrigerer RR gesetzt werden. Grundlage für future regime-adaptive Konfiguration.

---

### 19) Drawdown Duration Analysis

**Frage:** Wie lange dauern Drawdown-Phasen? Wie lange muss man einen Verlust aussitzen?

**Methode:** Aus der chronologischen Equity-Kurve werden alle Drawdown-Perioden extrahiert: Beginn (Abweichung vom Peak), Tief (maximale Abweichung), Ende (Rückkehr auf altes High). Dauer und Tiefe jeder Periode werden statistisch ausgewertet.

**Ergebnis:** Drei Charts via Telegram:
1. **Scatter:** Drawdown-Tiefe vs. Erholungsdauer (Zusammenhang?)
2. **Histogramm:** Verteilung der Erholungsdauern
3. **Equity-Kurve:** Visuell mit rot markierten Drawdown-Zonen

| Kennzahl | Bedeutung |
|---|---|
| Ø Erholungsdauer | Wie lange man typischerweise aussitzen muss |
| 90. Perzentil | In 90% der Fälle war die Erholung kürzer als X Tage |
| Längste Erholung | Extremfall — mentale Vorbereitung |

**Was man lernt:** Wenn die durchschnittliche Erholungsdauer > 60 Tage → das System reagiert langsam auf Verluste. Ursache prüfen: zu wenige Trades? Zu enger SL? Oder einfach normales Marktverhalten?

---

### Option 0 — Alle Analysen auf einmal

```bash
./run_analysis.sh
# → Auswahl: 0
```

Führt alle 19 Analysen nacheinander mit Standard-Parametern aus (Kapital 100 USDT, Risk 2.5%). Analysen die keine Daten finden (z.B. leere Genome-DB oder keine Backtest-Daten) werden übersprungen ohne Fehler. Alle Charts landen in `docs/` und werden via Telegram gesendet.

---

## Tägliche Verwaltung & Wichtige Befehle ⚙️

#### Logs ansehen

```bash
# Live mitverfolgen
tail -f logs/cron.log

# Nach Fehlern suchen
grep -i "ERROR" logs/cron.log

# Discovery-Log
tail -f logs/scan_and_learn.log

# Auto-Optimizer
tail -f logs/auto_optimizer_trigger.log

# Einzelnes Symbol
tail -n 100 logs/dnabot_BTCUSDTUSDT_4h.log

# Letzte 200 Zeilen der zentralen Log-Datei
tail -n 200 logs/cron.log
```

#### Telegram-Listener (GenCode-Abfrage per Nachricht)

Der `telegram_listener.py` ist ein dauerhaft laufender Dienst, der auf Telegram-Nachrichten reagiert.

**Befehl:** Sende einfach das Wort `Gen` an den Bot.

**Antwort:** Für jede aktive Strategie erhältst du:
- Die **letzten 4 kodierten Kerzen** (GenCode) mit lesbarer Beschreibung (Richtung, Körpergröße, Volatilität, Wick, Volumen)
- Den **wahrscheinlichsten nächsten GenCode** — basierend auf historischen DB-Mustern (die letzten 3 Gene als Prefix → häufigstes 4. Gen in der DB)
- Anzahl der historischen Fälle + Datenlage-Bewertung

**Beispielausgabe:**
```
🧬 dnabot GenCode-Report
17.03.2026 22:45

────────────────────────────────
📊 DOGE (2h) · Regime: RANGE
  -3  S1L-DL      🔴 Bearish · klein · Vola↓ · ↓Wick · vol↓
  -2  B3H-UH      🟢 Bullish · groß · Vola↑ · ↑Wick · vol↑
  -1  S2L-BL      🔴 Bearish · mittel · Vola↓ · ↕Wick · vol↓
  »   S3H-UH      🔴 Bearish · groß · Vola↑ · ↑Wick · vol↑  ← jetzt
🔮 Nächste Kerze:
     B2H-NH      🟢 Bullish · mittel · Vola↑ · kein Wick · vol↑
     47 Fälle in DB · starke Basis
```

**Start (einmalig manuell, danach übernimmt der Cronjob):**

```bash
cd ~/dnabot && nohup .venv/bin/python3 telegram_listener.py >> logs/telegram_listener.log 2>&1 &
```

> **Hinweis:** Der Cronjob startet den Listener automatisch beim nächsten Lauf (alle 15 Min).
> Nach einem Neustart des VPS also bis zu 15 Minuten warten — oder obigen Befehl manuell ausführen.

**Log:**
```bash
tail -f logs/telegram_listener.log
```

---

#### Manueller Start (Test)

Einmalig manuell ausführen — nützlich zum Testen oder nach einem Update:

```bash
cd ~/dnabot && .venv/bin/python3 master_runner.py
```

#### Auto-Optimizer: Status & manueller Start

Prüfen wann der Auto-Optimizer zuletzt lief und wann er wieder fällig ist:

```bash
# Letzter Optimierungszeitpunkt
cat ~/dnabot/artifacts/cache/.last_optimization_run

# Optimizer-Log (läuft er? überspringt er? Fehler?)
tail -f ~/dnabot/logs/auto_optimizer_trigger.log

# Optimierung sofort erzwingen (ignoriert den Zeitplan)
cd ~/dnabot && .venv/bin/python3 auto_optimizer_scheduler.py --force
```

> **Intervall:** Standardmäßig alle 7 Tage (konfigurierbar in `optimization_settings.schedule`).
> Der Optimizer testet automatisch Risikowerte von 1%–5% und wählt das Portfolio
> mit dem höchsten Final Equity — solange MaxDD unter dem konfigurierten Limit bleibt.
> `settings.json` wird **nur überschrieben wenn das neue Ergebnis besser als das aktuelle ist.**

#### Genome-Discovery manuell starten

```bash
# Alle konfigurierten Pairs
./run_pipeline.sh

# Nur ein bestimmtes Pair
.venv/bin/python3 scan_and_learn.py --symbol BTC/USDT:USDT --timeframe 4h
```

#### Tests ausführen

```bash
./run_tests.sh
```

Führt alle Pytest-Tests aus (Sicherheitscheck vor dem Live-Betrieb).

#### Bot aktualisieren

```bash
./update.sh
```

Sichert automatisch `secret.json` vor dem `git reset --hard`.

#### Genome-Datenbank zurücksetzen

```bash
# Achtung: löscht alle erlernten Muster und Backtest-Ergebnisse!
rm artifacts/db/genome.db
rm -f artifacts/results/backtest_*.json
./run_pipeline.sh
```

#### Aktives Portfolio löschen

```bash
python3 -c "
import json
s = json.load(open('settings.json'))
s['live_trading_settings']['active_strategies'] = []
json.dump(s, open('settings.json','w'), indent=2, ensure_ascii=False)
"
```

Danach beim nächsten `./show_results.sh` → Modus 3 findet kein aktives Portfolio mehr und fragt ob das neue eingetragen werden soll.

---

## Wichtige Regeln

- `secret.json` ist **nicht in Git** — wird von `update.sh` gesichert
- `artifacts/db/genome.db` ist **nicht in Git** — bleibt nach Updates erhalten
- `artifacts/tracker/` ist **nicht in Git** — enthält den offenen Trade-Status pro Symbol
- Immer erst `./run_pipeline.sh` bevor Live-Trading aktiviert wird
- Genome-Discovery wird automatisch wöchentlich wiederholt (Auto-Optimizer)
- Genome mit weniger als 5 Samples (4h) werden grundsätzlich nicht gehandelt

---

## Coin & Timeframe Empfehlungen

DNABot ist eine **Genome-basierte Pattern-Strategie** — er kodiert Kerzen als Gen-Strings (z.B. `B3H-UH`) und sucht in der Datenbank nach 4/5/6-Kerzen-Sequenzen mit statistisch valider Win-Rate. Benötigt: Coins mit wiederkehrenden, lernbaren Kerzenmustern und ausreichend historische Daten für die Genome-Datenbank.

### Effektive Zeitspannen der Sequenz-Fenster

| TF | 4-Kerzen-Sequenz | 6-Kerzen-Sequenz | Muster-Qualität | Geeignet |
|---|---|---|---|---|
| 15m | 1h | 1.5h | Noise-dominiert | ❌ |
| 30m | 2h | 3h | Marginal | ⚠️ |
| 1h | 4h | 6h | Intraday-Session | ✅ |
| **2h** | **8h** | **12h** | **Mehrere Sessions** | **✅✅** |
| **4h** | **16h** | **24h** | **Voller Handelstag** | **✅✅** |
| **6h** | **24h** | **36h** | **1.5 Tage — Swing** | **✅✅** |
| 1d | 4d | 6d | Wochen-Muster | ✅ |

Auf 15m/30m sind 4-6 Kerzen nur 1-3 Stunden — zu kurz für statistisch bedeutsame wiederkehrende Muster. Ab 2h deckt eine Sequenz komplette Handelssessions ab. Die Genome-Datenbank braucht außerdem ausreichend historische Kerzen für die Discovery-Phase.

### Coin-Eignung

| Coin | Kerzenmuster-Qualität | Wiederholbarkeit | DB-Datenbasis | Bewertung |
|---|---|---|---|---|
| **BTC** | Exzellent — institutionelle Muster | Sehr hoch durch globale Beobachtung | Längste Historie, beste Basis | ✅✅ Beste Wahl |
| **ETH** | Exzellent — klare, strukturierte Kerzen | Sehr hoch | Sehr gute Datenbasis | ✅✅ Sehr gut |
| **SOL** | Sehr gut — klare Richtungskerzen | Hoch | Gute Datenbasis ab 2020 | ✅ Gut |
| **BNB** | Gut — stabile, wiederholende Muster | Gut | Lange Datenbasis | ✅ Gut |
| **XRP** | Gut — klare Kerzenstruktur | Gut, besonders in Range-Phasen | Sehr lange Datenbasis | ✅ Gut |
| **AVAX** | Gut — ordentliche Kerzenformen | Mittel-hoch | Ausreichend ab 2020 | ✅ Gut |
| **LTC** | Gut — BTC-korreliert | Gut | Lange Datenbasis | ✅ Gut |
| **ADA** | Mittel — wenig Körper in Seitwärts | Mittel | Gute Datenbasis | ⚠️ Mittel |
| **ARB** | Mittel — junge Datenbasis | Noch aufbauend | Kurze Datenbasis (ab 2023) | ⚠️ Mittel |
| **DOT** | Mittel — oft indifferente Kerzen | Gering | Ausreichend | ⚠️ Mittel |
| **LINK** | Mittel — explosiv in Bull, träge sonst | Ungleichmäßig | Ausreichend | ⚠️ Mittel |
| **DOGE** | Schlecht — sentiment-getriebene Muster | Niedrig, nicht statistisch | Vorhanden aber unbrauchbar | ❌ Schlecht |
| **SHIB/PEPE** | Nicht lernbar — Pump-Candles | Keine Wiederholbarkeit | Zu kurze Datenbasis | ❌❌ Nicht geeignet |

### Empfohlene Kombinationen (Ranking)

| Rang | Kombination | Begründung |
|---|---|---|
| 🥇 1 | **BTC 4h / 6h** | Beste institutionelle Kerzenmuster, längste Datenbasis für DB |
| 🥇 1 | **ETH 4h / 6h** | Ähnlich BTC, exzellente Sequenz-Qualität |
| 🥈 2 | **BTC 2h / ETH 2h** | Mehr Sequenzen für schnelleres DB-Befüllen |
| 🥉 3 | **SOL 4h** | Klare Directional-Candles, gute Sequenzabdeckung |
| 4 | **BNB 4h** | Stabile, wiederholende Muster |
| 4 | **XRP 4h** | Gute Sequenzen in Range- und Trendphasen |
| 4 | **LTC 4h** | BTC-Muster, gute Datenbasis |
| 5 | **AVAX 4h** | Gute Bullmarkt-Sequenzen |
| ❌ | **Alles auf 15m / 30m** | Sequenzen zu kurz, kein statistischer Wert |
| ❌ | **DOGE / SHIB** | Muster nicht wiederholbar, kein Lerneffekt |

> **Hinweis:** Das Self-Learning greift nach jedem Trade. Je mehr Trades auf einem Coin/TF-Paar, desto besser wird die Genome-DB. BTC 4h liefert die schnellste und zuverlässigste DB-Reife.


---

## Abhängigkeiten

```
ccxt==4.3.5      # Exchange-Verbindung (Bitget)
pandas==2.1.3    # Datenverarbeitung
ta==0.11.0       # ATR-Berechnung (für Encoding + Regime)
numpy            # Array-Operationen
requests==2.31.0 # Telegram
plotly           # Interaktive Charts (show_results.sh Modus 5)
sqlite3          # Built-in Python — keine Installation nötig
```

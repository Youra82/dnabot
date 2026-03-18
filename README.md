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
  4. Entry: Trigger-Limit-Order (±0.05% Delta)
  5. SL: Low/High der Sequenz-Kerzen
  6. TP: 2:1 R:R

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
        "rr_ratio": 2.0
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
| `rr_ratio` | Risk-Reward-Ratio (2.0 = 1:2). |
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

#### Bot aktualisieren

```bash
./update.sh
```

Sichert automatisch `secret.json` vor dem `git reset --hard`.

#### Genome-Datenbank zurücksetzen

```bash
# Achtung: löscht alle erlernten Muster!
rm artifacts/db/genome.db
./run_pipeline.sh
```

---

## Wichtige Regeln

- `secret.json` ist **nicht in Git** — wird von `update.sh` gesichert
- `artifacts/db/genome.db` ist **nicht in Git** — bleibt nach Updates erhalten
- `artifacts/tracker/` ist **nicht in Git** — enthält den offenen Trade-Status pro Symbol
- Immer erst `./run_pipeline.sh` bevor Live-Trading aktiviert wird
- Genome-Discovery wird automatisch wöchentlich wiederholt (Auto-Optimizer)
- Genome mit weniger als 5 Samples (4h) werden grundsätzlich nicht gehandelt

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

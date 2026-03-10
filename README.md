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
├── scan_and_learn.py          # Haupt-Lernprozess (Discovery + Evolver)
├── master_runner.py           # Cronjob-Orchestrator für Live-Trading
├── run_pipeline.sh            # Vollständige Pipeline (Discovery → Report)
├── install.sh                 # Erstinstallation auf VPS
├── update.sh                  # Git-Update (sichert secret.json)
├── settings.json              # Konfiguration
├── secret.json                # API-Keys (nicht in Git)
│
└── src/dnabot/
    ├── genome/
    │   ├── encoder.py         # Kerze → Gen-String
    │   ├── database.py        # SQLite-Interface (Genome-Library)
    │   ├── discovery.py       # Pattern-Mining aus Historien-Daten
    │   └── evolver.py         # Scoring + Aktivierung/Deaktivierung
    │
    ├── strategy/
    │   ├── genome_logic.py    # Aktuelle Kerzen vs. DB → Signal
    │   └── run.py             # Entry Point für eine Strategie
    │
    ├── analysis/
    │   ├── backtester.py      # Historische Simulation
    │   └── show_results.py    # Report: Genome-Library + Backtest
    │
    └── utils/
        ├── exchange.py        # Bitget CCXT Wrapper
        ├── trade_manager.py   # Entry/TP/SL + Self-Learning
        ├── telegram.py        # Telegram-Benachrichtigungen
        └── guardian.py        # Crash-Schutz Decorator
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
  - occ_regime  ≥ 100 Samples (statistisch belastbar)
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

  age_days basiert auf last_seen (letzter echter Beobachtung in Discovery
  oder Live-Trade). Evolver-Updates ändern last_seen NICHT.

  Beispiel: 1000 Samples, 1 Jahr nicht gesehen (half_life=180d):
    decay ≈ 0.13 → effective_occ ≈ 130 (wie ~130 frische Samples)
```

**Beispiel:** Ein Genome mit 3 Regime-Profilen:

| Regime  | Samples | Winrate | Score  | Status   |
|---------|---------|---------|--------|----------|
| TREND   | 120     | 38%     | 0.06   | inaktiv  |
| RANGE   | 210     | 64%     | 0.41   | **aktiv** |
| NEUTRAL | 180     | 52%     | 0.19   | **aktiv** |

→ `active_regimes = ["RANGE", "NEUTRAL"]` — wird nur in diesen Phasen gehandelt.

> **Overfitting-Warnung:** Mit 96 möglichen Genen entstehen theoretisch 96⁴ ≈ 85 Mio.
> mögliche 4er-Sequenzen. In der Praxis sind nur wenige Tausend tatsächlich beobachtbar.
> Sequenzen mit weniger als 100 Samples werden grundsätzlich ignoriert.
> Seltene Muster werden nicht gehandelt.

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

> **Bekannte Einschränkung — ADX-Latenz:** ADX und ATR sind nachlaufende Indikatoren.
> Wenn ein neuer Trend startet, ist der ADX noch zu niedrig — das System klassifiziert
> die Situation als RANGE oder NEUTRAL. Das ist systemimmanent und unvermeidbar.
> Vorteil: Es verhindert auch Fehlsignale am Anfang schwacher Trendbewegungen.

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

### Schema

```sql
CREATE TABLE genomes (
    genome_id           TEXT PRIMARY KEY,
    sequence            TEXT NOT NULL,
    market              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    direction           TEXT NOT NULL,
    seq_length          INTEGER NOT NULL,
    total_occurrences   INTEGER DEFAULT 0,
    wins                INTEGER DEFAULT 0,
    avg_move_pct        REAL DEFAULT 0.0,
    score               REAL DEFAULT 0.0,
    active              INTEGER DEFAULT 0,
    occ_trend           INTEGER DEFAULT 0,
    wins_trend          INTEGER DEFAULT 0,
    occ_range           INTEGER DEFAULT 0,
    wins_range          INTEGER DEFAULT 0,
    occ_neutral         INTEGER DEFAULT 0,
    wins_neutral        INTEGER DEFAULT 0,
    active_regimes      TEXT DEFAULT '[]',
    discovered_at       TEXT NOT NULL,
    last_updated        TEXT NOT NULL
);
```

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
        "min_samples_to_activate": 100
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
    }
}
```

> **Automatische Ableitung:** `symbols`, `timeframes` und `history_days` müssen in `scan_settings` **nicht** definiert werden.
> - Der Scanner übernimmt die (Symbol, Timeframe)-Paare direkt aus `active_strategies` — jede Strategie wird einzeln gescannt. Im Beispiel oben: BTC@4h und ETH@1h, **nicht** BTC@1h oder ETH@4h.
> - Alle `scan_settings`-Felder werden automatisch nach Timeframe gewählt — nichts muss gesetzt werden:
>
> | Parameter | 1h | 4h | 1d |
> |---|---|---|---|
> | `history_days` | 365d | 730d | 1095d |
> | `discovery_horizon` | 24 Kerzen | 6 Kerzen | 3 Kerzen |
> | `move_threshold_pct` | 0.5% | 1.0% | 2.0% |
> | `min_samples_to_activate` | 100 | 80 | 50 |
>
> Sollen mehr Kombinationen gescannt werden als gehandelt werden, können `symbols`, `timeframes` und/oder
> einzelne Parameter in `scan_settings` explizit gesetzt werden — dann gilt dieser Wert fest für alle Paare.

| Parameter | Erklärung |
|---|---|
| `history_days` | **Auto nach Timeframe** (4h→730d, 1h→365d, 1d→1095d …). Explizit setzen für festen Wert. |
| `discovery_horizon` | **Auto nach Timeframe** (~1 Tag Lookahead: 4h→6, 1h→24, 1d→3). |
| `move_threshold_pct` | **Auto nach Timeframe** (4h→1.0%, 1h→0.5%, 1d→2.0%). |
| `min_samples_to_activate` | **Auto nach Timeframe** (4h→80, 1h→100, 1d→50). |
| `min_score` | Mindest-Score (nach Decay, 0.08 = guter Startpunkt) |
| `min_winrate` | Mindest-Winrate (0.45 = 45%) |
| `half_life_days` | Halbwertszeit für Score-Decay (180 = 6 Monate) |
| `risk_per_entry_pct` | % des Guthabens als Risiko pro Trade |
| `rr_ratio` | Risk-Reward-Ratio (2.0 = 1:2) |

---

## Installation 🚀

Führe die folgenden Schritte auf einem frischen Ubuntu-Server (oder lokal) aus.

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

*(Achte darauf, dass der Hauptschlüssel `"dnabot"` heißt — siehe Struktur unten.)*

Speichere mit `Strg + X`, dann `Y`, dann `Enter`.

```json
{
    "dnabot": [
        {
            "name": "Main-Account",
            "apiKey": "DEIN_API_KEY",
            "secret": "DEIN_SECRET",
            "password": "DEIN_PASSPHRASE"
        }
    ],
    "telegram": {
        "bot_token": "DEIN_BOT_TOKEN",
        "chat_id": "DEINE_CHAT_ID"
    }
}
```

---

## Konfiguration & Automatisierung

#### 1. Coins und Timeframes einstellen

Bearbeite `settings.json` und trage deine gewünschten Handelspaare in `active_strategies` ein:

```bash
nano settings.json
```

```json
"active_strategies": [
    { "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": false },
    { "symbol": "ETH/USDT:USDT", "timeframe": "1h", "active": false }
]
```

> Alle `scan_settings`-Parameter werden automatisch nach Timeframe abgeleitet — nichts weiter nötig.

#### 2. Genome-Discovery starten (Pipeline)

```bash
./run_pipeline.sh
```

Die Pipeline lädt historische Daten, entdeckt Muster und bewertet sie. Dauert je nach Märkten 10–30 Minuten.

#### 3. Ergebnisse analysieren

```bash
./show_results.sh
```

- **Modus 1:** Einzel-Backtest — simuliert jedes aktive Pair und zeigt WR, PnL, Drawdown.
- **Modus 2:** Genome Bibliothek — Top-Patterns, Stats und Verteilung aus der DB.
- **Modus 3:** Regime-Analyse — welches Markt-Regime (TREND/RANGE/NEUTRAL) wo am besten funktioniert.
- **Modus 4:** Interaktive Charts — Candlestick + Entry/Exit-Marker + Equity-Kurve als HTML.

#### 4. Strategien live schalten

```bash
nano settings.json
```

Setze für die gewünschten Pairs `"active": true`:

```json
{ "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": true }
```

#### 5. Cronjob einrichten

```bash
crontab -e
```

Füge passend zum Timeframe eine der folgenden Zeilen ein (Pfad anpassen):

```cron
# 1h-Strategien: jede Stunde, 5 Min nach voll
5 * * * * /usr/bin/flock -n /root/dnabot/dnabot.lock /bin/sh -c "cd /root/dnabot && .venv/bin/python3 master_runner.py >> /root/dnabot/logs/cron.log 2>&1"

# 4h-Strategien: alle 4 Stunden
5 */4 * * * /usr/bin/flock -n /root/dnabot/dnabot.lock /bin/sh -c "cd /root/dnabot && .venv/bin/python3 master_runner.py >> /root/dnabot/logs/cron.log 2>&1"

# Wöchentliches Relearning — neue Marktdaten einlernen (Sonntag 2 Uhr)
0 2 * * 0 cd /root/dnabot && .venv/bin/python3 scan_and_learn.py >> /root/dnabot/logs/scan.log 2>&1
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

# Einzelnes Symbol
tail -n 100 logs/dnabot_BTCUSDTUSDT_4h.log
```

#### Manueller Start (Test)

```bash
cd /root/dnabot && .venv/bin/python3 master_runner.py
```

#### Genome-Discovery manuell neu starten

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
- Genome-Discovery sollte mindestens **1x pro Woche** wiederholt werden (neue Marktdaten)
- Genome mit weniger als 80 Samples (4h) werden grundsätzlich nicht gehandelt

---

## Abhängigkeiten

```
ccxt==4.3.5      # Exchange-Verbindung (Bitget)
pandas==2.1.3    # Datenverarbeitung
ta==0.11.0       # ATR-Berechnung (für Encoding + Regime)
numpy            # Array-Operationen
requests==2.31.0 # Telegram
plotly           # Interaktive Charts (show_results.sh Modus 4)
optuna==4.5.0    # Optional: Threshold-Optimierung
sqlite3          # Built-in Python — keine Installation nötig
```

# dnabot — Adaptive Risk-Genome System

Ein selbstlernender Trading-Bot, der über eine **lebende, selbstlernende
Risiko-/Exit-Gen-Datenbank** handelt (`momentum_exit`-Strategie). Entry,
Trailing-Stop, Self-Learning und Telegram-Benachrichtigungen laufen über eine
gemeinsame Live-Infrastruktur.

> **Disclaimer:** Diese Software ist experimentell und dient ausschließlich Forschungszwecken.
> Der Handel mit Kryptowährungen birgt erhebliche finanzielle Risiken. Nutzung auf eigene Gefahr.
> Backtest-Ergebnisse (auch die unten gezeigten) sind keine Garantie für zukünftige Performance.

---

## Grundidee

Eine ausführliche Recherche-Session (2026-08-24, 44 Funde, dokumentiert in
`research_dnabot_direction_calibration.md`) hat systematisch geprüft, ob sich
aus reinem OHLCV-Preis-/Volumenverlauf ein robuster **Richtungs-Edge**
gewinnen lässt — mit jeder methodisch sinnvollen Herangehensweise: exaktem
Kerzen-Gen-Sequenz-Lookup (das ursprüngliche Konzept dieses Bots), kontinuier­
lichen Feature-Modellen, genetischer Suche über den gesamten Feature-Raum,
Bioinformatik-Motiverkennung und zusätzlichen Kontextachsen (BTC-Zustand,
Handelssession). **Keiner dieser Ansätze fand einen Richtungs-Edge, der auf
einem zweiten, unabhängigen Zeitfenster reproduzierte.**

Der einzige Ansatz, der reproduzierte — und zwar deutlich — dreht die Frage
um: Statt "welche Kerzensequenz sagt die Richtung vorher?" lautet sie
"welche Risiko-/Exit-Parameter erzeugen eine positive Kurve, **obwohl** der
Einstieg selbst keinen Vorhersage-Anspruch hat?" Der Einstieg ist bewusst
naiv (eigene Kerzenrichtung, reine Momentum-Fortsetzung) — der Edge steckt
ausschließlich im **Risiko-/Exit-Gen**: einer Kombination aus struktureller
SL-Fensterlänge, Risk/Reward-Ratio und Trailing-Stop-Callback.

Ein "Gen" ist hier also keine Kerzensequenz mehr, sondern eine Parameter-
Kombination:

```
seq_len=5 | rr_ratio=1.5 | trailing_pct=0.5% | risk_pct=1.0%
   ↓
Dieses Gen erzielte im In-Sample-Backtest einen Calmar (PnL/MaxDD) von 22.35.
Auf einem nie zuvor gesehenen Out-of-Sample-Fenster (26 Wochen) bestätigte
es sich mit Calmar 0.03 (schwach, aber positiv) → bleibt aktiv.
```

Die Architektur ist bewusst dieselbe wie beim ursprünglichen Kerzen-Genome-
System (Kandidaten-Datenbank, Evolver, der das beste Gen aktiviert,
Self-Learning aus echten Live-Ergebnissen) — nur die **Definition** dessen,
was ein Gen ist, hat sich geändert: von einem Kerzenmuster zu einer
Risiko-/Exit-Parameter-Kombination. Der Bot handelt jede Kerze, aber nur mit
den Parametern, die sich für dieses Pair/Timeframe echt bewährt haben.

---

## Architektur

```
dnabot/
├── risk_genome_discover.py        # Discovery: Kandidaten-Gene erzeugen, IS/OOS-getrennt
│                                     bewerten, bestes Gen aktivieren (echte simulate_trade())
├── backtest_momentum_exit.py      # Backtest ueber die ECHTE Live-Signalfunktion gegen
│                                     frische Bitget-Daten (kein Nachbau)
├── run_momentum_exit_pipeline.sh  # Liest active_strategies, backtestet jedes Pair + Fee-Report
├── master_runner.py               # Cronjob-Orchestrator fuer Live-Trading
├── auto_optimizer_scheduler.py    # Automatischer Wochentimer: stoesst risk_genome_discover.py an
├── install.sh                     # Erstinstallation auf VPS
├── update.sh                      # Git-Update (sichert secret.json UND settings.json --
│                                     Live-Config wird NICHT durch Git ueberschrieben, siehe unten)
├── run_tests.sh                   # Pytest-Sicherheitscheck
├── settings.json                  # Konfiguration
├── secret.json                    # API-Keys (nicht in Git)
│
└── src/dnabot/
    ├── genome/
    │   ├── risk_genome_db.py      # SQLite-DB fuer Risiko-/Exit-Gene
    │   │                            (artifacts/db/risk_genome.db)
    │   └── risk_evolver.py        # aktiviert das Gen mit dem hoechsten Calmar-Ratio
    │
    ├── strategy/
    │   ├── momentum_exit_logic.py # Momentum-Einstieg (kein Vorhersage-Anspruch),
    │   │                            liest live das aktive Risiko-Gen aus risk_genome_db
    │   └── run.py                 # Entry Point fuer eine Pair/Timeframe-Strategie
    │
    ├── analysis/
    │   └── backtester.py          # simulate_trade() -- identisch in Live UND Backtest
    │                                (siehe feedback_live_backtest_must_match)
    │
    └── utils/
        ├── exchange.py            # Bitget CCXT Wrapper
        ├── trade_manager.py       # Entry/TP/SL + Self-Learning (RiskGenomeDB)
        ├── strategy_overrides.py  # Loest risk_overrides/momentum_exit_overrides auf
        ├── config_loader.py       # Geteiltes settings.json/secret.json-Laden + HISTORY_DAYS_MAP
        ├── telegram.py            # Telegram-Benachrichtigungen
        └── guardian.py            # Crash-Schutz Decorator

analysis/
├── show_risk_genes.py             # Report: aktive + Kandidaten-Gene pro Pair
└── fee_impact_momentum_exit.py    # Isolierte Gebuehren-/Slippage-Impact-Analyse
```

---

## Mechanik

```
Einstieg (KEIN Vorhersage-Anspruch):
  Richtung = eigene Kerzenrichtung der letzten Kerze (Momentum-Fortsetzung)
  KEIN Score-Gate, KEIN Regime-Filter -- jede Kerze wird potenziell gehandelt

Exit (HIER steckt der Edge -- Parameter kommen aus dem AKTIVEN Risiko-Gen,
nicht aus einer festen Konfiguration):
  SL = Low/High der letzten `seq_len` Kerzen (strukturell)
  Trailing-Aktivierung = Entry + rr_ratio × SL-Distanz
  Trailing Stop = trailing_pct nachgezogen, nativ ueber Bitget place_trailing_stop_order
```

Ergebnis-Profil (R-Multiple-Diagnose, siehe Fund AQ in
`research_dnabot_direction_calibration.md`): **viele kleine Verluste, aber
ein Schwanz seltener großer Gewinner** — ein eng nachgezogener Trailing-Stop
lässt eine Position weiterlaufen, solange der Trend hält, und gibt beim
Umkehren nur wenig zurück. Klassisches Trendfolge-Payoff-Profil.

### Risiko-Gen-Datenbank (`risk_genome.db`)

```
src/dnabot/genome/risk_genome_db.py   — SQLite-DB (artifacts/db/risk_genome.db)
                                          Tabellen: risk_genes, risk_gene_occurrences
src/dnabot/genome/risk_evolver.py     — aktiviert das Gen mit dem hoechsten
                                          Calmar (PnL/MaxDD) pro (Pair, Timeframe) --
                                          Selektionskriterium ist Calmar statt
                                          Winrate (bei duennem Edge entscheidet die
                                          Positionsgroesse/der Drawdown ueber
                                          Profitabilitaet, nicht die Trefferquote)
risk_genome_discover.py               — erzeugt Kandidaten-Gene aus einem festen
                                          Parameter-Raster (seq_len x rr_ratio x
                                          trailing_pct x risk_pct), backtestet
                                          jede Kombination mit der ECHTEN
                                          simulate_trade()-Funktion, waehlt via
                                          Evolver das beste Gen NUR auf dem
                                          IS-Anteil, prueft es DANACH einmalig
                                          auf dem echten, nie gesehenen 26-
                                          Wochen-OOS-Anteil -- nur bei
                                          positivem OOS-Calmar bleibt es aktiv
                                          (sonst deaktiviert es sich selbst)
```

`get_momentum_exit_signal(df, params, db)` liest live das **aktive** Gen fuer
das jeweilige Pair/Timeframe aus der DB und nutzt dessen `seq_len`/`rr_ratio`/
`trailing_pct`/`risk_pct` fuer JEDEN Trade. Nach jedem geschlossenen Trade
schreibt `trade_manager.py::self_learn_from_closed_trade()` das Ergebnis
zurueck in `risk_gene_occurrences` (`source='live'`) -- das aktive Gen lernt
laufend aus echten Ergebnissen.

**Kein aktives Gen fuer ein Pair/Timeframe → kein Signal, kein Trade**
(konservativ: kein blindes Handeln mit Default-Werten ohne validierte
Discovery).

### Discovery-Ergebnisse (2026-08-24, 7 Paare bei 6h, echte Bitget-Daten)

```bash
.venv/bin/python3 risk_genome_discover.py --symbol BTC/USDT:USDT --timeframe 6h
# oder ohne Argumente: alle momentum_exit-Paare aus active_strategies
.venv/bin/python3 risk_genome_discover.py
```

| Pair | Aktiviertes Gen | IS-Calmar | OOS n | OOS-Calmar | Status |
|---|---|---|---|---|---|
| BTC | seq5/rr3.0/trail0.5%/risk2.0% | 11.58 | 56 | 1.09 | ✅ Aktiv |
| XRP | seq5/rr1.5/trail0.5%/risk2.0% | 22.35 | 67 | 0.03 | ⚠️ Aktiv (sehr knapp) |
| ETH | seq5/rr1.5/trail0.5%/risk2.0% | 8.85 | 60 | 0.36 | ✅ Aktiv |
| SOL | seq5/rr3.0/trail0.5%/risk2.0% | 11.75 | 61 | **-0.26** | ❌ **Selbst deaktiviert** |
| ADA | seq5/rr1.5/trail0.5%/risk2.0% | 25.71 | 72 | 1.39 | ✅ Aktiv |
| AAVE | seq10/rr2.0/trail0.5%/risk2.0% | 12.17 | 56 | 1.91 | ✅ Aktiv |
| DOGE | seq10/rr3.0/trail0.5%/risk2.0% | 12.71 | 55 | 1.42 | ✅ Aktiv |

SOL wurde vom Evolver **automatisch deaktiviert**, weil das IS-beste Gen im
echten OOS-Test negativ war — deckt sich mit der urspruenglichen Recherche
(Fund AQ zeigte SOL dort ebenfalls als einziges klar negatives Paar). Das ist
ein starkes Konsistenz-Signal, dass der OOS-Gate-Mechanismus echt greift statt
nur simuliert gut auszusehen.

> **Offener Vorbehalt:** Alle 7 Paare wählten unabhängig voneinander das
> höchste getestete Risiko (`risk_pct=2.0%`) — dasselbe Muster, das beim
> ursprünglichen GA-Test bei 1h zur Explosion führte (Fund AN/AQ). Der OOS-
> Check fängt grobe Fehlschläge ab (siehe SOL), aber XRPs Bestätigung
> (Calmar 0.03) ist statistisch praktisch bedeutungslos. Vor einer laengeren
> Live-Phase lohnt es, die Risiko-Obergrenze in `RISK_CHOICES`
> (`risk_genome_discover.py`) probeweise niedriger anzusetzen und/oder die
> OOS-Schwelle in `evolve_risk_genes()`/`discover_pair()` strenger als
> "> 0" zu setzen.

**Nur 6h ist bisher discovered.** Fuer andere Timeframes: erst
`risk_genome_discover.py --symbol ... --timeframe ...` laufen lassen, bevor
`strategy_type: "momentum_exit"` dafuer aktiviert wird -- sonst bleibt das
Pair ohne aktives Gen und handelt schlicht nicht (sicherer Default).

---

## Konfiguration (`settings.json`)

```json
{
    "live_trading_settings": {
        "active_strategies": [
            { "symbol": "BTC/USDT:USDT", "timeframe": "6h", "strategy_type": "momentum_exit",
              "risk_overrides": { "rr_ratio": 1.5, "risk_per_entry_pct": 1.0, "trailing_callback_rate_pct": 0.5 },
              "momentum_exit_overrides": { "enabled": true, "seq_len": 5 },
              "active": true }
        ]
    },
    "momentum_exit_settings": {
        "enabled": false,
        "seq_len": 5
    },
    "risk_settings": {
        "risk_per_entry_pct": 1.0,
        "leverage": 5,
        "margin_mode": "isolated",
        "rr_ratio": 2.0,
        "trailing_callback_rate_pct": 1.0
    },
    "optimization_settings": {
        "schedule": {
            "day_of_week": 6,
            "hour": 3,
            "minute": 0,
            "interval": { "value": 7, "unit": "days" }
        },
        "backtest_lookback_weeks": 26,
        "send_telegram_on_completion": true
    }
}
```

| Parameter | Erklärung |
|---|---|
| `active_strategies[].active` | Muss `true` sein, sonst überspringt `master_runner.py` das Pair stillschweigend. |
| `active_strategies[].risk_overrides` | Fallback-Werte, falls (noch) kein aktives Risiko-Gen in der DB existiert. Im Live-Betrieb überschreibt das aktive Gen diese Werte pro Trade. |
| `active_strategies[].momentum_exit_overrides.enabled` | Pro-Strategie-Schalter, überschreibt den globalen `momentum_exit_settings.enabled`-Fallback. |
| `risk_per_entry_pct` | % des Guthabens als Risiko pro Trade (Fallback, siehe oben). |
| `rr_ratio` | Risk-Reward-Ratio — bestimmt Aktivierungspreis des Trailing Stops (Fallback). |
| `trailing_callback_rate_pct` | Trailing Stop Callback in % (Fallback). |
| `optimization_settings.schedule` | Wochentag + Uhrzeit + Intervall für den Auto-Optimizer (`risk_genome_discover.py`). |
| `optimization_settings.backtest_lookback_weeks` | Dokumentiert die OOS-Fensterkonvention (26W) — `risk_genome_discover.py::OOS_WEEKS` ist der tatsächliche, hartkodierte Wert. |

> `strategy_overrides.py` löst `risk_overrides`/`momentum_exit_overrides` pro
> (Symbol, Timeframe) auf — identisch für Live (`run.py`) und Backtest
> (`backtest_momentum_exit.py`), siehe `feedback_live_backtest_must_match`.

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

Neuen Eintrag in `active_strategies` anlegen (siehe Konfigurationsbeispiel
oben) — zunaechst mit `"active": false`, bis die Discovery gelaufen ist.

#### 2. Risiko-Gen-Discovery starten

```bash
.venv/bin/python3 risk_genome_discover.py
# oder gezielt fuer ein neues Pair/Timeframe:
.venv/bin/python3 risk_genome_discover.py --symbol BTC/USDT:USDT --timeframe 6h
```

Laedt historische Daten, testet das Parameter-Raster, waehlt per Calmar das
beste Gen im In-Sample-Zeitraum und bestaetigt es einmalig Out-of-Sample.
Nur ein Pair mit einem aktiven Gen wird spaeter tatsaechlich gehandelt.

#### 3. Ergebnisse prüfen

```bash
.venv/bin/python3 analysis/show_risk_genes.py
# oder ueber den Wrapper:
./show_results.sh
```

Zeigt pro Pair das aktive Gen (falls vorhanden) und die Top-Kandidaten.

#### 4. Backtest & Gebühren-Check

```bash
./run_momentum_exit_pipeline.sh
```

Backtestet jedes konfigurierte `momentum_exit`-Pair über die echte
Live-Signalfunktion gegen frische Bitget-Daten und hängt eine isolierte
Gebühren-/Slippage-Impact-Analyse an.

#### 5. Strategie live schalten

```bash
nano settings.json
```

```json
{ "symbol": "BTC/USDT:USDT", "timeframe": "6h", "active": true }
```

#### 6. Cronjob einrichten

```bash
crontab -e
```

```cron
*/15 * * * * /usr/bin/flock -n /home/matola/dnabot/dnabot.lock /bin/sh -c "cd /home/matola/dnabot && /home/matola/dnabot/.venv/bin/python3 master_runner.py >> /home/matola/dnabot/logs/cron.log 2>&1"
```

> `master_runner.py` ruft beim Start automatisch `auto_optimizer_scheduler.py`
> auf. Dieser prüft ob eine Risiko-Gen-Discovery fällig ist und führt sie
> dann automatisch aus. Ein separater Cronjob dafür ist **nicht nötig**.

---

## Automatische Wochentimer-Optimierung

```
master_runner.py startet
    ↓
auto_optimizer_scheduler.py prüft: Ist eine Discovery fällig?
    ├── Nein → sofort beendet (kein Overhead)
    └── Ja →
           risk_genome_discover.py     (Risiko-Gene fuer alle momentum_exit-
                                         Paare aus active_strategies neu
                                         bewerten, IS/OOS-gated aktivieren)
               ↓
           Telegram: Start + Ende Benachrichtigung
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

# Auto-Optimizer
tail -f logs/auto_optimizer_trigger.log

# Einzelnes Symbol
tail -n 100 logs/dnabot_BTCUSDTUSDT_6h.log
```

#### Manueller Start (Test)

```bash
cd ~/dnabot && .venv/bin/python3 master_runner.py
```

#### Auto-Optimizer: Status & manueller Start

```bash
# Letzter Discovery-Zeitpunkt
cat ~/dnabot/artifacts/cache/.last_optimization_run

# Optimizer-Log (läuft er? überspringt er? Fehler?)
tail -f ~/dnabot/logs/auto_optimizer_trigger.log

# Discovery sofort erzwingen (ignoriert den Zeitplan)
cd ~/dnabot && .venv/bin/python3 auto_optimizer_scheduler.py --force
```

> **Intervall:** Standardmäßig alle 7 Tage (konfigurierbar in
> `optimization_settings.schedule`).

#### Risiko-Gene aktualisieren, Backtest & Gebühren-Check

```bash
# Risiko-Gen-Discovery (neu/aktualisieren) -- laeuft automatisch mit dem
# Scheduler (siehe oben), hier fuer manuelles Anstossen/Testen
.venv/bin/python3 risk_genome_discover.py                                        # alle momentum_exit-Paare
.venv/bin/python3 risk_genome_discover.py --symbol ETH/USDT:USDT --timeframe 4h  # neuer Timeframe/Pair

# Report: welches Gen ist aktiv, wie sehen die Top-Kandidaten aus
.venv/bin/python3 analysis/show_risk_genes.py       # oder: ./show_results.sh

# Alle konfigurierten momentum_exit-Strategien backtesten + Fee-Report
./run_momentum_exit_pipeline.sh

# Isolierte Gebühren-Analyse
.venv/bin/python3 analysis/fee_impact_momentum_exit.py --capital 1000 --risk 1.0

# Einzelnes Pair, eigene (noch nicht discovered) Parameter isoliert testen
.venv/bin/python3 backtest_momentum_exit.py --symbol BTC/USDT:USDT --timeframe 6h \
    --capital 1000 --risk 1.0 --rr-ratio 1.5 --trailing-callback-pct 0.5 --seq-len 5 --oos-weeks 26
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

Sichert automatisch `secret.json` und `settings.json` vor dem `git reset --hard`.

#### Risiko-Gen-Datenbank zurücksetzen

```bash
# Achtung: löscht alle erlernten Risiko-Gene!
rm -f artifacts/db/risk_genome.db*
.venv/bin/python3 risk_genome_discover.py
```

#### Aktives Portfolio löschen

```bash
python3 -c "
import json
s = json.load(open('settings.json', encoding='utf-8'))
s['live_trading_settings']['active_strategies'] = []
json.dump(s, open('settings.json', 'w', encoding='utf-8'), indent=2, ensure_ascii=False)
"
```

---

## Wichtige Regeln

- `secret.json` ist **nicht in Git** — wird von `update.sh` gesichert
- `settings.json` ist **in Git getrackt, wird aber von `update.sh` NICHT überschrieben**
  — vor `git reset --hard` gesichert und danach wiederhergestellt (siehe `update.sh`).
  Das heißt: eine `active_strategies`-Änderung im Repo (z.B. neue momentum_exit-Paare)
  erreicht den VPS **nicht automatisch** über `./update.sh` — muss dort manuell in
  die lokale `settings.json` übernommen werden.
- `artifacts/db/risk_genome.db` ist **nicht in Git** — bleibt nach Updates erhalten
  und enthält das gesamte gelernte Wissen (aktive Gene + Trade-Historie)
- `artifacts/tracker/` ist **nicht in Git** — enthält den offenen Trade-Status pro Symbol
- Immer erst `risk_genome_discover.py` + `run_momentum_exit_pipeline.sh` laufen
  lassen, bevor ein Pair live geschaltet wird
- Ohne aktives Risiko-Gen für ein Pair/Timeframe wird schlicht nicht gehandelt
  (kein blindes Handeln mit ungetesteten Default-Werten)

---

## Coin & Timeframe Empfehlungen

**Nur 6h ist bisher validiert** (BTC, XRP, ETH, SOL, ADA, AAVE, DOGE — siehe
Discovery-Ergebnisse oben). Für einen neuen Coin oder Timeframe:

1. `risk_genome_discover.py --symbol ... --timeframe ...` laufen lassen
2. `analysis/show_risk_genes.py` prüfen — gibt es ein aktives Gen, und wie
   sieht dessen OOS-Bestätigung aus (Anzahl Trades, Calmar)?
3. Erst danach `strategy_type: "momentum_exit"` mit `"active": true` in
   `active_strategies` eintragen

Frühere Versionen dieses READMEs enthielten hier Tabellen mit qualitativen
Coin-/Timeframe-Einschätzungen ("BTC: exzellente institutionelle Muster",
"4h deckt einen vollen Handelstag ab") — diese waren **nie durch einen
Backtest oder eine Signifikanzprüfung belegt**, sondern Plausibilitäts­
annahmen aus der Zeit des ursprünglichen Kerzen-Genome-Systems. Sie wurden
entfernt statt mit unbelegten Behauptungen stehen gelassen. Was zählt, sind
die tatsächlichen IS/OOS-Zahlen aus `risk_genome_discover.py` pro Pair.

---

## Abhängigkeiten

```
ccxt==4.3.5      # Exchange-Verbindung (Bitget)
pandas==2.1.3    # Datenverarbeitung
numpy            # Array-Operationen
requests==2.31.0 # Telegram
matplotlib       # Entry-Charts + Fee-Impact-Charts
sqlite3          # Built-in Python — keine Installation nötig
```

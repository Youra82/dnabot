#!/usr/bin/env python3
"""Zeigt, welches Portfolio (Team) wann live aktiv war.

  python3 show_team_history.py                          # alle Teamwechsel
  python3 show_team_history.py --at "2026-10-18 20:00"  # Team zu diesem Zeitpunkt (UTC)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from dnabot.utils.team_history import HISTORY_FILE, load_history, team_at


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--at', help='Zeitpunkt in UTC, z. B. "2026-10-18 20:00"')
    args = ap.parse_args()

    history = load_history()
    if not history:
        print(f"Noch keine Teamhistorie ({HISTORY_FILE}). Sie entsteht beim naechsten master_runner-Lauf.")
        return 1
    if args.at:
        team = team_at(args.at)
        if team is None:
            print(f"{args.at} UTC liegt vor der ersten Aufzeichnung ({history[0]['valid_from']}).")
            return 1
        print(f"Team am {args.at} UTC:")
        for key, cfg_hash in team.items():
            print(f"  {key:<22} Config {cfg_hash}")
        return 0
    for i, entry in enumerate(history):
        until = history[i + 1]['valid_from'] if i + 1 < len(history) else 'jetzt'
        print(f"{entry['valid_from']}  bis  {until}  ({entry['source']})")
        print("  " + ", ".join(entry['team']))
    return 0


if __name__ == '__main__':
    sys.exit(main())

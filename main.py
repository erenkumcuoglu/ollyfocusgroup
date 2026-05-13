"""
main.py — CLI giriş noktası
Ad-hoc ve scheduled çalıştırma

Kullanım:
  python main.py                              # tüm personalar, tek seferlik
  python main.py --persona emre              # tek persona
  python main.py --group university_male     # grup bazlı
  python main.py --schedule monday 09:00     # her pazartesi
  python main.py --schedule daily 08:00      # her gün
  python main.py --list-personas             # mevcut personaları listele
"""

import sys
import asyncio
import schedule
import time

from rich.console import Console
from rich.table import Table

import config
from personas import PERSONAS
from runner import run_and_report

console = Console()


# ────────────────────────────────────────────────────────────────────────────
# BAŞLANGIÇ KONTROLLERI
# ────────────────────────────────────────────────────────────────────────────

def startup_checks():
    warnings = config.validate()
    if warnings:
        console.print("\n[bold yellow]⚠️  Yapılandırma uyarıları:[/bold yellow]")
        for w in warnings:
            console.print(f"  • {w}")
        console.print()


# ────────────────────────────────────────────────────────────────────────────
# PERSONA LİSTESİ
# ────────────────────────────────────────────────────────────────────────────

def list_personas():
    tbl = Table(title="Mevcut Personalar", show_lines=True)
    tbl.add_column("ID",           style="cyan",  min_width=10)
    tbl.add_column("İsim",         style="white", min_width=30)
    tbl.add_column("Grup",         style="dim",   min_width=20)
    tbl.add_column("Dil",          justify="center")
    tbl.add_column("Max Adım",     justify="right")

    for pid, p in PERSONAS.items():
        tbl.add_row(
            pid,
            p["display_name"],
            p["group"],
            p["language"].upper(),
            str(p.get("max_steps", "—")),
        )
    console.print(tbl)

    # Gruplar
    groups = sorted(set(p["group"] for p in PERSONAS.values()))
    console.print(f"\n[dim]Mevcut gruplar: {', '.join(groups)}[/dim]")


# ────────────────────────────────────────────────────────────────────────────
# SCHEDULED JOB
# ────────────────────────────────────────────────────────────────────────────

def scheduled_job():
    console.print("\n[bold]⏰ Scheduled focus group başlatılıyor...[/bold]")
    asyncio.run(run_and_report())


# ────────────────────────────────────────────────────────────────────────────
# CLI PARSER — argparse yerine sade sys.argv (bağımlılık azaltmak için)
# ────────────────────────────────────────────────────────────────────────────

def parse_args(args: list) -> dict:
    """
    Döner:
      { "mode": "adhoc",     "persona_ids": [...] }
      { "mode": "schedule",  "day": "monday", "time": "09:00" }
      { "mode": "list" }
      { "mode": "help" }
    """
    if not args:
        return {"mode": "adhoc", "persona_ids": None}

    cmd = args[0].lstrip("-")

    if cmd in ("list-personas", "list"):
        return {"mode": "list"}

    if cmd in ("help", "h"):
        return {"mode": "help"}

    if cmd == "all":
        return {"mode": "adhoc", "persona_ids": None}

    if cmd == "persona":
        if len(args) < 2:
            console.print("[red]--persona için bir ID gerekli (örn: --persona emre)[/red]")
            sys.exit(1)
        pid = args[1]
        if pid not in PERSONAS:
            console.print(f"[red]Persona bulunamadı: '{pid}'. Mevcut personalar için --list-personas[/red]")
            sys.exit(1)
        return {"mode": "adhoc", "persona_ids": [pid]}

    if cmd == "group":
        if len(args) < 2:
            console.print("[red]--group için bir grup adı gerekli[/red]")
            sys.exit(1)
        grp = args[1]
        ids = [p["id"] for p in PERSONAS.values() if p["group"] == grp]
        if not ids:
            groups = sorted(set(p["group"] for p in PERSONAS.values()))
            console.print(f"[red]Grup bulunamadı: '{grp}'. Mevcut gruplar: {', '.join(groups)}[/red]")
            sys.exit(1)
        return {"mode": "adhoc", "persona_ids": ids}

    if cmd == "schedule":
        day      = args[1] if len(args) > 1 else "monday"
        time_str = args[2] if len(args) > 2 else "09:00"
        return {"mode": "schedule", "day": day, "time": time_str}

    console.print(f"[red]Bilinmeyen komut: {args[0]}[/red]")
    return {"mode": "help"}


def print_help():
    console.print("""
[bold]Olly Focus Group Test Runner[/bold]

[cyan]python main.py[/cyan]                              Tüm personalar, tek seferlik
[cyan]python main.py --all[/cyan]                        Aynı
[cyan]python main.py --persona emre[/cyan]               Tek persona
[cyan]python main.py --group university_male[/cyan]      Grup bazlı
[cyan]python main.py --list-personas[/cyan]              Mevcut personaları listele
[cyan]python main.py --schedule monday 09:00[/cyan]      Her pazartesi 09:00
[cyan]python main.py --schedule daily 08:00[/cyan]       Her gün 08:00

[dim]Mevcut gruplar:[/dim]
  university_male · university_female
  midcareer_male  · midcareer_female
  executive_male  · executive_female
  edge_reserved   · edge_oversharer
  english_male    · english_female

[dim]Ayarlar .env dosyasından okunur — bkz. README.md[/dim]
""")


# ────────────────────────────────────────────────────────────────────────────
# ANA FONKSİYON
# ────────────────────────────────────────────────────────────────────────────

def main():
    startup_checks()
    parsed = parse_args(sys.argv[1:])

    if parsed["mode"] == "help":
        print_help()
        return

    if parsed["mode"] == "list":
        list_personas()
        return

    if parsed["mode"] == "adhoc":
        asyncio.run(run_and_report(parsed.get("persona_ids")))
        return

    if parsed["mode"] == "schedule":
        day      = parsed["day"]
        time_str = parsed["time"]

        valid_days = {
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday", "daily",
        }
        if day not in valid_days:
            console.print(f"[red]Geçersiz gün: {day}. Kullanılabilecekler: {', '.join(sorted(valid_days))}[/red]")
            sys.exit(1)

        if day == "daily":
            schedule.every().day.at(time_str).do(scheduled_job)
            console.print(f"[bold green]⏰ Scheduler aktif: her gün {time_str}[/bold green]")
        else:
            getattr(schedule.every(), day).at(time_str).do(scheduled_job)
            console.print(f"[bold green]⏰ Scheduler aktif: her {day} {time_str}[/bold green]")

        console.print("[dim]Durdurmak için Ctrl+C[/dim]\n")
        try:
            while True:
                schedule.run_pending()
                time.sleep(30)
        except KeyboardInterrupt:
            console.print("\n[dim]Scheduler durduruldu.[/dim]")
        return


if __name__ == "__main__":
    main()

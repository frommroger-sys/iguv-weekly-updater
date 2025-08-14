# IGUV / INPASU Weekly Updater

Zweck: Wöchentliche, KI-gestützte Zusammenfassung (letzte X Tage) aus definierten Quellen, gefiltert nach Keywords. HTML wird in einen definierten Container auf einer WordPress-Seite geschrieben.

## Dateien
- `updater.py` — Hauptskript (KI + Rendering + WP-Update)
- `data_sources.yaml` — Quellen, Zeitfenster, Keywords, Stil
- `html_template.html` — HTML-Template mit Container-ID
- `requirements.txt` — Python-Pakete
- `.github/workflows/update.yml` — GitHub Action (manueller Start, Cron später)

## Secrets (werden in Schritt 2 gesetzt)
- `OPENAI_API_KEY`
- `WP_BASE` (z. B. `https://iguv.ch`)
- `WP_PAGE_ID` (z. B. `50489`)
- `WP_USERNAME` (Application Password User, z. B. `agent@iguv.ch`)
- `WP_APP_PASSWORD` (Application Password, inkl. Leerzeichen)
- `WP_CONTAINER_ID` (z. B. `weekly-update-content`)

## Nutzung
1) Secrets setzen
2) Action manuell via `Run workflow` starten
3) Logs prüfen
4) Cron aktivieren (Do 06:00 Europe/Zurich)

# Flugwetter Böhming / Oberemmendorf

Tägliche Auswertung frei verfügbarer Wettermodelle für die Gleitschirm-Startplätze Böhming (Start Ost) und Oberemmendorf (Start Nordwest) im Altmühltal: Wind, Startfenster, Thermik und eine Ampel je Zeitstufe, als interaktive Seite.

**Private Modellauswertung ohne Gewähr. Keine Flugwetterberatung und keine Startfreigabe.** Die Grenzwerte sind nicht am Gelände geeicht. Jeder Pilot entscheidet selbst und beachtet die Regeln des Geländehalters.

## Aufbau

- `flugwetter.py` – Abruf und Auswertung, nur Python-Standardbibliothek
- `config.json` – Startplätze, Zeitstufen, Grenzwerte
- `vorlage.html` – Vorlage der Seite; das Skript setzt die Auswertung ein
- `.github/workflows/flugwetter.yml` – täglicher Lauf um 8:00 und 9:00 Uhr Ortszeit, veröffentlicht die Seite über GitHub Pages

## Datenquellen

- Wettermodelle ICON-D2, ICON-EU, GFS und IFS über [Open-Meteo](https://open-meteo.com/) (CC BY 4.0)
- Segelflug-Gebietsvorhersage Fränkische Alb: Deutscher Wetterdienst

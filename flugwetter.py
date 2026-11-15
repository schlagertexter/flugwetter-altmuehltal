#!/usr/bin/env python3
"""Flugwetter für Gleitschirm-Startplätze (Böhming, Oberemmendorf).

Wertet frei verfügbare Vorhersagemodelle über die Open-Meteo-API aus und
schreibt je Startplatz und Zeitstufe Wind, Startfenster, Thermik und Ampel:
als Markdown-Datei je Tag und als interaktive HTML-Seite.

Aufruf:
    python flugwetter.py                 heute und morgen
    python flugwetter.py --oeffnen       HTML-Seite danach im Browser öffnen
    python flugwetter.py --stunden       Markdown zusätzlich mit Stundenwerten
    python flugwetter.py --modelle       Markdown zusätzlich mit Modellvergleich
    python flugwetter.py --datum 2026-07-04   Rückblick auf einen vergangenen Tag
    python flugwetter.py --ohne-datei    nur Ausgabe im Terminal
    python flugwetter.py --ohne-abgleich ohne Abruf der Holfuy-Messwerte und ohne Rückschau

Jeder Lauf mit Dateien legt die Auswertung zusätzlich unter vorhersagen/laeufe ab.
Die Rückschau vergleicht den ersten Lauf eines vergangenen Tages mit den
Holfuy-Messwerten und führt messwerte/abgleich.csv fort.

Nur Standardbibliothek, keine Zusatzpakete.
"""
import argparse
import html as html_modul
import json
import math
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

ORDNER = Path(__file__).resolve().parent
G = 9.81          # m/s²
CP = 1005.0       # J/(kg K)
R_L = 287.05      # J/(kg K)
GAMMA = 0.0098    # K/m, trockenadiabatischer Gradient

KOMPASS = ["N", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
AMPEL = {0: "🟢 grün", 1: "🟡 gelb", 2: "🔴 rot"}
AMPELWORT = ["grün", "gelb", "rot"]
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


# ---------------------------------------------------------------- Hilfen

def kompass(grad):
    return KOMPASS[int(((grad + 11.25) % 360) // 22.5)]


def winkel_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def vektor_mittel(paare):
    """Mittelt (Stärke, Richtung)-Paare vektoriell. Liefert (Stärke, Richtung)."""
    paare = [(s, d) for s, d in paare if s is not None and d is not None]
    if not paare:
        return None, None
    u = sum(-s * math.sin(math.radians(d)) for s, d in paare) / len(paare)
    v = sum(-s * math.cos(math.radians(d)) for s, d in paare) / len(paare)
    richtung = math.degrees(math.atan2(-u, -v)) % 360
    staerke = sum(s for s, _ in paare) / len(paare)
    return staerke, richtung


def mittel(werte):
    werte = [w for w in werte if w is not None]
    return sum(werte) / len(werte) if werte else None


def maximum(werte):
    werte = [w for w in werte if w is not None]
    return max(werte) if werte else None


def zahl(x, stellen=0, leer="–"):
    if x is None:
        return leer
    return f"{x:.{stellen}f}".replace(".", ",")


def wind_text(staerke, richtung):
    if staerke is None or richtung is None:
        return "–"
    return f"{kompass(richtung)} {richtung:.0f}° · {staerke:.0f} km/h"


def gerundet(objekt):
    """Rundet alle Gleitkommazahlen für die Einbettung in die HTML-Seite."""
    if isinstance(objekt, float):
        return round(objekt, 2)
    if isinstance(objekt, dict):
        return {k: gerundet(v) for k, v in objekt.items()}
    if isinstance(objekt, (list, tuple)):
        return [gerundet(v) for v in objekt]
    return objekt


# ---------------------------------------------------------------- Abruf

def abruf(host, parameter):
    url = f"https://{host}/v1/forecast?" + urllib.parse.urlencode(parameter, safe=",")
    letzter_fehler = None
    for versuch in range(3):
        try:
            anfrage = urllib.request.Request(url, headers={"User-Agent": "flugwetter-skript/1.0"})
            with urllib.request.urlopen(anfrage, timeout=40) as antwort:
                return json.load(antwort)
        except urllib.error.HTTPError as fehler:
            text = fehler.read().decode("utf-8", "replace")
            if fehler.code == 400:
                raise RuntimeError(f"Open-Meteo lehnt die Anfrage ab: {text}") from None
            letzter_fehler = f"HTTP {fehler.code}: {text[:200]}"
        except (urllib.error.URLError, TimeoutError) as fehler:
            letzter_fehler = str(fehler)
        time.sleep(2 + 3 * versuch)
    raise RuntimeError(f"Abruf fehlgeschlagen ({letzter_fehler})")


def felder_fuer(modell, haupt):
    felder = ["temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
              "wind_speed_80m", "wind_direction_80m"]
    if haupt:
        felder += [
            "dew_point_2m", "surface_pressure",
            "wind_speed_180m", "wind_direction_180m",
            "sensible_heat_flux", "updraft", "cape", "shortwave_radiation",
            "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
            "precipitation", "weather_code", "visibility",
        ]
    if modell.get("grenzschicht"):
        felder.append("boundary_layer_height")
    for flaeche in modell.get("druckflaechen", []):
        felder += [f"temperature_{flaeche}hPa", f"geopotential_height_{flaeche}hPa"]
        if haupt:
            felder += [f"wind_speed_{flaeche}hPa", f"wind_direction_{flaeche}hPa"]
    return felder


def lade_modell(cfg, ort, modell, haupt, rueckblick):
    parameter = {
        "latitude": ort["lat"],
        "longitude": ort["lon"],
        "elevation": ort["hoehe_m"],
        "hourly": ",".join(felder_fuer(modell, haupt)),
        "models": modell["id"],
        "timezone": cfg["zeitzone"],
        "wind_speed_unit": "kmh",
    }
    if haupt:
        parameter["daily"] = "sunrise,sunset"
    if rueckblick:
        host = "historical-forecast-api.open-meteo.com"
        parameter["start_date"] = parameter["end_date"] = rueckblick.isoformat()
    else:
        host = "api.open-meteo.com"
        parameter["forecast_days"] = cfg["tage"]
    daten = abruf(host, parameter)
    daten["_index"] = {zeit: i for i, zeit in enumerate(daten["hourly"]["time"])}
    return daten


def wert(daten, feld, zeit):
    """Stundenwert eines Modells zur Zeit 'JJJJ-MM-TTThh:mm' oder None."""
    if daten is None:
        return None
    i = daten["_index"].get(zeit)
    reihe = daten["hourly"].get(feld)
    if i is None or reihe is None:
        return None
    return reihe[i]


# ---------------------------------------------------------------- Thermik

def thermik_obergrenze(daten, zeit, z0, flaechen, ueberwaerme):
    """Höhe (m MSL), bis zu der ein trockenadiabatisch aufsteigendes Luftpaket
    vom Startplatz wärmer bleibt als die Umgebung. None ohne Profil."""
    t0 = wert(daten, "temperature_2m", zeit)
    if t0 is None:
        return None
    punkte = []
    for flaeche in flaechen:
        z = wert(daten, f"geopotential_height_{flaeche}hPa", zeit)
        t = wert(daten, f"temperature_{flaeche}hPa", zeit)
        # Druckflächen unter oder knapp über Gelände tragen keine Information
        if z is not None and t is not None and z > z0 + 50:
            punkte.append((z, t))
    if not punkte:
        return None
    punkte.sort()
    z_alt, d_alt = z0, max(ueberwaerme, 0.05)
    for z, t in punkte:
        d = (t0 + ueberwaerme - GAMMA * (z - z0)) - t
        if d <= 0:
            return z_alt + (z - z_alt) * d_alt / (d_alt - d)
        z_alt, d_alt = z, d
    return punkte[-1][0]


def w_stern(waermestrom_auf, tiefe, t_c, druck_hpa):
    """Konvektive Geschwindigkeit w* in m/s."""
    if waermestrom_auf is None or tiefe is None or t_c is None:
        return 0.0
    if waermestrom_auf <= 0 or tiefe <= 0:
        return 0.0
    t_k = t_c + 273.15
    rho = (druck_hpa * 100) / (R_L * t_k) if druck_hpa else 1.15
    return (G / t_k * waermestrom_auf / (rho * CP) * tiefe) ** (1 / 3)


def richtungsklasse(staerke, abweichung, gw):
    if staerke is None or abweichung is None:
        return None
    if staerke < gw["wind_kaum_kmh"]:
        return "kaum"
    if abweichung <= gw["richtung_gruen_grad"]:
        return "fenster"
    if abweichung <= gw["richtung_gelb_grad"]:
        return "schraeg"
    if abweichung <= 90:
        return "quer"
    return "lee"


def startwind(cfg, v10, d10, v80, d80):
    if cfg.get("startwind_quelle") == "80m" and v80 is not None:
        return v80, d80
    return v10, d10


def stunde_auswerten(zeit, haupt, vergleich, ort, cfg, dwd_stunde=None):
    th = cfg["thermik"]
    z0 = ort["hoehe_m"]
    h = haupt["daten"]
    t = wert(h, "temperature_2m", zeit)
    td = wert(h, "dew_point_2m", zeit)

    obergrenzen = {}
    for modell in [haupt] + vergleich:
        if modell["daten"] is None or not modell["druckflaechen"]:
            continue
        hoehe = thermik_obergrenze(modell["daten"], zeit, z0, modell["druckflaechen"], th["ueberwaerme_k"])
        if hoehe is not None:
            obergrenzen[modell["name"]] = hoehe
    # DWD-Arbeitshöhe (Referenzstation, ersatzweise Gebietsmittel) als weitere Schätzung
    if dwd_stunde:
        arbeitshoehe = dwd_stunde.get("arbeitshoehe")
        if arbeitshoehe is None:
            arbeitshoehe = dwd_stunde.get("mittel_arbeitshoehe")
        if arbeitshoehe is not None:
            obergrenzen["DWD"] = arbeitshoehe
    obergrenze = statistics.median(obergrenzen.values()) if obergrenzen else None

    lcl = z0 + th["lcl_m_je_kelvin"] * max(t - td, 0) if t is not None and td is not None else None
    cumulus = obergrenze is not None and lcl is not None and lcl < obergrenze
    nutzbar = None
    if obergrenze is not None:
        nutzbar = min(obergrenze, lcl) if lcl is not None else obergrenze
    tiefe = nutzbar - z0 if nutzbar is not None else None

    waermestrom = wert(h, "sensible_heat_flux", zeit)
    waermestrom_auf = waermestrom * th["waermestrom_vorzeichen"] if waermestrom is not None else None
    ws = w_stern(waermestrom_auf, tiefe, t, wert(h, "surface_pressure", zeit))
    steigen = max(0.0, ws - th["eigensinken_ms"])
    if tiefe is None or tiefe < th["mindesttiefe_m"]:
        steigen = 0.0

    # Wind in der Arbeitsschicht: 180 m über Grund plus Druckflächen bis zur nutzbaren Höhe
    schicht = [(wert(h, "wind_speed_180m", zeit), wert(h, "wind_direction_180m", zeit))]
    if nutzbar is not None:
        for flaeche in haupt["druckflaechen"]:
            z = wert(h, f"geopotential_height_{flaeche}hPa", zeit)
            if z is not None and z0 + 180 < z <= nutzbar:
                schicht.append((wert(h, f"wind_speed_{flaeche}hPa", zeit),
                                wert(h, f"wind_direction_{flaeche}hPa", zeit)))
    hw_v, hw_d = vektor_mittel(schicht)
    hw_max = maximum([s for s, _ in schicht])

    grenzschicht = {}
    for modell in vergleich:
        if modell.get("grenzschicht"):
            hoehe = wert(modell["daten"], "boundary_layer_height", zeit)
            if hoehe is not None:
                grenzschicht[modell["name"]] = hoehe

    v10, d10 = wert(h, "wind_speed_10m", zeit), wert(h, "wind_direction_10m", zeit)
    v80, d80 = wert(h, "wind_speed_80m", zeit), wert(h, "wind_direction_80m", zeit)
    sv, sd = startwind(cfg, v10, d10, v80, d80)
    abweichung = winkel_diff(sd, ort["startrichtung_grad"]) if sd is not None else None

    # Windscherung zwischen Startwind und Arbeitsschicht: Zunahme und größte Drehung
    kaum = cfg["grenzwerte"]["wind_kaum_kmh"]
    scher_v = hw_max - sv if hw_max is not None and sv is not None else None
    scher_d = None
    if sv is not None and sd is not None and sv >= kaum:
        drehungen = [winkel_diff(d, sd) for s, d in schicht if s is not None and d is not None and s >= kaum]
        scher_d = max(drehungen) if drehungen else None
    # Wind auf einer festen Vergleichsfläche oberhalb der Arbeitsschicht (nur Anzeige)
    flaeche = cfg.get("vergleichsflaeche_hpa", 850)
    return {
        "w15_v": wert(h, f"wind_speed_{flaeche}hPa", zeit),
        "w15_d": wert(h, f"wind_direction_{flaeche}hPa", zeit),
        "w15_z": wert(h, f"geopotential_height_{flaeche}hPa", zeit),
        "sicht": wert(h, "visibility", zeit),
        "dwd": dwd_stunde,
        "scher_v": scher_v, "scher_d": scher_d,
        "zeit": zeit, "stunde": int(zeit[11:13]),
        "t": t, "td": td,
        "v10": v10, "d10": d10, "v80": v80, "d80": d80,
        "boe": wert(h, "wind_gusts_10m", zeit),
        "sv": sv, "sd": sd, "abweichung": abweichung,
        "rk": richtungsklasse(sv, abweichung, cfg["grenzwerte"]),
        "hw_v": hw_v, "hw_d": hw_d, "hw_max": hw_max,
        "obergrenzen": obergrenzen, "obergrenze": obergrenze,
        "lcl": lcl, "cumulus": cumulus, "nutzbar": nutzbar, "tiefe": tiefe,
        "waermestrom_auf": waermestrom_auf, "w_stern": ws, "steigen": steigen,
        "grenzschicht": grenzschicht,
        "wolken_tief": wert(h, "cloud_cover_low", zeit),
        "wolken_mittel": wert(h, "cloud_cover_mid", zeit),
        "wolken_hoch": wert(h, "cloud_cover_high", zeit),
        "regen": wert(h, "precipitation", zeit),
        "wcode": wert(h, "weather_code", zeit),
        "cape": wert(h, "cape", zeit),
        "updraft": wert(h, "updraft", zeit),
    }


# ---------------------------------------------------------------- Zeitstufen

def thermik_wort(steigen):
    if steigen is None or steigen < 0.3:
        return "keine"
    if steigen < 1.0:
        return "schwach"
    if steigen < 2.0:
        return "mäßig"
    if steigen < 3.0:
        return "gut"
    return "stark"


DWD_SPALTE = "DWD 10 m"


def modell_urteil(zeiten, haupt, vergleich, ort, cfg, dwd_std=None):
    """Vergleicht den Startwind der Modelle über eine Zeitstufe, auf derselben
    Höhe, die auch die Ampel bewertet (80 m, ersatzweise 10 m); Böen aus 10 m."""
    mv = cfg["modellvergleich"]
    gw = cfg["grenzwerte"]
    zeilen = {}
    for modell in [haupt] + vergleich:
        if modell["daten"] is None:
            continue
        m = modell["daten"]
        v, d = vektor_mittel([startwind(cfg, wert(m, "wind_speed_10m", z), wert(m, "wind_direction_10m", z),
                                        wert(m, "wind_speed_80m", z), wert(m, "wind_direction_80m", z))
                              for z in zeiten])
        boe = maximum([wert(modell["daten"], "wind_gusts_10m", z) for z in zeiten])
        if v is not None:
            zeilen[modell["name"]] = (v, d, boe)
    # DWD-Bodenwind der Referenzstation: zählt bei der Richtung mit, nicht bei der
    # Stärke, weil er in 10 m gilt und die Modelle in 80 m verglichen werden.
    if dwd_std:
        v, d = vektor_mittel([(s.get("v10"), s.get("d10")) for s in dwd_std])
        if v is not None:
            zeilen[DWD_SPALTE] = (v, d, maximum([s.get("boe") for s in dwd_std]))
    if len(zeilen) < 2:
        return "kein Vergleich", zeilen
    staerken = [v for name, (v, _, _) in zeilen.items() if name != DWD_SPALTE]
    spanne_v = max(staerken) - min(staerken)
    richtungen = [d for v, d, _ in zeilen.values() if v >= gw["wind_kaum_kmh"]]
    spanne_d = max((winkel_diff(a, b) for a in richtungen for b in richtungen), default=0)
    fenster = {winkel_diff(d, ort["startrichtung_grad"]) <= gw["richtung_gelb_grad"] for d in richtungen}
    gruende = []
    if len(fenster) > 1:
        gruende.append("Startfenster strittig")
    if spanne_d > mv["richtung_streuung_grad"]:
        gruende.append(f"Richtung streut {spanne_d:.0f}°")
    if spanne_v > mv["staerke_streuung_kmh"]:
        gruende.append(f"Stärke streut {spanne_v:.0f} km/h")
    return ("unsicher: " + ", ".join(gruende)) if gruende else "einig", zeilen


def wind_regeln(sv, sd, boe, boe_q, ort, gw, melde):
    """Ampelregeln für Startwind und Böen, für Modellwerte wie für Messwerte.
    Liefert die Abweichung von der Startrichtung."""
    abweichung = None
    if sv is None or sd is None:
        melde(1, "keine Winddaten")
    else:
        abweichung = winkel_diff(sd, ort["startrichtung_grad"])
        if sv < gw["wind_kaum_kmh"]:
            melde(1, "kaum Wind, Richtung unbestimmt")
        else:
            if abweichung > 90:
                melde(2, f"Rückenwind aus {kompass(sd)} – Lee")
            elif abweichung > gw["richtung_gelb_grad"]:
                melde(2, f"Wind aus {kompass(sd)} statt {ort['startrichtung']}")
            elif abweichung > gw["richtung_gruen_grad"]:
                melde(1, f"Wind schräg ({abweichung:.0f}° Abweichung)")
            if sv > gw["wind_rot_kmh"]:
                melde(2, f"Wind zu stark ({sv:.0f} km/h)")
            elif sv > gw["wind_max_gruen_kmh"]:
                melde(1, f"Wind kräftig ({sv:.0f} km/h)")
            elif sv < gw["wind_min_gruen_kmh"]:
                melde(1, "Wind schwach")
    if boe is not None:
        if boe >= gw["boe_rot_kmh"]:
            melde(2, f"Böen {boe:.0f} km/h{boe_q}")
        elif boe >= gw["boe_gelb_kmh"]:
            melde(1, f"Böen {boe:.0f} km/h{boe_q}")
        # Spanne gegen den Startwind, nicht gegen das 10-m-Mittel: Modellböen liegen
        # über Land regelmäßig beim Doppelten des 10-m-Mittels.
        if sv is not None and boe - sv > gw["boeenspanne_gelb_kmh"]:
            melde(1, f"böig (Spanne {boe - sv:.0f} km/h{boe_q})")
    return abweichung


def windampel(sv, sd, boe, ort, gw):
    """Ampel nur aus Richtung, Stärke und Böen. Liefert (Stufe, Gründe)."""
    stufe, gruende = 0, []

    def melde(neu, text):
        nonlocal stufe
        stufe = max(stufe, neu)
        gruende.append(text)

    wind_regeln(sv, sd, boe, "", ort, gw, melde)
    return stufe, gruende


def verlauf_text(einzel):
    """Beschreibt den Wechsel der Stundenampel in einer Zeitstufe, z. B.
    '11 Uhr rot, ab 12 Uhr grün'. Leer, wenn alle Stunden gleich bewertet sind.
    einzel: Liste aus (Stunde, Stufe)."""
    abschnitte = []
    for stunde, stufe in einzel:
        if abschnitte and abschnitte[-1][2] == stufe:
            abschnitte[-1][1] = stunde
        else:
            abschnitte.append([stunde, stunde, stufe])
    if len(abschnitte) < 2:
        return ""
    teile = []
    for i, (von, bis, stufe) in enumerate(abschnitte):
        wort = AMPELWORT[stufe]
        if i == len(abschnitte) - 1:
            teile.append(f"ab {von} Uhr {wort}")
        elif von == bis:
            teile.append(f"{von} Uhr {wort}")
        else:
            teile.append(f"{von}–{bis} Uhr {wort}")
    return ", ".join(teile)


def stufe_auswerten(stunden, ort, cfg):
    gw = cfg["grenzwerte"]
    v10, d10 = vektor_mittel([(s["v10"], s["d10"]) for s in stunden])
    v80, d80 = vektor_mittel([(s["v80"], s["d80"]) for s in stunden])
    boe = maximum([s["boe"] for s in stunden])
    hw_v, hw_d = vektor_mittel([(s["hw_v"], s["hw_d"]) for s in stunden])
    hw_max = maximum([s["hw_max"] for s in stunden])
    w15_v, w15_d = vektor_mittel([(s["w15_v"], s["w15_d"]) for s in stunden])
    w15_z = mittel([s["w15_z"] for s in stunden])
    sv, sd = startwind(cfg, v10, d10, v80, d80)
    dwd_std = [s["dwd"] for s in stunden if s.get("dwd")]

    stufe, gruende, hinweise = 0, [], []

    def melde(neu, text):
        nonlocal stufe
        stufe = max(stufe, neu)
        gruende.append(text)

    # Wettercode der DWD-Segelflugvorhersage (ICAO), z. B. -RA, SHRA, TSRA, FG, BR
    for code in sorted({d["wetter"] for d in dwd_std if d.get("wetter")}):
        kern = code.lstrip("+-")
        if "TS" in kern:
            melde(2, f"Gewitter laut DWD ({code})")
        elif "FG" in kern:
            melde(2, f"Nebel laut DWD ({code})")
        elif any(k in kern for k in ("RA", "DZ", "SN", "SG", "GR", "GS", "PL", "SH", "FZ")):
            melde(2, f"Niederschlag laut DWD ({code})")
        elif any(k in kern for k in ("BR", "HZ")):
            melde(1, f"Dunst laut DWD ({code})")
        else:
            melde(1, f"Wetter laut DWD: {code}")

    regen = sum(s["regen"] or 0 for s in stunden)
    codes = [s["wcode"] for s in stunden if s["wcode"] is not None]
    if any(c >= 95 for c in codes):
        melde(2, "Gewitter")
    if any(c in (45, 48) for c in codes):
        melde(2, "Nebel")
    if regen >= gw["niederschlag_rot_mm"]:
        melde(2, f"Niederschlag {zahl(regen, 1)} mm")

    # Tiefe Wolkendecke: das Modell liefert keine Wolkenuntergrenze, daher dient die
    # Kondensationshöhe aus Temperatur und Taupunkt als Maß für die Nähe zum Startplatz.
    bedeckt = [s for s in stunden if s["wolken_tief"] is not None and s["wolken_tief"] >= gw["tiefwolken_gelb_prozent"]]
    if bedeckt:
        achtel = round(max(s["wolken_tief"] for s in bedeckt) / 12.5)
        nah = [s["lcl"] - ort["hoehe_m"] for s in bedeckt if s["lcl"] is not None]
        if nah and min(nah) < gw["wolkenbasis_rot_m"]:
            melde(2, f"tiefe Wolkendecke am Startplatz ({achtel}/8, Kondensationshöhe {min(nah):.0f} m über Start)")
        else:
            melde(1, f"tiefe Wolkendecke ({achtel}/8)")
    tief_dwd = maximum([d.get("wolken_tief") for d in dwd_std])
    if not bedeckt and tief_dwd is not None and tief_dwd * 12.5 >= gw["tiefwolken_gelb_prozent"]:
        melde(1, f"tiefe Wolkendecke laut DWD ({tief_dwd:.0f}/8)")
    sicht =[s["sicht"] for s in stunden if s["sicht"] is not None]
    if sicht:
        if min(sicht) < gw["sicht_rot_m"]:
            melde(2, f"Sicht {min(sicht):.0f} m")
        elif min(sicht) < gw["sicht_gelb_m"]:
            melde(1, f"Sicht {min(sicht) / 1000:.1f} km".replace(".", ","))

    # Böen: die stärkere von eigener Rechnung und DWD-Referenzstation zählt
    boe_dwd = maximum([d.get("boe") for d in dwd_std])
    boe_w, boe_q = boe, ""
    if boe_dwd is not None and (boe is None or boe_dwd > boe):
        boe_w, boe_q = boe_dwd, " laut DWD"
    abweichung = wind_regeln(sv, sd, boe_w, boe_q, ort, gw, melde)
    if hw_max is not None:
        if hw_max >= gw["hoehenwind_rot_kmh"]:
            melde(2, f"Höhenwind bis {hw_max:.0f} km/h")
        elif hw_max >= gw["hoehenwind_gelb_kmh"]:
            melde(1, f"Höhenwind bis {hw_max:.0f} km/h")
    # DWD-Wind in 914 m MSL: schließt die Lücke über der eigenen Arbeitsschicht
    w914 = [d["w914"][0] for d in dwd_std if d.get("w914")]
    if w914:
        if max(w914) >= gw["dwd_hoehenwind_rot_kmh"]:
            melde(2, f"Wind in 914 m bis {max(w914):.0f} km/h laut DWD")
        elif max(w914) >= gw["dwd_hoehenwind_gelb_kmh"]:
            melde(1, f"Wind in 914 m bis {max(w914):.0f} km/h laut DWD")
    scher_v = maximum([s["scher_v"] for s in stunden])
    scher_d = maximum([s["scher_d"] for s in stunden])
    if scher_d is not None and scher_d > gw["scherung_richtung_grad"]:
        melde(1, f"Windscherung: dreht bis zur Arbeitshöhe um {scher_d:.0f}°")
    if scher_v is not None and scher_v > gw["scherung_staerke_kmh"]:
        melde(1, f"Windscherung: nimmt bis zur Arbeitshöhe um {scher_v:.0f} km/h zu")
    cape = maximum([s["cape"] for s in stunden])
    if cape is not None and cape >= gw["cape_gelb"]:
        melde(1, f"Schauerneigung (CAPE {cape:.0f})")

    steigen = mittel([s["steigen"] for s in stunden])
    nutzbar = mittel([s["nutzbar"] for s in stunden])
    cumulus = sum(1 for s in stunden if s["cumulus"]) * 2 > len(stunden)
    spanne = [min(o, s["lcl"]) if s["lcl"] is not None else o
              for s in stunden for o in s["obergrenzen"].values()]
    wolken = [mittel([s[k] for s in stunden]) for k in ("wolken_tief", "wolken_mittel", "wolken_hoch")]

    # Schwache Thermik unter dem Eigensinken: allein nicht tragend, am Hang aber nutzbar,
    # weil dort der Hangaufwind das Sinken ausgleicht.
    th = cfg["thermik"]
    w_st = mittel([s["w_stern"] for s in stunden])
    wort = thermik_wort(steigen)
    hangthermik = (wort == "keine" and nutzbar is not None and w_st is not None
                   and nutzbar - ort["hoehe_m"] >= th["mindesttiefe_m"]
                   and w_st >= th.get("hangthermik_w_stern_ms", 0.5))
    if hangthermik:
        wort = "nur mit Hangaufwind"

    steigen_dwd = mittel([d.get("steigen") for d in dwd_std])
    if steigen_dwd is not None and steigen is not None and abs(steigen_dwd - steigen) > 1.0:
        hinweise.append(f"Steigen uneinig: DWD {zahl(steigen_dwd, 1)} m/s, eigen {zahl(steigen, 1)} m/s")

    if (wolken[1] or 0) >= 70 or (wolken[2] or 0) >= 80:
        hinweise.append("abgeschirmt")
    if sv is not None and abweichung is not None and stufe < 2:
        if sv * math.cos(math.radians(abweichung)) >= gw["soaring_min_kmh"]:
            hinweise.append("Hangaufwind wahrscheinlich")

    return {
        "stufe": stufe, "gruende": gruende, "hinweise": hinweise,
        "sv": sv, "sd": sd, "abweichung": abweichung,
        "v10": v10, "d10": d10, "boe": boe,
        "hw_v": hw_v, "hw_d": hw_d, "hw_max": hw_max,
        "w15_v": w15_v, "w15_d": w15_d, "w15_z": w15_z,
        "sicht": min(sicht) if sicht else None,
        "quelle_dwd": bool(dwd_std), "boe_dwd": boe_dwd, "steigen_dwd": steigen_dwd,
        "scher_v": scher_v, "scher_d": scher_d,
        "steigen": steigen, "thermikwort": wort,
        "w_stern": w_st, "hangthermik": hangthermik,
        "nutzbar": nutzbar, "cumulus": cumulus,
        "spanne": [min(spanne), max(spanne)] if spanne else None,
        "wolken": wolken,
    }


def ort_auswerten(tag, ort, haupt, vergleich, cfg, dwd=None):
    """Alle Stunden und Zeitstufen eines Startplatzes für einen Tag."""
    dwd_je_zeit = {s["zeit"]: s for s in dwd["stunden"]} if dwd else {}
    daten = haupt["daten"]
    tage = daten["daily"]["time"]
    if tag not in tage:
        return None
    k = tage.index(tag)
    aufgang, untergang = daten["daily"]["sunrise"][k], daten["daily"]["sunset"][k]
    h_von = int(aufgang[11:13])
    h_bis = min(23, int(untergang[11:13]) + (0 if untergang[14:16] == "00" else 1))

    stunden = {}
    for h in range(h_von, h_bis + 1):
        zeit = f"{tag}T{h:02d}:00"
        if zeit in daten["_index"]:
            stunde = stunde_auswerten(zeit, haupt, vergleich, ort, cfg, dwd_je_zeit.get(zeit))
            stunde["hell"] = aufgang <= zeit <= untergang
            # Stundenampel: dieselben Regeln wie für die Zeitstufe, auf die einzelne Stunde angewandt
            stunde["ampel"], stunde["ampel_gruende"] = None, []
            if stunde["hell"]:
                einzel = stufe_auswerten([stunde], ort, cfg)
                stunde["ampel"], stunde["ampel_gruende"] = einzel["stufe"], einzel["gruende"]
            stunden[zeit] = stunde

    stufen = []
    for zs in cfg["zeitstufen"]:
        zeiten = [f"{tag}T{h:02d}:00" for h in range(zs["von"], zs["bis"])]
        zeiten = [z for z in zeiten if z in stunden and stunden[z]["hell"]]
        eintrag = {"name": zs["name"], "von": zs["von"], "bis": zs["bis"], "dunkel": not zeiten}
        if zeiten:
            eintrag.update(stufe_auswerten([stunden[z] for z in zeiten], ort, cfg))
            # Die Zeitstufe mittelt den Wind über ihre Stunden. Sie darf dadurch nicht
            # besser ausfallen als ihre schlechteste Stunde.
            schlecht = max(stunden[z]["ampel"] for z in zeiten)
            if schlecht > eintrag["stufe"]:
                for z in zeiten:
                    if stunden[z]["ampel"] == schlecht:
                        eintrag["gruende"].append(f"{stunden[z]['stunde']} Uhr: " + ", ".join(stunden[z]["ampel_gruende"]))
                eintrag["stufe"] = schlecht
                if schlecht == 2:
                    eintrag["hinweise"] = [t for t in eintrag["hinweise"] if t != "Hangaufwind wahrscheinlich"]
            eintrag["stundenampel"] = [[stunden[z]["stunde"], stunden[z]["ampel"]] for z in zeiten]
            eintrag["verlauf"] = verlauf_text(eintrag["stundenampel"])
            eintrag["urteil"], eintrag["modelle"] = modell_urteil(
                zeiten, haupt, vergleich, ort, cfg, [stunden[z]["dwd"] for z in zeiten if stunden[z].get("dwd")])
        stufen.append(eintrag)
    return {"ort": ort, "aufgang": aufgang[11:], "untergang": untergang[11:],
            "stufen": stufen, "stunden": list(stunden.values())}


# ---------------------------------------------------------------- DWD-Segelflugvorhersage

class TabellenLeser(HTMLParser):
    """Liest alle Tabellen einer HTML-Seite als Listen von Zeilen mit Zelltexten."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tabellen, self._zeile, self._zelle, self._tiefe = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tiefe += 1
            self.tabellen.append([])
        elif self._tiefe and tag == "tr":
            self._zeile = []
        elif self._zeile is not None and tag in ("td", "th"):
            self._zelle = []

    def handle_endtag(self, tag):
        if tag == "table" and self._tiefe:
            self._tiefe -= 1
        elif tag == "tr" and self._zeile is not None:
            self.tabellen[-1].append(self._zeile)
            self._zeile = None
        elif tag in ("td", "th") and self._zelle is not None and self._zeile is not None:
            self._zeile.append(" ".join("".join(self._zelle).split()))
            self._zelle = None

    def handle_data(self, data):
        if self._zelle is not None:
            self._zelle.append(data)


def dwd_zahl(text):
    text = text.strip()
    if not text or set(text) <= {"-"}:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def dwd_wind(text):
    """'240/45' -> [Stärke km/h, Richtung Grad] wie in den eigenen Daten."""
    teile = text.split("/")
    if len(teile) != 2:
        return None
    richtung, staerke = dwd_zahl(teile[0]), dwd_zahl(teile[1])
    return None if richtung is None or staerke is None else [staerke, richtung]


def dwd_text(text):
    text = text.strip()
    return None if not text or set(text) <= {"-"} else text


# Zeilenanfang in der Tabelle der Referenzstation -> Schlüssel, Umwandlung
DWD_ZEILEN = [
    ("Bedeckung hohe", "wolken_hoch", dwd_zahl), ("Bedeckung mittelhohe", "wolken_mittel", dwd_zahl),
    ("Bedeckung tiefe", "wolken_tief", dwd_zahl),
    ("Wind 10000 FT", "w3048", dwd_wind), ("Wind 5000 FT", "w1524", dwd_wind), ("Wind 3000 FT", "w914", dwd_wind),
    ("Wetter", "wetter", dwd_text), ("Temperatur", "t", dwd_zahl), ("Taupunkt", "td", dwd_zahl),
    ("Windrichtung", "d10", dwd_zahl), ("Wind Mittel", "v10", dwd_zahl), ("Wind B", "boe", dwd_zahl),
    ("QNH", "qnh", dwd_zahl), ("Arbeitsh", "arbeitshoehe", dwd_zahl),
    ("Mittleres Steigen", "steigen", dwd_zahl), ("PFD", "pfd", dwd_zahl),
]


def hole_dwd_seite(url):
    anfrage = urllib.request.Request(url, headers={"User-Agent": "flugwetter-skript/1.0"})
    try:
        with urllib.request.urlopen(anfrage, timeout=40) as antwort:
            return antwort.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as fehler:
        raise RuntimeError(f"Abruf fehlgeschlagen ({fehler})") from None


def lade_dwd(cfg, utc_versatz_s):
    """Liest die Segelflug-Gebietsvorhersage des DWD (Tabellen einer HTML-Seite).
    Zweite Quelle für Ampel, Modellvergleich und Thermikhöhe am Tag des Berichts."""
    url = cfg["dwd_segelflug"]["url"]
    roh = hole_dwd_seite(url)

    text = " ".join(html_modul.unescape(re.sub(r"<[^>]+>", " ", roh)).split())
    m_tag = re.search(r"Vorhersagen f\S+r \w+, (\d\d)\.(\d\d)\.(\d{4})", text)
    m_ausgabe = re.search(r"Vorhersage ausgegeben (\d\d\.\d\d\.\d{4} \d\d:\d\d UTC) \(([^)]*)\)", text)
    m_gebiet = re.search(r"GAFOR-Gebiet (\d+) - ([^()]+?) \((\d+) m\)", text)
    m_station = re.search(r"Werte f\S+r die Referenzstation ([^()]+?) \(([^)]*)\)", text)
    leser = TabellenLeser()
    leser.feed(roh)
    station = next((t for t in leser.tabellen if t and t[0] and t[0][0].startswith("Prognosezeit")), None)
    if station is None or m_tag is None:
        raise RuntimeError("Aufbau der Seite nicht erkannt")

    basis = datetime(int(m_tag.group(3)), int(m_tag.group(2)), int(m_tag.group(1)))
    stunden, vorher, tage = [], -1, 0
    for zelle in station[0][1:]:
        m = re.search(r"(\d{1,2})$", zelle)
        if m is None:
            raise RuntimeError(f"Zeitspalte nicht lesbar: {zelle!r}")
        h = int(m.group(1))
        if h < vorher:
            tage += 1
        vorher = h
        lokal = basis + timedelta(days=tage, hours=h, seconds=utc_versatz_s)
        stunden.append({"utc": f"{h:02d}", "zeit": lokal.strftime("%Y-%m-%dT%H:00")})

    def fuelle(zeile, werte, vorsilbe=""):
        for anfang, schluessel, umwandlung in DWD_ZEILEN:
            if zeile.startswith(anfang):
                for stunde, zelle in zip(stunden, werte):
                    stunde[vorsilbe + schluessel] = umwandlung(zelle)
                return

    for zeile in station[1:]:
        if zeile:
            fuelle(zeile[0], zeile[1:])
    # Thermik-Gebietsangaben: Gebietsmittel und Maximum im Gebiet
    gebiet = next((t for t in leser.tabellen if t and len(t[0]) > 5 and t[0][0] == "Thermik"), None)
    if gebiet is not None:
        vorsilbe = ""
        for zeile in gebiet[1:]:
            if len(zeile) == len(gebiet[0]):
                vorsilbe = "max_" if zeile[0].startswith("Maximum") else "mittel_"
                zeile = zeile[1:]
            if zeile:
                fuelle(zeile[0], zeile[1:], vorsilbe)

    return {
        "url": url,
        "datum": basis.date().isoformat(),
        "gebiet": f"GAFOR-Gebiet {m_gebiet.group(1)} – {m_gebiet.group(2)} ({m_gebiet.group(3)} m)" if m_gebiet else "",
        "station": f"{m_station.group(1)} ({m_station.group(2)})" if m_station else "",
        "ausgabe": m_ausgabe.group(1) if m_ausgabe else "",
        "modell": m_ausgabe.group(2) if m_ausgabe else "",
        "stunden": stunden,
    }


def dwd_stufe(dwd, tag, zs):
    """Fasst die DWD-Stunden einer Zeitstufe zusammen (Ortszeit)."""
    zeiten = {f"{tag}T{h:02d}:00" for h in range(zs["von"], zs["bis"])}
    stunden = [s for s in dwd["stunden"] if s["zeit"] in zeiten]
    if not stunden:
        return None
    winde = [s["w914"] for s in stunden if s.get("w914")]
    v, d = vektor_mittel([(w[0], w[1]) for w in winde])
    return {"w914_v": v, "w914_d": d, "w914_max": maximum([w[0] for w in winde]),
            "wetter": sorted({s["wetter"] for s in stunden if s.get("wetter")})}


def markdown_dwd(dwd, tag):
    def wind(w):
        return "–" if not w else f"{w[1]:.0f}° · {w[0]:.0f}"
    zeilen = [f"### DWD-Segelflugvorhersage · {dwd['gebiet']}", "",
              f"Referenzstation {dwd['station']} · ausgegeben {dwd['ausgabe']} · {dwd['modell']} · {dwd['url']}", "",
              "| Zeit | Bodenwind | Böen | Wind 914 m | Wind 1524 m | Wind 3048 m | Wolken t/m/h (Achtel) | Wetter | T / Taupunkt | Arbeitshöhe | Steigen |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in dwd["stunden"]:
        if not s["zeit"].startswith(tag):
            continue
        boden = "–" if s.get("v10") is None or s.get("d10") is None else f"{s['d10']:.0f}° · {s['v10']:.0f} km/h"
        zeilen.append(f"| {s['zeit'][11:]} | {boden} | {zahl(s.get('boe'))} | {wind(s.get('w914'))} | {wind(s.get('w1524'))} | "
                      f"{wind(s.get('w3048'))} | {zahl(s.get('wolken_tief'))}/{zahl(s.get('wolken_mittel'))}/{zahl(s.get('wolken_hoch'))} | "
                      f"{s.get('wetter') or '–'} | {zahl(s.get('t'))} / {zahl(s.get('td'))} °C | "
                      f"{zahl(s.get('arbeitshoehe'))} | {zahl(s.get('steigen'), 1)} |")
    zeilen += ["", "Winde in Grad und km/h. Zeiten in Ortszeit (DWD-Tabelle in UTC). In die Ampel dieses Tages gehen ein: Böen, "
               "Wind in 914 m, Wettercode und tiefe Bewölkung; in den Modellvergleich die Bodenwindrichtung; in die Thermikhöhe die Arbeitshöhe.", ""]
    return zeilen


# ---------------------------------------------------------------- Holfuy-Messwerte und Rückschau

HOLFUY_FELDER = [("gd_speed", "wind"), ("gd_gust", "boe"), ("gd_min_wind", "wind_min"),
                 ("gd_direction", "richtung"), ("gd_temp", "temp"), ("gd_light", "licht"),
                 ("gd_pressure", "druck")]
ABGLEICH_SPALTEN = ["datum", "startplatz", "zeitstufe", "lauf_uhrzeit", "prognose_vor_beginn",
                    "prog_ampel", "prog_windampel", "prog_richtung_grad", "prog_wind80_kmh",
                    "prog_wind10_kmh", "prog_boe_kmh", "mess_anzahl", "mess_richtung_grad",
                    "mess_wind_kmh", "mess_boe_max_kmh", "mess_windampel", "diff_richtung_grad",
                    "diff_wind80_kmh", "diff_wind10_kmh", "diff_boe_kmh"]


def dateiname(text):
    for von, nach in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(von, nach)
    return re.sub(r"[^A-Za-z0-9-]+", "-", text).strip("-")


def hole_holfuy(station):
    """Datei, aus der die Holfuy-Stationsseite ihre Diagramme zeichnet (etwa fünf Tage)."""
    url = f"https://holfuy.com/dynamic/graphs/tdarr{station}.js"
    try:
        anfrage = urllib.request.Request(url, headers={"User-Agent": "flugwetter-skript/1.0"})
        with urllib.request.urlopen(anfrage, timeout=40) as antwort:
            return antwort.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as fehler:
        raise RuntimeError(f"Holfuy-Station {station} nicht abrufbar: {fehler}") from None


def lese_holfuy(text):
    """Liest die Messreihen als Liste von Messungen. Zeiten sind Ortszeit der Station."""
    felder = {name: [x.strip().strip("'\"") for x in inhalt.split(",")]
              for name, inhalt in re.findall(r"var\s+(\w+)\s*=\s*\[(.*?)\];", text, flags=re.S)}
    zeiten = felder.get("unt")
    if not zeiten or len(felder.get("gd_speed", [])) != len(zeiten):
        raise RuntimeError("Holfuy-Daten nicht lesbar (Aufbau geändert?)")
    messungen = []
    for i, z in enumerate(zeiten):
        try:
            messung = {"zeit": datetime.strptime(z, "%Y/%m/%d %H:%M:%S")}
        except ValueError:
            continue
        for quelle, ziel in HOLFUY_FELDER:
            reihe = felder.get(quelle)
            try:
                messung[ziel] = float(reihe[i]) if reihe and len(reihe) == len(zeiten) else None
            except ValueError:
                messung[ziel] = None
        messungen.append(messung)
    return messungen


def sichere_holfuy(ort, tag, werte):
    """Schreibt die Messwerte eines vollständigen Tages als CSV, wenn noch keine Datei besteht."""
    if werte[0]["zeit"].hour > 0 or werte[0]["zeit"].minute > 30:
        return None
    ziel = ORDNER / "messwerte"
    ziel.mkdir(exist_ok=True)
    datei = ziel / f"{tag.isoformat()}_Holfuy-{ort['holfuy_station']}_{dateiname(ort['name'])}_Messwerte.csv"
    if datei.exists():
        return None
    zeilen = ["zeit_ortszeit;wind_kmh;boe_kmh;wind_min_kmh;richtung_grad;temperatur_c;licht;druck_hpa"]
    for m in werte:
        zeilen.append(";".join([f"{m['zeit']:%H:%M}"] + ["" if m[k] is None else f"{m[k]:g}" for _, k in HOLFUY_FELDER]))
    datei.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    return datei


def erster_lauf(tag):
    """Frühester gespeicherter Vorhersagelauf eines Tages oder None."""
    dateien = sorted((ORDNER / "vorhersagen" / "laeufe").glob(f"{tag.isoformat()}_*_Vorhersage.json"))
    if not dateien:
        return None
    try:
        return json.loads(dateien[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def abgleich_ort(lauf, tag, ort, werte, gw):
    """Vergleicht je Zeitstufe die Vorhersage eines Laufs mit den Messwerten der Station."""
    eintrag = next((t for t in lauf.get("tage", []) if t.get("datum") == tag.isoformat()), None)
    erg = next((o for o in (eintrag or {}).get("orte", []) if o["ort"]["name"] == ort["name"]), None)
    if erg is None:
        return []
    try:
        erstellt = datetime.strptime(lauf["erstellt"], "%d.%m.%Y %H:%M")
    except (KeyError, ValueError):
        return []
    null_uhr = datetime.combine(tag, datetime.min.time())
    hell_von = datetime.strptime(f"{tag.isoformat()} {erg['aufgang']}", "%Y-%m-%d %H:%M")
    hell_bis = datetime.strptime(f"{tag.isoformat()} {erg['untergang']}", "%Y-%m-%d %H:%M")
    zeilen = []
    for z in erg["stufen"]:
        if z.get("dunkel"):
            continue
        beginn = max(null_uhr + timedelta(hours=z["von"]), hell_von)
        ende = min(null_uhr + timedelta(hours=z["bis"]), hell_bis)
        mess = [m for m in werte if beginn <= m["zeit"] < ende and m["wind"] is not None and m["richtung"] is not None]
        if not mess:
            continue
        m_sv, m_sd = vektor_mittel([(m["wind"], m["richtung"]) for m in mess])
        m_boe = maximum([m["boe"] for m in mess])
        m_stufe, m_gruende = windampel(m_sv, m_sd, m_boe, ort, gw)
        p_stufe_wind, _ = windampel(z.get("sv"), z.get("sd"), z.get("boe"), ort, gw)

        def diff(a, b):
            return None if a is None or b is None else a - b

        # Bei kaum Wind ist die Richtung unbestimmt; dann kein Richtungsvergleich
        d_richtung = None
        if z.get("sd") is not None and min(z.get("sv") or 0, m_sv) >= gw["wind_kaum_kmh"]:
            d_richtung = (z["sd"] - m_sd + 180) % 360 - 180
        zeilen.append({
            "zeitstufe": z["name"], "von": z["von"], "bis": z["bis"],
            "lauf": f"{erstellt:%H:%M}", "vor_beginn": erstellt <= null_uhr + timedelta(hours=z["von"]),
            "p_stufe": z["stufe"], "p_windstufe": p_stufe_wind,
            "p_sd": z.get("sd"), "p_sv": z.get("sv"), "p_v10": z.get("v10"), "p_boe": z.get("boe"),
            "m_n": len(mess), "m_sd": m_sd, "m_sv": m_sv, "m_boe": m_boe,
            "m_stufe": m_stufe, "m_gruende": m_gruende,
            "d_richtung": d_richtung, "d_sv": diff(z.get("sv"), m_sv),
            "d_v10": diff(z.get("v10"), m_sv), "d_boe": diff(z.get("boe"), m_boe),
        })
    return zeilen


def holfuy_abgleich(cfg, heute, warnungen, tage=None):
    """Holt die Messwerte aller Holfuy-Stationen, sichert vergangene Tage als CSV und
    vergleicht sie mit dem ersten gespeicherten Lauf des Tages. Neue Zeilen kommen in
    messwerte/abgleich.csv. Liefert die Rückschau auf den jüngsten verglichenen Tag.
    tage: nur zur Probe – legt die zu vergleichenden Tage fest und schreibt nichts."""
    probe = tage is not None
    tabelle = ORDNER / "messwerte" / "abgleich.csv"
    bekannt = set()
    if tabelle.exists():
        for zeile in tabelle.read_text(encoding="utf-8-sig").splitlines()[1:]:
            teile = zeile.split(";")
            if len(teile) >= 2:
                bekannt.add((teile[0], teile[1]))
    je_tag, neu = {}, []
    for ort in cfg["startplaetze"]:
        if not ort.get("holfuy_station"):
            continue
        try:
            messungen = lese_holfuy(hole_holfuy(ort["holfuy_station"]))
        except RuntimeError as fehler:
            warnungen.append(str(fehler))
            continue
        gruppen = {}
        for m in messungen:
            gruppen.setdefault(m["zeit"].date(), []).append(m)
        for tag, werte in sorted(gruppen.items()):
            if probe:
                if tag not in tage:
                    continue
            elif tag >= heute:
                continue
            else:
                sichere_holfuy(ort, tag, werte)
            lauf = erster_lauf(tag)
            if lauf is None:
                continue
            zeilen = abgleich_ort(lauf, tag, ort, werte, cfg["grenzwerte"])
            if not zeilen:
                continue
            je_tag.setdefault(tag, {"erstellt": lauf.get("erstellt", ""), "orte": []})["orte"].append(
                {"name": ort["name"], "station": ort["holfuy_station"], "zeilen": zeilen})
            if not probe and (tag.isoformat(), ort["name"]) not in bekannt:
                for z in zeilen:
                    neu.append([tag.isoformat(), ort["name"], z["zeitstufe"], z["lauf"],
                                "ja" if z["vor_beginn"] else "nein",
                                AMPELWORT[z["p_stufe"]], AMPELWORT[z["p_windstufe"]],
                                zahl(z["p_sd"], leer=""), zahl(z["p_sv"], leer=""), zahl(z["p_v10"], leer=""),
                                zahl(z["p_boe"], leer=""), str(z["m_n"]), zahl(z["m_sd"], leer=""),
                                zahl(z["m_sv"], leer=""), zahl(z["m_boe"], leer=""), AMPELWORT[z["m_stufe"]],
                                zahl(z["d_richtung"], leer=""), zahl(z["d_sv"], leer=""),
                                zahl(z["d_v10"], leer=""), zahl(z["d_boe"], leer="")])
    if neu:
        tabelle.parent.mkdir(exist_ok=True)
        if not tabelle.exists():
            tabelle.write_text(";".join(ABGLEICH_SPALTEN) + "\n", encoding="utf-8-sig")
        with open(tabelle, "a", encoding="utf-8") as datei:
            datei.writelines(";".join(zeile) + "\n" for zeile in neu)
    if not je_tag:
        return None
    tag = max(je_tag)
    return {"datum": tag.isoformat(), "titel": f"{WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.%Y}",
            "lauf": je_tag[tag]["erstellt"], "orte": je_tag[tag]["orte"]}


def markdown_rueckschau(rs):
    def vorzeichen(x, einheit):
        return "–" if x is None else f"{x:+.0f}{einheit}".replace("-", "−")
    zeilen = [f"## Rückschau {rs['titel']} – Prognose gegen Holfuy-Messung", "",
              f"Prognose aus dem ersten gespeicherten Lauf des Tages (erstellt {rs['lauf']}). "
              "Windampel = Ampel nur aus Richtung, Stärke und Böen. Abweichung = Prognose minus Messung.", ""]
    for o in rs["orte"]:
        zeilen += [f"### {o['name']} · Holfuy-Station {o['station']}", "",
                   "| Zeitstufe | Prognose Startwind 80 m | Prognose Böen | Ampel Prognose | davon Wind | Messung Wind | "
                   "Messung Böen | Windampel gemessen | Abweichung Richtung / Stärke / Böen |",
                   "|---|---|---|---|---|---|---|---|---|"]
        for z in o["zeilen"]:
            name = f"{z['zeitstufe']} {z['von']}–{z['bis']}" + ("" if z["vor_beginn"] else " (Lauf nach Beginn)")
            zeilen.append(f"| {name} | {wind_text(z['p_sv'], z['p_sd'])} | {zahl(z['p_boe'])} km/h | "
                          f"{AMPEL[z['p_stufe']]} | {AMPEL[z['p_windstufe']]} | {wind_text(z['m_sv'], z['m_sd'])} | "
                          f"{zahl(z['m_boe'])} km/h | {AMPEL[z['m_stufe']]} | "
                          f"{vorzeichen(z['d_richtung'], '°')} / {vorzeichen(z['d_sv'], ' km/h')} / {vorzeichen(z['d_boe'], ' km/h')} |")
        zeilen.append("")
    return zeilen


# ---------------------------------------------------------------- Zusammenfassung in Worten

def aufzaehlung(teile):
    teile = list(teile)
    if len(teile) < 2:
        return "".join(teile)
    return ", ".join(teile[:-1]) + " und " + teile[-1]


def zusammenfassung_ort(erg):
    """Fasst den Tag eines Startplatzes in Sätzen zusammen, nur aus den Werten der Zeitstufen."""
    stufen = [s for s in erg["stufen"] if not s["dunkel"]]
    if not stufen:
        return "Keine Zeitstufe im Tageslicht."

    def mit_grund(s):
        # Einzelheiten in Klammern stehen bei der Zeitstufe; hier nur das Stichwort
        kurz = [re.sub(r"\s*\([^)]*\)", "", g) for g in s["gruende"][:3]]
        text = f"{s['name']} ({', '.join(kurz)}" + (" u. a." if len(s["gruende"]) > 3 else "") + ")"
        return text + (f", dabei {s['verlauf']}" if s.get("verlauf") else "")

    gruen = [s for s in stufen if s["stufe"] == 0]
    gelb = [s for s in stufen if s["stufe"] == 1]
    rot = [s for s in stufen if s["stufe"] == 2]
    saetze = []
    if gruen:
        saetze.append(f"Nach den Grenzwerten startbar am {aufzaehlung(s['name'] for s in gruen)}.")
    if gelb:
        saetze.append("Eingeschränkt: " + "; ".join(mit_grund(s) for s in gelb) + ".")
    if rot:
        saetze.append(("Nicht startbar: " if gruen or gelb else "In keiner Zeitstufe startbar: ")
                      + "; ".join(mit_grund(s) for s in rot) + ".")

    winde = [f"{s['name']} {kompass(s['sd'])} {s['sv']:.0f}" for s in stufen if s["sv"] is not None and s["sd"] is not None]
    if winde:
        boe = maximum([s["boe"] for s in stufen])
        saetze.append("Startwind: " + ", ".join(winde) + " km/h" + (f"; Böen bis {boe:.0f} km/h." if boe is not None else "."))
    hang = [s["name"] for s in stufen if "Hangaufwind wahrscheinlich" in s["hinweise"]]
    if hang:
        saetze.append(f"Hangaufwind wahrscheinlich am {aufzaehlung(hang)}.")

    beste = max(stufen, key=lambda s: s["steigen"] or 0)
    hangthermik = [s for s in stufen if s.get("hangthermik") and s["nutzbar"] is not None]
    if (beste["steigen"] or 0) >= 0.3 and beste["nutzbar"] is not None:
        saetze.append(f"Thermik {beste['thermikwort']}, am stärksten am {beste['name']} mit {zahl(beste['steigen'], 1)} m/s "
                      f"bis etwa {round(beste['nutzbar'], -1):.0f} m MSL ({'Cumulus' if beste['cumulus'] else 'Blauthermik'}).")
    elif hangthermik:
        hoch = max(s["nutzbar"] for s in hangthermik)
        saetze.append(f"Schwache Thermik am {aufzaehlung(s['name'] for s in hangthermik)}, trägt nur mit Hangaufwind; "
                      f"damit bis etwa {round(hoch, -1):.0f} m MSL möglich.")
    else:
        saetze.append("Keine nutzbare Thermik.")

    unsicher = [f"{s['name']} ({s['urteil'][10:]})" for s in stufen if s.get("urteil", "").startswith("unsicher")]
    if unsicher:
        saetze.append("Modelle uneins: " + "; ".join(unsicher) + ".")
    elif all(s.get("urteil") == "einig" for s in stufen):
        saetze.append("Die Modelle sind sich einig.")
    return " ".join(saetze)


def zusammenfassung_tag(ergebnisse):
    """Ein Satz für den ganzen Tag: wo und wann es am ehesten geht."""
    def plaetze(stufe):
        teile = []
        for erg in ergebnisse:
            namen = [s["name"] for s in erg["stufen"] if not s["dunkel"] and s["stufe"] == stufe]
            if namen:
                teile.append(f"{erg['ort']['name']} am {aufzaehlung(namen)}")
        return teile
    gruen, gelb = plaetze(0), plaetze(1)
    if gruen:
        return "Am ehesten geht: " + "; ".join(gruen) + "." + (" Eingeschränkt: " + "; ".join(gelb) + "." if gelb else "")
    if gelb:
        return "Keine Zeitstufe ohne Einschränkung. Eingeschränkt möglich: " + "; ".join(gelb) + "."
    return "Nach den Grenzwerten ist an keinem der Startplätze eine Zeitstufe startbar."


# ---------------------------------------------------------------- Markdown

def thermik_zelle(e, ort, cfg):
    if e["nutzbar"] is None:
        return "–"
    ueber_start = e["nutzbar"] - ort["hoehe_m"]
    if ueber_start < cfg["thermik"]["mindesttiefe_m"]:
        return f"unter {cfg['thermik']['mindesttiefe_m']} m über Start"
    if e.get("hangthermik"):
        return (f"schwache Thermik (w* {zahl(e['w_stern'], 1)} m/s), trägt nur mit Hangaufwind; damit bis etwa "
                f"{round(e['nutzbar'], -1):.0f} m möglich (Durchmischung, {ueber_start:.0f} m über Start)")
    if not e["steigen"] or e["steigen"] < 0.3:
        return f"keine nutzbare Thermik (Durchmischung bis {round(e['nutzbar'], -1):.0f} m)"
    art = "Cu-Basis" if e["cumulus"] else "blau"
    text = f"{round(e['nutzbar'], -1):.0f} m ({ueber_start:.0f} m über Start, {art})"
    if e["spanne"] and not e["cumulus"]:
        text += f", Modelle {round(e['spanne'][0], -1):.0f}–{round(e['spanne'][1], -1):.0f}"
    return text


def markdown_ort(erg, cfg, optionen, modellnamen):
    ort = erg["ort"]
    zeilen = [f"### {ort['name']} · Start {ort['startrichtung']} ({ort['startrichtung_grad']}°) · "
              f"{ort['hoehe_m']} m · Höhendifferenz ca. {ort['hoehendifferenz_m']} m", "",
              f"Sonne {erg['aufgang']}–{erg['untergang']} · Livewind: {ort['livewind']}", "",
              "| Zeitstufe | Ampel | Startwind (80 m) | Böen | Bodenwind (10 m) | Höhenwind | Wind ca. 1500 m | Steigen | Thermik bis (MSL) | Wolken t/m/h | Modelle | Hinweise |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    kurz = []
    for e in erg["stufen"]:
        name = f"{e['name']} {e['von']}–{e['bis']}"
        if e["dunkel"]:
            zeilen.append(f"| {name} | ⚫ dunkel | – | – | – | – | – | – | – | – | – | außerhalb des Tageslichts |")
            kurz.append(f"{e['name']} ⚫")
            continue
        wind = wind_text(e["sv"], e["sd"])
        if e["abweichung"] is not None:
            wind += f" (Abw. {e['abweichung']:.0f}°)"
        steigen = "–" if not e["steigen"] or e["steigen"] < 0.05 else f"{zahl(e['steigen'], 1)} m/s"
        wolken = "/".join(zahl(w) for w in e["wolken"]) + " %"
        verlauf = [f"Stunden: {e['verlauf']}"] if e.get("verlauf") else []
        bemerkung = "; ".join(e["gruende"] + verlauf + e["hinweise"]) or "–"
        zeilen.append(f"| {name} | {AMPEL[e['stufe']]} | {wind} | {zahl(e['boe'])} km/h | "
                      f"{wind_text(e['v10'], e['d10'])} | {wind_text(e['hw_v'], e['hw_d'])} | "
                      f"{wind_text(e['w15_v'], e['w15_d'])} | "
                      f"{steigen} ({e['thermikwort']}) | {thermik_zelle(e, ort, cfg)} | "
                      f"{wolken} | {e['urteil']} | {bemerkung} |")
        kurz.append(f"{e['name']} {AMPEL[e['stufe']].split()[0]}")
    zeilen.append("")

    if optionen.modelle:
        hoehe = "80 m" if cfg.get("startwind_quelle") == "80m" else "10 m"
        zeilen += [f"Modellvergleich Startwind {hoehe} (Richtung · Mittel in km/h / Böe 10 m):", "",
                   "| Zeitstufe | " + " | ".join(modellnamen) + " | Urteil |",
                   "|---|" + "---|" * (len(modellnamen) + 1)]
        for e in erg["stufen"]:
            if e["dunkel"]:
                continue
            zellen = []
            for n in modellnamen:
                if n in e["modelle"]:
                    v, d, boe = e["modelle"][n]
                    zellen.append(f"{kompass(d)} {d:.0f}° · {v:.0f}/{zahl(boe)}")
                else:
                    zellen.append("–")
            zeilen.append(f"| {e['name']} {e['von']}–{e['bis']} | " + " | ".join(zellen) + f" | {e['urteil']} |")
        zeilen.append("")

    if optionen.stunden:
        zeilen += ["Stundenwerte:", "",
                   "| Zeit | Ampel | Wind 10 m | Wind 80 m | Böe | T / Taupunkt | Wärmestrom | w* | Steigen | Obergrenze je Modell (MSL) | Kondensationshöhe | Grenzschicht (≈ MSL) | Scherung | CAPE | updraft |",
                   "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for s in erg["stunden"]:
            if not s["hell"]:
                continue
            ober = ", ".join(f"{n} {round(h, -1):.0f}" for n, h in s["obergrenzen"].items()) or "–"
            # Modellwert ist Höhe über Modellgelände; Startplatzhöhe addiert, daher nur ungefähr MSL
            grenz = ", ".join(f"{n} {round(ort['hoehe_m'] + h, -1):.0f}" for n, h in s["grenzschicht"].items()) or "–"
            scherung = "–" if s["scher_v"] is None else f"{s['scher_v']:+.0f} km/h" + (f", {s['scher_d']:.0f}°" if s["scher_d"] is not None else "")
            ampel = AMPELWORT[s["ampel"]] + (": " + ", ".join(s["ampel_gruende"]) if s["ampel_gruende"] else "")
            zeilen.append(f"| {s['zeit'][11:]} | {ampel} | {wind_text(s['v10'], s['d10'])} | {wind_text(s['v80'], s['d80'])} | "
                          f"{zahl(s['boe'])} | {zahl(s['t'], 1)} / {zahl(s['td'], 1)} °C | "
                          f"{zahl(s['waermestrom_auf'])} W/m² | {zahl(s['w_stern'], 1)} | {zahl(s['steigen'], 1)} | "
                          f"{ober} | {zahl(s['lcl'])} m | {grenz} m | {scherung} | {zahl(s['cape'])} | {zahl(s['updraft'], 2)} |")
        zeilen.append("")
    return zeilen, kurz


# ---------------------------------------------------------------- Hauptprogramm

def main():
    parser = argparse.ArgumentParser(description="Flugwetter Böhming / Oberemmendorf")
    parser.add_argument("--datum", help="Rückblick auf einen vergangenen Tag (JJJJ-MM-TT)")
    parser.add_argument("--stunden", action="store_true", help="Markdown: Stundenwerte anhängen")
    parser.add_argument("--modelle", action="store_true", help="Markdown: Modellvergleich je Zeitstufe anhängen")
    parser.add_argument("--ohne-datei", action="store_true", help="keine Dateien schreiben")
    parser.add_argument("--oeffnen", action="store_true", help="HTML-Seite nach dem Lauf im Browser öffnen")
    parser.add_argument("--ohne-abgleich", action="store_true",
                        help="Holfuy-Messwerte nicht abrufen, keine Rückschau")
    optionen = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = json.loads((ORDNER / "config.json").read_text(encoding="utf-8"))
    rueckblick = date.fromisoformat(optionen.datum) if optionen.datum else None
    jetzt = datetime.now()

    warnungen, orte = [], []
    for ort in cfg["startplaetze"]:
        haupt = dict(cfg["modelle"]["haupt"])
        haupt["daten"] = lade_modell(cfg, ort, haupt, True, rueckblick)
        vergleich = []
        for modell in cfg["modelle"]["vergleich"]:
            modell = dict(modell)
            try:
                modell["daten"] = lade_modell(cfg, ort, modell, False, rueckblick)
            except RuntimeError as fehler:
                modell["daten"] = None
                warnungen.append(f"{modell['name']} für {ort['name']} nicht abrufbar: {fehler}")
            vergleich.append(modell)
        orte.append((ort, haupt, vergleich))

    dwd = None
    if not rueckblick and cfg.get("dwd_segelflug", {}).get("aktiv"):
        try:
            dwd = lade_dwd(cfg, orte[0][1]["daten"].get("utc_offset_seconds", 0))
        except RuntimeError as fehler:
            warnungen.append(f"DWD-Segelflugvorhersage nicht verfügbar: {fehler}")

    modellnamen = [cfg["modelle"]["haupt"]["name"]] + [m["name"] for m in cfg["modelle"]["vergleich"]]
    if dwd:
        modellnamen.append(DWD_SPALTE)
    titel = "Rückblick" if rueckblick else "Flugwetter"
    erstellt = f"{jetzt:%d.%m.%Y %H:%M}"
    zeilen = [f"# {titel} {cfg['gebiet']} – Gleitschirm", "",
              f"Erstellt {erstellt} · Hauptmodell {cfg['modelle']['haupt']['name']} über Open-Meteo · "
              "Modellschätzung, Grenzwerte nicht am Gelände geeicht. Vor dem Start Livewind und Gelände selbst beurteilen.", ""]
    seite = {
        "titel": titel, "gebiet": cfg["gebiet"], "erstellt": erstellt, "rueckblick": bool(rueckblick),
        "hauptmodell": cfg["modelle"]["haupt"]["name"], "modellnamen": modellnamen,
        "grenzwerte": cfg["grenzwerte"], "thermik": cfg["thermik"],
        "startwind_quelle": cfg.get("startwind_quelle", "10m"),
        "tage": [], "warnungen": warnungen, "dwd": dwd, "impressum": cfg.get("impressum"),
    }
    for tag in orte[0][1]["daten"]["daily"]["time"]:
        d = date.fromisoformat(tag)
        kopf = f"{WOCHENTAGE[d.weekday()]}, {d:%d.%m.%Y}"
        zeilen += [f"## {kopf}", ""]
        abschnitte, fazit, ergebnisse = [], [], []
        for ort, haupt, vergleich in orte:
            erg = ort_auswerten(tag, ort, haupt, vergleich, cfg, dwd)
            if erg is None:
                abschnitte += [f"### {ort['name']}", "", "Keine Daten für diesen Tag.", ""]
                continue
            if dwd:
                for stufe in erg["stufen"]:
                    stufe["dwd"] = dwd_stufe(dwd, tag, stufe)
            erg["zusammenfassung"] = zusammenfassung_ort(erg)
            ergebnisse.append(erg)
            teil, kurz = markdown_ort(erg, cfg, optionen, modellnamen)
            abschnitte += teil
            fazit.append(f"{ort['name']}: " + ", ".join(kurz))
        if fazit:
            zeilen += ["**Überblick:** " + " · ".join(fazit), ""]
        gesamt = zusammenfassung_tag(ergebnisse) if ergebnisse else ""
        if ergebnisse:
            zeilen += [f"**Zusammenfassung:** {gesamt}", ""]
            zeilen += [f"- **{e['ort']['name']}:** {e['zusammenfassung']}" for e in ergebnisse] + [""]
        zeilen += abschnitte
        if dwd and dwd["datum"] == tag:
            zeilen += markdown_dwd(dwd, tag)
        seite["tage"].append({"datum": tag, "titel": kopf, "kurz": f"{WOCHENTAGE[d.weekday()][:2]} {d:%d.%m.}",
                              "zusammenfassung": gesamt, "orte": ergebnisse})
    # Rückschau: Messwerte vergangener Tage sichern und gegen den ersten Lauf des Tages stellen
    seite["rueckschau"] = None
    if not rueckblick and not optionen.ohne_abgleich and not optionen.ohne_datei:
        try:
            seite["rueckschau"] = holfuy_abgleich(cfg, jetzt.date(), warnungen)
        except Exception as fehler:  # noqa: BLE001 – die Rückschau darf die Vorhersage nie verhindern
            warnungen.append(f"Rückschau nicht möglich: {type(fehler).__name__}: {fehler}")
        if seite["rueckschau"]:
            zeilen += markdown_rueckschau(seite["rueckschau"])
    if warnungen:
        zeilen += ["## Warnungen", ""] + [f"- {w}" for w in warnungen] + [""]

    text = "\n".join(zeilen)
    print(text)
    if optionen.ohne_datei:
        return
    ziel = ORDNER / "vorhersagen"
    ziel.mkdir(exist_ok=True)
    stempel = rueckblick.isoformat() if rueckblick else jetzt.date().isoformat()
    datei = ziel / f"{stempel}_{titel}_Boehming-Oberemmendorf.md"
    datei.write_text(text + "\n", encoding="utf-8")
    print(f"\nGespeichert: {datei}")

    vorlage = (ORDNER / "vorlage.html").read_text(encoding="utf-8")
    daten = json.dumps(gerundet(seite), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    if "/*DATEN*/null" not in vorlage:
        raise RuntimeError("vorlage.html enthält die Marke /*DATEN*/null nicht")
    seite_datei = ziel / (f"{stempel}_Rückblick.html" if rueckblick else "flugwetter_aktuell.html")
    seite_datei.write_text(vorlage.replace("/*DATEN*/null", daten), encoding="utf-8")
    print(f"Gespeichert: {seite_datei}")
    if not rueckblick:
        # Jeder Lauf bleibt erhalten, damit die Rückschau die Prognose vom Morgen findet
        laeufe = ziel / "laeufe"
        laeufe.mkdir(exist_ok=True)
        lauf_datei = laeufe / f"{jetzt:%Y-%m-%d_%H%M}_Vorhersage.json"
        lauf_datei.write_text(json.dumps(gerundet(seite), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"Gespeichert: {lauf_datei}")
    if optionen.oeffnen:
        webbrowser.open(seite_datei.as_uri())


def protokoll(text):
    """Hängt eine Zeile an vorhersagen/lauf.log an (für den Lauf ohne Fenster)."""
    try:
        ziel = ORDNER / "vorhersagen"
        ziel.mkdir(exist_ok=True)
        with open(ziel / "lauf.log", "a", encoding="utf-8") as datei:
            datei.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
        protokoll(("OK " + " ".join(sys.argv[1:])).strip())
    except RuntimeError as fehler:
        protokoll(f"FEHLER {fehler}")
        print(f"FEHLER: {fehler}", file=sys.stderr)
        sys.exit(1)
    except Exception as fehler:
        protokoll(f"FEHLER {type(fehler).__name__}: {fehler}")
        raise

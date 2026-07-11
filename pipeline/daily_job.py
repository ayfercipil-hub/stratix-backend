# -*- coding: utf-8 -*-
"""
STRATIX - Gunluk Analiz Isi (Asama 1)
=====================================
Her sabah GitHub Actions uzerinde calisir:
  1. football-data.co.uk'den guncel tarihi veriyi indirir (20 lig: 8 orijinal + 12 yeni), modeli egitir
  2. football-data.org'dan onumuzdeki 7 gunun fikstürünü ceker (orijinal 8 lig + Sampiyonlar Ligi)
     football-data.co.uk'den yeni 12 ligin fikstürlerini ceker
  3. Her mac icin olasiliklari hesaplar (Dixon-Coles v2; turnuva maclari icin
     lig-guc duzeltmeli capraz-lig surumu)
  4. Model ciktisindan 3 maddelik Turkce gerekce metni uretir (sablon tabanli,
     deterministik; dis LLM servisi YOK -> limit/kesinti riski yok)
  5. Sonuclari Firestore'a yazar (DEGISTIRILEMEZ tahmin gunlugu mantigiyla:
     ayni mac icin kayit varsa uzerine yazilmaz, 'guncelleme' ayri belge olur)
  6. Biten maclarin sonuclarini isler (seffaf gecmis paneli icin)

Gerekli ortam degiskenleri (GitHub Secrets):
  FIREBASE_SERVICE_ACCOUNT : Firebase servis hesabi JSON iceriginin tamami
  FOOTBALLDATA_KEY         : football-data.org API token'i (ucretsiz katman)
"""
import json
import os
import re
import sys
import time
import difflib
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd

from stratix_core import (load_matches, DixonColes, EDGE_THRESHOLD,
                          predict_cross_league)

# ------------------------------------------------------------------ ayarlar
SEASONS_BACK = 4                    # model icin kac sezon geriye gidilecek
LEAGUES = {                         # football-data.org lig kodu -> football-data.co.uk kodu
    "PL":  "E0",    # Premier League
    "BL1": "D1",    # Bundesliga
    "PD":  "SP1",   # La Liga
    "SA":  "I1",    # Serie A
    "FL1": "F1",    # Ligue 1
    "DED": "N1",    # Eredivisie (Hollanda)     - ucretsiz katmanda
    "PPL": "P1",    # Primeira Liga (Portekiz)  - ucretsiz katmanda
    "ELC": "E1",    # Championship (Ingiltere 2)- ucretsiz katmanda
}

# Yeni liglar: football-data.co.uk'dan bagisiz (fdo'da yok)
# Grup A: mmz4281/{season}/{div}.csv semasi
LEAGUES_MAIN_SCHEMA = {
    "B1": "Belcika Pro League",
    "SC0": "Iskocya Premiership",
    "G1": "Yunanistan Super League",
    "T1": "Turkiye Super Lig",
}

# Grup B: new/{code}.csv semasi (tum sezonlar tek dosya)
LEAGUES_EXTRA_SCHEMA = {
    "SWZ": "Switzerland",
    "AUT": "Austria",
    "NOR": "Norway",
    "SWE": "Sweden",
    "IRL": "Ireland",
    "FIN": "Finland",
    "ROU": "Romania",
    "RUS": "Russia",
}

# Capraz-lig turnuvalari (takimlar farkli liglerden gelir)
CROSS_COMPS = ["CL"]                # Sampiyonlar Ligi (ucretsiz katmanda)

# Lig guc duzeltmesi (log-gol olcegi; referans: Premier League = 0).
# Kaba baslangic degerleri; CL sonuclari biriktikce elle guncellenebilir.
LEAGUE_STRENGTH = {
    "E0": 0.00, "SP1": -0.05, "D1": -0.08, "I1": -0.08,
    "F1": -0.15, "P1": -0.22, "N1": -0.25, "E1": -0.55,
    # Yeni liglar
    "T1": -0.30, "B1": -0.30, "SC0": -0.45, "G1": -0.45,
    "AUT": -0.45, "SWZ": -0.45, "RUS": -0.40,
    "NOR": -0.55, "SWE": -0.60, "ROU": -0.60, "IRL": -0.70, "FIN": -0.70,
}
FD_ROOT = "https://www.football-data.co.uk"
FD_BASE = f"{FD_ROOT}/mmz4281"
FDO_BASE = "https://api.football-data.org/v4"
FDO_DELAY = 6.5                     # ucretsiz katman: dakikada 10 istek
HORIZON_DAYS = 7

# football-data.org takim adi -> football-data.co.uk takim adi (kismi harita;
# once ad normallestirilir (FC/AFC vb. ekler atilir), eslesmeyenler difflib
# ile denenir, o da olmazsa mac atlanir ve loglanir)
TEAM_MAP = {
    "Manchester United": "Man United", "Man United": "Man United",
    "Manchester City": "Man City", "Man City": "Man City",
    "Newcastle United": "Newcastle", "Newcastle": "Newcastle",
    "Nottingham Forest": "Nott'm Forest", "Nottingham": "Nott'm Forest",
    "Wolverhampton Wanderers": "Wolves", "Wolverhampton": "Wolves",
    "Tottenham Hotspur": "Tottenham", "Tottenham": "Tottenham",
    "Sheffield United": "Sheffield United", "Sheffield Utd": "Sheffield United",
    "Leeds United": "Leeds", "Leeds": "Leeds",
    "Brighton & Hove Albion": "Brighton", "Brighton Hove": "Brighton",
    "West Ham United": "West Ham", "West Ham": "West Ham",
    "Leicester City": "Leicester", "Ipswich Town": "Ipswich",
    "Bayern München": "Bayern Munich", "Bayern Munich": "Bayern Munich",
    "Borussia Dortmund": "Dortmund", "Bayer 04 Leverkusen": "Leverkusen",
    "Leverkusen": "Leverkusen",
    "Borussia Mönchengladbach": "M'gladbach", "M'gladbach": "M'gladbach",
    "Eintracht Frankfurt": "Ein Frankfurt", "Frankfurt": "Ein Frankfurt",
    "1. FSV Mainz 05": "Mainz", "Mainz 05": "Mainz",
    "VfB Stuttgart": "Stuttgart", "SC Freiburg": "Freiburg",
    "TSG 1899 Hoffenheim": "Hoffenheim", "Hoffenheim": "Hoffenheim",
    "1. FC Köln": "FC Koln", "Köln": "FC Koln",
    "St. Pauli": "St Pauli", "1. FC Union Berlin": "Union Berlin",
    "Union Berlin": "Union Berlin", "1. FC Heidenheim 1846": "Heidenheim",
    "Heidenheim": "Heidenheim", "VfL Bochum 1848": "Bochum",
    "Atlético de Madrid": "Ath Madrid", "Atleti": "Ath Madrid",
    "Atletico Madrid": "Ath Madrid",
    "Athletic Club": "Ath Bilbao", "Real Sociedad": "Sociedad",
    "Real Betis Balompié": "Betis", "Real Betis": "Betis",
    "RC Celta de Vigo": "Celta", "Celta Vigo": "Celta", "Celta": "Celta",
    "Rayo Vallecano de Madrid": "Vallecano", "Rayo Vallecano": "Vallecano",
    "Deportivo Alavés": "Alaves", "Alavés": "Alaves",
    "Cádiz": "Cadiz", "Almería": "Almeria", "Leganés": "Leganes",
    "AC Milan": "Milan", "Milan": "Milan",
    "Inter Milano": "Inter", "Inter": "Inter",
    "AS Roma": "Roma", "Roma": "Roma",
    "Hellas Verona": "Verona", "Verona": "Verona",
    "Paris Saint-Germain": "Paris SG", "Paris Saint Germain": "Paris SG",
    "PSG": "Paris SG",
    "Olympique de Marseille": "Marseille", "Marseille": "Marseille",
    "Olympique Lyonnais": "Lyon", "Lyon": "Lyon",
    "AS Saint-Étienne": "St Etienne", "Saint-Étienne": "St Etienne",
    "Stade Brestois 29": "Brest", "Stade Brestois": "Brest",
    "LOSC Lille": "Lille", "Lille": "Lille",
    "RC Lens": "Lens", "Lens": "Lens",
    "Stade Rennais": "Rennes", "Rennes": "Rennes",
    "RC Strasbourg Alsace": "Strasbourg", "Strasbourg": "Strasbourg",
    "Le Havre AC": "Le Havre", "Le Havre": "Le Havre",
    # --- Eredivisie (N1)
    "PSV Eindhoven": "PSV Eindhoven", "PSV": "PSV Eindhoven",
    "AFC Ajax": "Ajax", "Ajax": "Ajax",
    "Feyenoord Rotterdam": "Feyenoord", "Feyenoord": "Feyenoord",
    "AZ Alkmaar": "AZ Alkmaar", "AZ": "AZ Alkmaar",
    "FC Twente '65": "Twente", "Twente": "Twente",
    "FC Utrecht": "Utrecht", "Utrecht": "Utrecht",
    "SC Heerenveen": "Heerenveen", "Heerenveen": "Heerenveen",
    "NEC Nijmegen": "Nijmegen", "NEC": "Nijmegen",
    "Go Ahead Eagles": "Go Ahead Eagles",
    "Fortuna Sittard": "For Sittard", "Sparta Rotterdam": "Sparta Rotterdam",
    "PEC Zwolle": "Zwolle", "Zwolle": "Zwolle",
    "Almere City FC": "Almere City", "RKC Waalwijk": "Waalwijk",
    "FC Groningen": "Groningen", "Groningen": "Groningen",
    "Willem II Tilburg": "Willem II", "Willem II": "Willem II",
    "Heracles Almelo": "Heracles", "Heracles": "Heracles",
    # --- Primeira Liga (P1)
    "Sporting CP": "Sp Lisbon", "Sporting Lisbon": "Sp Lisbon",
    "Sporting": "Sp Lisbon",
    "SL Benfica": "Benfica", "Benfica": "Benfica",
    "FC Porto": "Porto", "Porto": "Porto",
    "SC Braga": "Sp Braga", "Braga": "Sp Braga",
    "Vitória SC": "Guimaraes", "Vitoria Guimaraes": "Guimaraes",
    "Vitória de Guimarães": "Guimaraes",
    "Boavista FC": "Boavista", "Boavista": "Boavista",
    "Casa Pia AC": "Casa Pia", "Casa Pia": "Casa Pia",
    "GD Estoril Praia": "Estoril", "Estoril": "Estoril",
    "FC Famalicão": "Famalicao", "Famalicão": "Famalicao",
    "Gil Vicente FC": "Gil Vicente", "Gil Vicente": "Gil Vicente",
    "Moreirense FC": "Moreirense", "Moreirense": "Moreirense",
    "CD Nacional": "Nacional", "Nacional": "Nacional",
    "Rio Ave FC": "Rio Ave", "Rio Ave": "Rio Ave",
    "CD Santa Clara": "Santa Clara", "Santa Clara": "Santa Clara",
    "FC Arouca": "Arouca", "Arouca": "Arouca",
    "AVS Futebol SAD": "AVS", "Estrela da Amadora": "Estrela",
    "CF Estrela da Amadora": "Estrela",
    # --- Championship (E1)
    "West Bromwich Albion": "West Brom", "West Brom": "West Brom",
    # --- Yeni liglar (CL icin kullanilacak isim eslemeleri)
    # Turkiye (T1)
    "Galatasaray": "Galatasaray", "Fenerbahce": "Fenerbahce",
    "Besiktas": "Besiktas", "Trabzonspor": "Trabzonspor",
    # Iskocya (SC0)
    "Celtic": "Celtic", "Rangers": "Rangers",
    # Belcika (B1)
    "Club Brugge": "Brugge", "Union Saint-Gilloise": "Union SG",
    "RSC Anderlecht": "Anderlecht", "KRC Genk": "Genk",
    # Yunanistan (G1)
    "Olympiacos": "Olympiacos", "PAOK": "PAOK", "Panathinaikos": "Panathinaikos",
    # Avusturya (AUT)
    "Red Bull Salzburg": "Salzburg", "Sturm Graz": "Sturm Graz",
    # Isvicre (SWZ)
    "FC Zurich": "Zurich", "Young Boys": "Young Boys",
    "FC Basel": "Basel", "Servette": "Servette",
    # Norvec (NOR)
    "Bodo/Glimt": "Bodo Glimt", "Molde": "Molde",
    "Stromsgodset": "Stromsgodset", "Rosenborg": "Rosenborg",
    # Isvec (SWE)
    "Malmo": "Malmo", "Djurgarden": "Djurgarden",
    # Romanya (ROU)
    "FCSB": "FCSB", "CFR Cluj": "Cluj", "Steaua Bucuresti": "Steaua",
    # Rusya (RUS)
    "Zenit": "Zenit", "Lokomotiv Moscow": "Lokomotiv",
    # Irlanda (IRL)
    "Shamrock Rovers": "Shamrock Rovers",
    "Queens Park Rangers": "QPR", "QPR": "QPR",
    "Sheffield Wednesday": "Sheffield Weds", "Sheffield Wed": "Sheffield Weds",
    "Preston North End": "Preston", "Preston": "Preston",
    "Blackburn Rovers": "Blackburn", "Blackburn": "Blackburn",
    "Bristol City": "Bristol City",
    "Coventry City": "Coventry", "Coventry": "Coventry",
    "Derby County": "Derby", "Derby": "Derby",
    "Hull City": "Hull", "Hull": "Hull",
    "Luton Town": "Luton", "Luton": "Luton",
    "Middlesbrough FC": "Middlesbrough", "Middlesbrough": "Middlesbrough",
    "Millwall FC": "Millwall", "Millwall": "Millwall",
    "Norwich City": "Norwich", "Norwich": "Norwich",
    "Oxford United": "Oxford", "Oxford Utd": "Oxford",
    "Plymouth Argyle": "Plymouth", "Plymouth": "Plymouth",
    "Portsmouth FC": "Portsmouth", "Portsmouth": "Portsmouth",
    "Stoke City": "Stoke", "Stoke": "Stoke",
    "Swansea City": "Swansea", "Swansea": "Swansea",
    "Watford FC": "Watford", "Watford": "Watford",
    "Cardiff City": "Cardiff", "Cardiff": "Cardiff",
    "Burnley FC": "Burnley", "Burnley": "Burnley",
    "Sunderland AFC": "Sunderland", "Sunderland": "Sunderland",
}

# ad normallestirme: "FC", "AFC", "CF", "SSC" gibi ekleri temizle
_STRIP = re.compile(
    r"\b(FC|AFC|CF|SSC|SS|AS|AC|SC|RC|VfL|VfB|TSG|US|UC|CD|SD|RCD|OGC|SM|"
    r"Calcio|1\.|1846|1848|04|05|1899|29)\b\.?")


def normalize(name):
    n = _STRIP.sub("", name)
    return " ".join(n.replace("  ", " ").split())


def slugify(name):
    """Takım adını URL-safe hale getir: küçükle, alfanumerik + - tutar."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def season_codes(today):
    """Aktif + gecmis sezon kodlari (orn. Temmuz 2026 -> ['2223',...,'2526'])."""
    # Avrupa sezonu Agustos'ta baslar; Haziran/Temmuz'da onceki sezon 'aktif'tir
    year = today.year if today.month >= 8 else today.year - 1
    codes = []
    for k in range(SEASONS_BACK, -1, -1):
        y = year - k
        codes.append(f"{str(y)[2:]}{str(y + 1)[2:]}")
    return codes


def download_history(tmpdir="fd_data"):
    os.makedirs(tmpdir, exist_ok=True)
    today = datetime.now(timezone.utc)
    paths = []

    # Orijinal liglar: sezonlara gore
    for s in season_codes(today):
        for code in LEAGUES.values():
            url = f"{FD_BASE}/{s}/{code}.csv"
            p = os.path.join(tmpdir, f"{code}_{s}.csv")
            try:
                r = requests.get(url, timeout=30)
                if r.status_code == 200 and len(r.content) > 1000:
                    open(p, "wb").write(r.content)
                    paths.append(p)
            except requests.RequestException as e:
                print(f"UYARI: {url} indirilemedi: {e}")

    # Grup A: Ana semasi liglar (sezonlara gore)
    for s in season_codes(today):
        for code in LEAGUES_MAIN_SCHEMA.keys():
            url = f"{FD_BASE}/{s}/{code}.csv"
            p = os.path.join(tmpdir, f"{code}_{s}.csv")
            try:
                r = requests.get(url, timeout=30)
                if r.status_code == 200 and len(r.content) > 1000:
                    open(p, "wb").write(r.content)
                    paths.append(p)
            except requests.RequestException as e:
                print(f"UYARI: {url} indirilemedi: {e}")

    # Grup B: Ekstra semasi liglar (tek dosya, tum sezonlar)
    for code in LEAGUES_EXTRA_SCHEMA.keys():
        url = f"{FD_ROOT}/new/{code}.csv"
        p = os.path.join(tmpdir, f"{code}_extra.csv")
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                open(p, "wb").write(r.content)
                paths.append(p)
            else:
                print(f"UYARI: {url} beklenen veriyi dondurmedi (HTTP {r.status_code}, {len(r.content)} bayt)")
        except requests.RequestException as e:
            print(f"UYARI: {url} indirilemedi: {e}")

    print(f"{len(paths)} tarihi veri dosyasi indirildi.")
    return paths


def adapt_extra_schema(df):
    """Ekstra semasi CSV'sini ana semasi'na uyarla.
    Kolon adlari: Home->HomeTeam, Away->AwayTeam, HG->FTHG, AG->FTAG
    Tarih degeri son 5 yildan geri getirilir, eksik skorlar cikarirlir.
    """
    if "Home" not in df.columns:
        return df
    d = df.copy()
    # Kolonu yeniden adlandir
    if "Home" in d.columns:
        d.rename(columns={"Home": "HomeTeam"}, inplace=True)
    if "Away" in d.columns:
        d.rename(columns={"Away": "AwayTeam"}, inplace=True)
    if "HG" in d.columns:
        d.rename(columns={"HG": "FTHG"}, inplace=True)
    if "AG" in d.columns:
        d.rename(columns={"AG": "FTAG"}, inplace=True)
    # Tarih dilimi: son 5 yil
    cutoff = datetime.now(timezone.utc) - timedelta(days=5*365)
    if "Date" in d.columns:
        try:
            d["Date"] = pd.to_datetime(d["Date"], dayfirst=True, format="mixed", errors="coerce")
            d = d[d["Date"] >= cutoff]
        except Exception:
            pass
    # Eksik skorlar
    d = d.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    return d


_last_call = [0.0]


def fdo_get(path, params, key):
    """football-data.org v4 GET; dakikada 10 istek limitine uyar."""
    wait = FDO_DELAY - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    r = requests.get(f"{FDO_BASE}{path}", params=params,
                     headers={"X-Auth-Token": key}, timeout=30)
    _last_call[0] = time.time()
    if r.status_code == 429:          # limit asildi -> bekle, bir kez dene
        time.sleep(65)
        r = requests.get(f"{FDO_BASE}{path}", params=params,
                         headers={"X-Auth-Token": key}, timeout=30)
        _last_call[0] = time.time()
    if r.status_code != 200:
        print(f"football-data.org hata ({path}): {r.status_code} {r.text[:200]}")
        return {}
    return r.json()


def download_fixtures_main_schema(tmpdir="fd_data"):
    """Grup A liglar icin: https://www.football-data.co.uk/fixtures.csv
    Div, Date, Time, HomeTeam, AwayTeam, ... (artı oran kolonları)
    Div in {B1,SC0,G1,T1} olanları döndür."""
    os.makedirs(tmpdir, exist_ok=True)
    url = f"{FD_ROOT}/fixtures.csv"
    p = os.path.join(tmpdir, "fixtures_main.csv")
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and len(r.content) > 100:
            open(p, "wb").write(r.content)
            df = pd.read_csv(p, encoding="utf-8-sig", on_bad_lines="skip")
            df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
            if "Div" in df.columns and "Date" in df.columns:
                # Tarih filtreleme: bugünden 7 gün ileri
                today = pd.Timestamp(datetime.now(timezone.utc).date())
                cutoff = today + pd.Timedelta(days=HORIZON_DAYS)
                try:
                    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
                    df = df[(df["Date"] >= today) & (df["Date"] <= cutoff)]
                except Exception:
                    pass
                df = df[df["Div"].isin(list(LEAGUES_MAIN_SCHEMA.keys()))]
                return df
    except requests.RequestException as e:
        print(f"UYARI: {url} indirilemedi: {e}")
    return pd.DataFrame()


def download_fixtures_extra_schema(tmpdir="fd_data"):
    """Grup B liglar icin: https://www.football-data.co.uk/new_league_fixtures.csv
    Country, League, Date, Time, Home, Away, ... (artı oran kolonları)
    Country kodu maplanır: Switzerland->SWZ, Austria->AUT, ... vb."""
    os.makedirs(tmpdir, exist_ok=True)
    url = f"{FD_ROOT}/new_league_fixtures.csv"
    p = os.path.join(tmpdir, "fixtures_extra.csv")
    country_map = {v: k for k, v in LEAGUES_EXTRA_SCHEMA.items()}
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and len(r.content) > 100:
            open(p, "wb").write(r.content)
            df = pd.read_csv(p, encoding="utf-8-sig", on_bad_lines="skip")
            df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
            if "Country" in df.columns and "Date" in df.columns:
                # Tarih filtreleme
                today = pd.Timestamp(datetime.now(timezone.utc).date())
                cutoff = today + pd.Timedelta(days=HORIZON_DAYS)
                try:
                    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
                    df = df[(df["Date"] >= today) & (df["Date"] <= cutoff)]
                except Exception:
                    pass
                # Country'yi Div koduna map et
                df["Div"] = df["Country"].map(country_map)
                df = df[df["Div"].notna()]
                # Home->HomeTeam, Away->AwayTeam (load_matches format'ina uyumlu ama
                # burada fixtures oldugu icin, ozel isleme ihtiyac yok)
                return df
    except requests.RequestException as e:
        print(f"UYARI: {url} indirilemedi: {e}")
    return pd.DataFrame()


def match_team(src_name, fd_teams):
    """football-data.org adini football-data.co.uk adina esler."""
    for cand in (src_name, normalize(src_name)):
        if cand in TEAM_MAP and TEAM_MAP[cand] in fd_teams:
            return TEAM_MAP[cand]
        if cand in fd_teams:
            return cand
    close = difflib.get_close_matches(normalize(src_name), list(fd_teams),
                                      n=1, cutoff=0.75)
    return close[0] if close else None


def gerekce_uret(rec):
    """Model ciktisindan 3 maddelik Turkce gerekce uretir.

    Sablon tabanli ve deterministiktir: dis LLM servisi kullanilmaz, bu yuzden
    hiz limiti / kesinti / maliyet riski yoktur. Sayilar modelin kendi
    ciktisidir; metin sadece bu sayilari anlatir ('model hesaplar' ilkesi).
    Kumar tesviki icermez; 'kesin', 'garanti' gibi ifadeler kullanilmaz.
    """
    ph, pdr, pa = rec["pH"] * 100, rec["pD"] * 100, rec["pA"] * 100
    po = rec["pO25"] * 100
    home, away = rec["home"], rec["away"]

    # 1) sonuc dagilimi
    sirali = sorted([ph, pdr, pa], reverse=True)
    if sirali[0] - sirali[1] < 5:
        m1 = (f"Model olasılıkları birbirine yakın hesaplıyor "
              f"({home} %{ph:.0f}, beraberlik %{pdr:.0f}, {away} %{pa:.0f}); "
              f"net bir favori görünmüyor.")
    else:
        if ph == sirali[0]:
            taraf = f"ev sahibi {home}"
        elif pa == sirali[0]:
            taraf = f"deplasman ekibi {away}"
        else:
            taraf = "beraberlik"
        m1 = (f"Model en yüksek olasılığı {taraf} için hesaplıyor: "
              f"{home} %{ph:.0f}, beraberlik %{pdr:.0f}, {away} %{pa:.0f}.")

    # 2) gol beklentisi
    if po >= 60:
        egilim = "gollü bir maç ihtimali öne çıkıyor"
    elif po <= 40:
        egilim = "düşük skorlu bir maç ihtimali öne çıkıyor"
    else:
        egilim = "gol beklentisi dengeli görünüyor"
    m2 = (f"Beklenen gol sayıları {rec['lam']:.1f} - {rec['mu']:.1f}; "
          f"2,5 üstü olasılığı %{po:.0f}, yani {egilim}.")

    # 3) dinlenme / form durumu
    rh, ra = rec.get("rest_h", 7), rec.get("rest_a", 7)
    fark = rh - ra
    if min(rh, ra) > 20:
        m3 = ("İki takım da uzun bir aradan sonra sahaya çıkıyor; "
              "form durumu her zamankinden daha belirsiz olabilir.")
    elif fark >= 3:
        m3 = (f"{home} rakibinden {fark:.0f} gün daha fazla dinlenmiş durumda; "
              f"bu küçük bir avantaj olabilir.")
    elif fark <= -3:
        m3 = (f"{away} rakibinden {-fark:.0f} gün daha fazla dinlenmiş durumda; "
              f"bu küçük bir avantaj olabilir.")
    else:
        m3 = (f"İki takım da benzer dinlenme süresiyle sahaya çıkıyor "
              f"({rh:.0f} ve {ra:.0f} gün); yorgunluk farkı belirleyici görünmüyor.")

    return "• " + m1 + "\n• " + m2 + "\n• " + m3


def main():
    fdo_key = os.environ.get("FOOTBALLDATA_KEY")
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not fdo_key or not sa_json:
        sys.exit("HATA: FOOTBALLDATA_KEY ve FIREBASE_SERVICE_ACCOUNT zorunlu.")

    # --- Firestore baglantisi
    import firebase_admin
    from firebase_admin import credentials, firestore
    cred = credentials.Certificate(json.loads(sa_json))
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    # --- 1) modeli egit
    paths = download_history()
    matches = load_matches(paths)
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    models, rest_info = {}, {}

    # Orijinal liglar + Grup A (ana semasi)
    all_div_codes = set(LEAGUES.values()) | set(LEAGUES_MAIN_SCHEMA.keys())
    for code in all_div_codes:
        dd = matches[matches["Div"] == code]
        m = DixonColes()
        if m.fit(dd, as_of_date=today + pd.Timedelta(days=1)):
            models[code] = m
        # takimlarin son mac tarihi -> dinlenme gunu tahmini
        last = {}
        for _, r in dd.iterrows():
            last[r["HomeTeam"]] = r["Date"]
            last[r["AwayTeam"]] = r["Date"]
        rest_info[code] = last

    # Grup B: Ekstra semasi dosyalari elle islenmeli (sezonlara gore parcalandi, Div eklenmemis)
    tmpdir = "fd_data"
    for code in LEAGUES_EXTRA_SCHEMA.keys():
        p = os.path.join(tmpdir, f"{code}_extra.csv")
        if os.path.exists(p):
            try:
                df = pd.read_csv(p, encoding="utf-8-sig", on_bad_lines="skip")
                df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
                df = adapt_extra_schema(df)
                if not df.empty:
                    df["Div"] = code  # Div sutununu ekle
                    # matches veri cerceveisine ekle
                    matches = pd.concat([matches, df], ignore_index=True)
            except Exception as e:
                print(f"UYARI: {code} ekstra semasi islenmedi: {e}")

    # Grup B modelleri egit
    for code in LEAGUES_EXTRA_SCHEMA.keys():
        dd = matches[matches["Div"] == code]
        if not dd.empty:
            m = DixonColes()
            if m.fit(dd, as_of_date=today + pd.Timedelta(days=1)):
                models[code] = m
            last = {}
            for _, r in dd.iterrows():
                last[r["HomeTeam"]] = r["Date"]
                last[r["AwayTeam"]] = r["Date"]
            rest_info[code] = last

    print(f"{len(models)} lig icin model egitildi.")

    # --- 2) fikstur cek ve tahmin uret
    n_written = n_skipped = 0
    for comp, code in LEAGUES.items():
        if code not in models:
            continue
        data = fdo_get(f"/competitions/{comp}/matches", {
            "dateFrom": str(today.date()),
            "dateTo": str((today + pd.Timedelta(days=HORIZON_DAYS)).date())},
            fdo_key)
        fixtures = data.get("matches", [])
        fd_teams = set(models[code].teams)
        for fx in fixtures:
            if fx.get("status") in ("FINISHED", "POSTPONED", "CANCELLED"):
                continue
            fid = fx["id"]
            h_src = fx["homeTeam"].get("shortName") or fx["homeTeam"]["name"]
            a_src = fx["awayTeam"].get("shortName") or fx["awayTeam"]["name"]
            h = match_team(h_src, fd_teams)
            a = match_team(a_src, fd_teams)
            if not h or not a:
                print(f"Eslesmedi, atlandi: {h_src} / {a_src}")
                n_skipped += 1
                continue
            kickoff = fx["utcDate"]
            rh = (today - rest_info[code].get(h, today - pd.Timedelta(days=7))).days
            ra = (today - rest_info[code].get(a, today - pd.Timedelta(days=7))).days
            pr = models[code].predict(h, a, rest_h=rh, rest_a=ra)
            if pr is None:
                n_skipped += 1
                continue
            rec = {
                "fixture_id": fid, "league_fd": code, "league_comp": comp,
                "home": h, "away": a, "home_src": h_src, "away_src": a_src,
                "kickoff": kickoff,
                "pH": round(float(pr["pH"]), 4), "pD": round(float(pr["pD"]), 4),
                "pA": round(float(pr["pA"]), 4),
                "pO25": round(float(pr["pO25"]), 4),
                "pU25": round(float(pr["pU25"]), 4),
                "lam": round(float(pr["lam"]), 3), "mu": round(float(pr["mu"]), 3),
                "rest_h": rh, "rest_a": ra,
                "model_version": "dc-v2",
                "created_at": firestore.SERVER_TIMESTAMP,
            }
            # DEGISTIRILEMEZLIK: ilk kayit predictions/{fid}; sonraki kosularda
            # degisiklik varsa updates alt koleksiyonuna eklenir, ilki bozulmaz
            doc = db.collection("predictions").document(str(fid))
            snap = doc.get()
            if not snap.exists:
                rec["gerekce"] = gerekce_uret(rec)
                doc.set(rec)
                n_written += 1
            else:
                old = snap.to_dict()
                if abs(old.get("pH", 0) - rec["pH"]) > 0.03:
                    doc.collection("updates").add(rec)
    print(f"{n_written} yeni tahmin yazildi, {n_skipped} mac atlandi.")

    # --- 2a) Yeni liglar: football-data.co.uk fikstürleri (Grup A + Grup B)
    n_new_written = n_new_skipped = n_atlandi = 0

    # Grup A: Ana semasi fikstürleri
    fx_main = download_fixtures_main_schema()
    for _, fx in fx_main.iterrows():
        code = fx.get("Div")
        if code not in models:
            continue
        home = fx.get("HomeTeam", "")
        away = fx.get("AwayTeam", "")
        date_str = fx.get("Date", "")
        time_str = fx.get("Time", "")
        if not (home and away and date_str):
            n_atlandi += 1
            continue

        fd_teams = set(models[code].teams)
        # Isim eslemesi: tam match gerekli (football-data.co.uk ile ayni kaynak)
        if home not in fd_teams or away not in fd_teams:
            print(f"{code}: Takimlar bilinmiyor, atlandi: {home} vs {away}")
            n_new_skipped += 1
            n_atlandi += 1
            continue

        # Fixture ID: fdcuk-{lig}-{YYYYMMDD}-{home_slug}-{away_slug}
        try:
            dt = pd.Timestamp(date_str)
            date_ymd = dt.strftime("%Y%m%d")
        except Exception:
            n_atlandi += 1
            continue

        fixture_id = f"fdcuk-{code}-{date_ymd}-{slugify(home)}-{slugify(away)}"

        # UTC tarihi (UK saati varsayıp UTC'ye çevir)
        # kickoff mutlaka saat dilimli (UTC) yazilir; yoksa sonuc isleme
        # bolumundeki tz_convert cagrisi hata verir
        kick = dt
        if time_str:
            try:
                time_obj = pd.Timestamp(str(time_str)).time()
                kick = dt.replace(hour=time_obj.hour, minute=time_obj.minute)
            except Exception:
                pass
        utc_date_str = kick.tz_localize("UTC").isoformat()

        # Dinlenme günü hesapla
        rh = (today - rest_info[code].get(home, today - pd.Timedelta(days=7))).days
        ra = (today - rest_info[code].get(away, today - pd.Timedelta(days=7))).days
        pr = models[code].predict(home, away, rest_h=rh, rest_a=ra)
        if pr is None:
            n_new_skipped += 1
            n_atlandi += 1
            continue

        rec = {
            "fixture_id": fixture_id, "league_fd": code, "league_comp": code,
            "home": home, "away": away, "home_src": home, "away_src": away,
            "kickoff": utc_date_str,
            "pH": round(float(pr["pH"]), 4), "pD": round(float(pr["pD"]), 4),
            "pA": round(float(pr["pA"]), 4),
            "pO25": round(float(pr["pO25"]), 4), "pU25": round(float(pr["pU25"]), 4),
            "lam": round(float(pr["lam"]), 3), "mu": round(float(pr["mu"]), 3),
            "rest_h": rh, "rest_a": ra,
            "model_version": "dc-v2",
            "created_at": firestore.SERVER_TIMESTAMP,
        }
        doc = db.collection("predictions").document(fixture_id)
        snap = doc.get()
        if not snap.exists:
            rec["gerekce"] = gerekce_uret(rec)
            doc.set(rec)
            n_new_written += 1
        else:
            old = snap.to_dict()
            if abs(old.get("pH", 0) - rec["pH"]) > 0.03:
                doc.collection("updates").add(rec)

    print(f"Grup A (ana semasi): {n_new_written} yeni tahmin, {n_new_skipped} mac atlandi, {n_atlandi} veri sorunu.")

    # Grup B: Ekstra semasi fikstürleri
    n_extra_written = n_extra_skipped = n_extra_atlandi = 0
    fx_extra = download_fixtures_extra_schema()
    for _, fx in fx_extra.iterrows():
        code = fx.get("Div")
        if code not in models:
            continue
        home = fx.get("Home", "")
        away = fx.get("Away", "")
        date_str = fx.get("Date", "")
        time_str = fx.get("Time", "")
        if not (home and away and date_str):
            n_extra_atlandi += 1
            continue

        fd_teams = set(models[code].teams)
        if home not in fd_teams or away not in fd_teams:
            print(f"{code} (Grup B): Takimlar bilinmiyor, atlandi: {home} vs {away}")
            n_extra_skipped += 1
            n_extra_atlandi += 1
            continue

        try:
            dt = pd.Timestamp(date_str)
            date_ymd = dt.strftime("%Y%m%d")
        except Exception:
            n_extra_atlandi += 1
            continue

        fixture_id = f"fdcuk-{code}-{date_ymd}-{slugify(home)}-{slugify(away)}"
        # kickoff mutlaka saat dilimli (UTC) yazilir; yoksa sonuc isleme
        # bolumundeki tz_convert cagrisi hata verir
        kick = dt
        if time_str:
            try:
                time_obj = pd.Timestamp(str(time_str)).time()
                kick = dt.replace(hour=time_obj.hour, minute=time_obj.minute)
            except Exception:
                pass
        utc_date_str = kick.tz_localize("UTC").isoformat()

        rh = (today - rest_info[code].get(home, today - pd.Timedelta(days=7))).days
        ra = (today - rest_info[code].get(away, today - pd.Timedelta(days=7))).days
        pr = models[code].predict(home, away, rest_h=rh, rest_a=ra)
        if pr is None:
            n_extra_skipped += 1
            n_extra_atlandi += 1
            continue

        rec = {
            "fixture_id": fixture_id, "league_fd": code, "league_comp": code,
            "home": home, "away": away, "home_src": home, "away_src": away,
            "kickoff": utc_date_str,
            "pH": round(float(pr["pH"]), 4), "pD": round(float(pr["pD"]), 4),
            "pA": round(float(pr["pA"]), 4),
            "pO25": round(float(pr["pO25"]), 4), "pU25": round(float(pr["pU25"]), 4),
            "lam": round(float(pr["lam"]), 3), "mu": round(float(pr["mu"]), 3),
            "rest_h": rh, "rest_a": ra,
            "model_version": "dc-v2",
            "created_at": firestore.SERVER_TIMESTAMP,
        }
        doc = db.collection("predictions").document(fixture_id)
        snap = doc.get()
        if not snap.exists:
            rec["gerekce"] = gerekce_uret(rec)
            doc.set(rec)
            n_extra_written += 1
        else:
            old = snap.to_dict()
            if abs(old.get("pH", 0) - rec["pH"]) > 0.03:
                doc.collection("updates").add(rec)

    print(f"Grup B (ekstra semasi): {n_extra_written} yeni tahmin, {n_extra_skipped} mac atlandi, {n_extra_atlandi} veri sorunu.")

    # --- 2b) capraz-lig turnuvalari (Sampiyonlar Ligi): lig-guc duzeltmeli tahmin
    # Orijinal liglar + yeni liglar
    all_models_for_cl = {**{code: models[code] for code in set(LEAGUES.values()) if code in models},
                         **{code: models[code] for code in set(LEAGUES_MAIN_SCHEMA.keys()) | set(LEAGUES_EXTRA_SCHEMA.keys()) if code in models}}

    def find_team(src_name):
        """Takimi tum lig modellerinde arar -> (lig_kodu, fd_adi) veya None."""
        for code_, m_ in all_models_for_cl.items():
            t_ = match_team(src_name, set(m_.teams))
            if t_:
                return code_, t_
        return None

    n_cl = n_cl_skip = 0
    for comp in CROSS_COMPS:
        data = fdo_get(f"/competitions/{comp}/matches", {
            "dateFrom": str(today.date()),
            "dateTo": str((today + pd.Timedelta(days=HORIZON_DAYS)).date())},
            fdo_key)
        for fx in data.get("matches", []):
            if fx.get("status") in ("FINISHED", "POSTPONED", "CANCELLED"):
                continue
            fid = fx["id"]
            h_src = fx["homeTeam"].get("shortName") or fx["homeTeam"]["name"]
            a_src = fx["awayTeam"].get("shortName") or fx["awayTeam"]["name"]
            fh, fa = find_team(h_src), find_team(a_src)
            if not fh or not fa:
                print(f"{comp}: Eslesmedi, atlandi: {h_src} / {a_src}")
                n_cl_skip += 1
                continue
            (div_h, h), (div_a, a) = fh, fa
            sdiff = LEAGUE_STRENGTH.get(div_h, -0.2) - LEAGUE_STRENGTH.get(div_a, -0.2)
            rh = (today - rest_info[div_h].get(h, today - pd.Timedelta(days=7))).days
            ra = (today - rest_info[div_a].get(a, today - pd.Timedelta(days=7))).days
            pr = predict_cross_league(all_models_for_cl[div_h], h, all_models_for_cl[div_a], a,
                                      strength_diff=sdiff, rest_h=rh, rest_a=ra)
            if pr is None:
                n_cl_skip += 1
                continue
            rec = {
                "fixture_id": fid, "league_fd": f"{div_h}-{div_a}",
                "league_comp": comp, "competition": comp,
                "home": h, "away": a, "home_src": h_src, "away_src": a_src,
                "kickoff": fx["utcDate"],
                "pH": round(float(pr["pH"]), 4), "pD": round(float(pr["pD"]), 4),
                "pA": round(float(pr["pA"]), 4),
                "pO25": round(float(pr["pO25"]), 4),
                "pU25": round(float(pr["pU25"]), 4),
                "lam": round(float(pr["lam"]), 3), "mu": round(float(pr["mu"]), 3),
                "rest_h": rh, "rest_a": ra,
                "strength_diff": round(float(sdiff), 3),
                "model_version": "dc-v2-cross",
                "created_at": firestore.SERVER_TIMESTAMP,
            }
            doc = db.collection("predictions").document(str(fid))
            snap = doc.get()
            if not snap.exists:
                rec["gerekce"] = gerekce_uret(rec)
                doc.set(rec)
                n_cl += 1
            else:
                old = snap.to_dict()
                if abs(old.get("pH", 0) - rec["pH"]) > 0.03:
                    doc.collection("updates").add(rec)
    print(f"Turnuva (capraz-lig): {n_cl} yeni tahmin, {n_cl_skip} mac atlandi.")

    # --- 3) biten maclarin sonuclarini isle (seffaf gecmis paneli)
    n_results = 0

    # Yeni liglerin sonuclari, bolum 1'de zaten indirilen yerel CSV'lerden okunur.
    # Lig basina bir kez okunur ve bellekte tutulur (tekrar indirme yok).
    _sonuc_cache = {}

    def yeni_lig_sonuc_df(code):
        if code in _sonuc_cache:
            return _sonuc_cache[code]
        df = None
        try:
            if code in LEAGUES_MAIN_SCHEMA:
                s = season_codes(today)[-1]  # guncel sezon
                p = os.path.join("fd_data", f"{code}_{s}.csv")
                if os.path.exists(p):
                    df = pd.read_csv(p, encoding="utf-8-sig", on_bad_lines="skip")
                    df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
            elif code in LEAGUES_EXTRA_SCHEMA:
                p = os.path.join("fd_data", f"{code}_extra.csv")
                if os.path.exists(p):
                    df = pd.read_csv(p, encoding="utf-8-sig", on_bad_lines="skip")
                    df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
                    df = adapt_extra_schema(df)
            if df is not None:
                if {"HomeTeam", "AwayTeam", "FTHG", "FTAG", "Date"} <= set(df.columns):
                    if not pd.api.types.is_datetime64_any_dtype(df["Date"]):
                        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True,
                                                    format="mixed", errors="coerce")
                    df = df.dropna(subset=["FTHG", "FTAG", "Date"])
                else:
                    df = None
        except Exception as e:
            print(f"UYARI: {code} sonuc dosyasi okunamadi: {e}")
            df = None
        _sonuc_cache[code] = df
        return df

    # son 10 gunun tahminlerini tara, sonucu islenmemis bitmis maclari guncelle
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    for snap in db.collection("predictions").where("kickoff", ">=", cutoff).stream():
        d = snap.to_dict()
        if d.get("result") is not None:
            continue
        if pd.Timestamp(d["kickoff"]).tz_convert("UTC") > pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=3):
            continue  # henuz bitmemis olabilir

        fixture_id = d.get("fixture_id")

        # fdcuk- ile baslayan kayitlar (yeni ligler): sonuc yerel CSV'den bulunur
        if fixture_id and str(fixture_id).startswith("fdcuk-"):
            # ID bicimi: fdcuk-{lig}-{YYYYMMDD}-{ev}-{deplasman}
            parts = str(fixture_id).split("-")
            if len(parts) >= 3:
                code, date_ymd = parts[1], parts[2]
                df = yeni_lig_sonuc_df(code)
                if df is not None:
                    try:
                        mac_tarihi = pd.Timestamp(date_ymd)
                        # Ayni sezonda iki kez karsilasan takimlari karistirmamak
                        # icin tarih de eslesmek zorunda
                        eslesen = df[(df["HomeTeam"] == d.get("home")) &
                                     (df["AwayTeam"] == d.get("away")) &
                                     (df["Date"] == mac_tarihi)]
                        if not eslesen.empty:
                            m = eslesen.iloc[0]
                            snap.reference.update({
                                "result": {"FTHG": int(m["FTHG"]), "FTAG": int(m["FTAG"])},
                                "result_processed_at": firestore.SERVER_TIMESTAMP})
                            n_results += 1
                    except Exception as e:
                        print(f"UYARI: {fixture_id} sonuc eslesmesi basarisiz: {e}")
        else:
            # Orijinal fixture'lar (football-data.org)
            fx = fdo_get(f"/matches/{fixture_id}", {}, fdo_key)
            fx = fx.get("match", fx)  # v4 bazen dogrudan, bazen sarmalanmis doner
            if fx and fx.get("status") == "FINISHED":
                g = fx["score"]["fullTime"]
                snap.reference.update({
                    "result": {"FTHG": g["home"], "FTAG": g["away"]},
                    "result_processed_at": firestore.SERVER_TIMESTAMP})
                n_results += 1
    print(f"{n_results} mac sonucu islendi.")


if __name__ == "__main__":
    main()

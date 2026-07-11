# -*- coding: utf-8 -*-
"""
STRATIX - Gunluk Analiz Isi (Asama 1)
=====================================
Her sabah GitHub Actions uzerinde calisir:
  1. football-data.co.uk'den guncel tarihi veriyi indirir, modeli egitir
  2. football-data.org'dan onumuzdeki 7 gunun fikstürünü ceker
     (8 lig + Sampiyonlar Ligi)
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

# Capraz-lig turnuvalari (takimlar farkli liglerden gelir)
CROSS_COMPS = ["CL"]                # Sampiyonlar Ligi (ucretsiz katmanda)

# Lig guc duzeltmesi (log-gol olcegi; referans: Premier League = 0).
# Kaba baslangic degerleri; CL sonuclari biriktikce elle guncellenebilir.
LEAGUE_STRENGTH = {
    "E0": 0.00, "SP1": -0.05, "D1": -0.08, "I1": -0.08,
    "F1": -0.15, "P1": -0.22, "N1": -0.25, "E1": -0.55,
}
FD_BASE = "https://www.football-data.co.uk/mmz4281"
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
    print(f"{len(paths)} tarihi veri dosyasi indirildi.")
    return paths


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
    for code in set(LEAGUES.values()):
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

    # --- 2b) capraz-lig turnuvalari (Sampiyonlar Ligi): lig-guc duzeltmeli tahmin
    def find_team(src_name):
        """Takimi tum lig modellerinde arar -> (lig_kodu, fd_adi) veya None."""
        for code_, m_ in models.items():
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
            pr = predict_cross_league(models[div_h], h, models[div_a], a,
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
    # son 10 gunun tahminlerini tara, sonucu islenmemis bitmis maclari guncelle
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    for snap in db.collection("predictions").where("kickoff", ">=", cutoff).stream():
        d = snap.to_dict()
        if d.get("result") is not None:
            continue
        if pd.Timestamp(d["kickoff"]).tz_convert("UTC") > pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=3):
            continue  # henuz bitmemis olabilir
        fx = fdo_get(f"/matches/{d['fixture_id']}", {}, fdo_key)
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

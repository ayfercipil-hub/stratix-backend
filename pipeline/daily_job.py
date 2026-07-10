# -*- coding: utf-8 -*-
"""
STRATIX - Gunluk Analiz Isi (Asama 1)
=====================================
Her sabah GitHub Actions uzerinde calisir:
  1. football-data.co.uk'den guncel tarihi veriyi indirir, modeli egitir
  2. API-Football'dan onumuzdeki 7 gunun fikstürünü ceker
  3. Her mac icin olasiliklari hesaplar (Dixon-Coles v2)
  4. (Varsa) Gemini API ile 3 maddelik gerekce metni uretir
  5. Sonuclari Firestore'a yazar (DEGISTIRILEMEZ tahmin gunlugu mantigiyla:
     ayni mac icin kayit varsa uzerine yazilmaz, 'guncelleme' ayri belge olur)
  6. Biten maclarin sonuclarini isler (seffaf gecmis paneli icin)

Gerekli ortam degiskenleri (GitHub Secrets):
  FIREBASE_SERVICE_ACCOUNT : Firebase servis hesabi JSON iceriginin tamami
  APIFOOTBALL_KEY          : api-football.com API anahtari
  GEMINI_API_KEY           : (istege bagli) Google AI Studio anahtari
"""
import json
import os
import sys
import difflib
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd

from stratix_core import load_matches, DixonColes, EDGE_THRESHOLD

# ------------------------------------------------------------------ ayarlar
SEASONS_BACK = 4                    # model icin kac sezon geriye gidilecek
LEAGUES = {                         # API-Football lig ID -> football-data kodu
    39: "E0",    # Premier League
    78: "D1",    # Bundesliga
    140: "SP1",  # La Liga
    135: "I1",   # Serie A
    61: "F1",    # Ligue 1
}
FD_BASE = "https://www.football-data.co.uk/mmz4281"
AF_BASE = "https://v3.football.api-sports.io"
HORIZON_DAYS = 7

# API-Football takim adi -> football-data takim adi (kismi harita;
# eslesmeyenler difflib ile denenir, o da olmazsa mac atlanir ve loglanir)
TEAM_MAP = {
    "Manchester United": "Man United", "Manchester City": "Man City",
    "Newcastle": "Newcastle", "Nottingham Forest": "Nott'm Forest",
    "Wolverhampton Wanderers": "Wolves", "Tottenham": "Tottenham",
    "Sheffield Utd": "Sheffield United", "Leeds": "Leeds",
    "Bayern München": "Bayern Munich", "Bayern Munich": "Bayern Munich",
    "Borussia Dortmund": "Dortmund", "Bayer Leverkusen": "Leverkusen",
    "Borussia Mönchengladbach": "M'gladbach", "Eintracht Frankfurt": "Ein Frankfurt",
    "FSV Mainz 05": "Mainz", "VfB Stuttgart": "Stuttgart",
    "SC Freiburg": "Freiburg", "TSG Hoffenheim": "Hoffenheim",
    "1. FC Köln": "FC Koln", "FC St. Pauli": "St Pauli",
    "Atletico Madrid": "Ath Madrid", "Athletic Club": "Ath Bilbao",
    "Real Sociedad": "Sociedad", "Real Betis": "Betis",
    "Celta Vigo": "Celta", "Rayo Vallecano": "Vallecano",
    "AC Milan": "Milan", "Inter": "Inter", "AS Roma": "Roma",
    "Hellas Verona": "Verona", "Paris Saint Germain": "Paris SG",
    "Marseille": "Marseille", "Saint Etienne": "St Etienne",
    "Stade Brestois 29": "Brest", "LOSC": "Lille",
}


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


def af_get(path, params, key):
    r = requests.get(f"{AF_BASE}{path}", params=params,
                     headers={"x-apisports-key": key}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        print("API-Football hata:", data["errors"])
    return data.get("response", [])


def match_team(af_name, fd_teams):
    """API-Football adini football-data adina esler."""
    if af_name in TEAM_MAP and TEAM_MAP[af_name] in fd_teams:
        return TEAM_MAP[af_name]
    if af_name in fd_teams:
        return af_name
    close = difflib.get_close_matches(af_name, list(fd_teams), n=1, cutoff=0.75)
    return close[0] if close else None


GEREKCE_PROMPT = """Sen STRATIX adli futbol istatistik uygulamasinin analiz yazarisin.
Sana verilen SAYILARI ASLA degistirme, kendi olasilik uretme, bahis tesvik etme.
Gorevin: asagidaki model ciktisini kullanicilar icin 3 maddelik kisa, notr ve
dürüst bir gerekceye cevirmek. Her madde tek cumle olsun. Turkce yaz.
Kumar tesviki yapma; 'kesin', 'garanti' gibi kelimeler kullanma.

Mac: {home} - {away} ({lig})
Model ciktisi: Ev kazanma %{ph:.0f}, Beraberlik %{pd:.0f}, Deplasman %{pa:.0f},
2.5 Ust %{po:.0f}. Beklenen goller: {lam:.2f} - {mu:.2f}.
Ev sahibi dinlenme: {rest_h:.0f} gun, deplasman: {rest_a:.0f} gun."""


def gerekce_uret(rec, api_key):
    """Gemini ile 3 maddelik gerekce. Hata olursa bos dondurur (is durmaz)."""
    if not api_key:
        return ""
    prompt = GEREKCE_PROMPT.format(
        home=rec["home"], away=rec["away"], lig=rec["league_fd"],
        ph=rec["pH"] * 100, pd=rec["pD"] * 100, pa=rec["pA"] * 100,
        po=rec["pO25"] * 100, lam=rec["lam"], mu=rec["mu"],
        rest_h=rec.get("rest_h", 7), rest_a=rec.get("rest_a", 7))
    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.0-flash:generateContent",
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print("Gerekce uretilemedi:", e)
        return ""


def main():
    af_key = os.environ.get("APIFOOTBALL_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not af_key or not sa_json:
        sys.exit("HATA: APIFOOTBALL_KEY ve FIREBASE_SERVICE_ACCOUNT zorunlu.")

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
    season_year = today.year if today.month >= 8 else today.year - 1
    n_written = n_skipped = 0
    for lid, code in LEAGUES.items():
        if code not in models:
            continue
        fixtures = af_get("/fixtures", {
            "league": lid, "season": season_year,
            "from": str(today.date()),
            "to": str((today + pd.Timedelta(days=HORIZON_DAYS)).date())}, af_key)
        fd_teams = set(models[code].teams)
        for fx in fixtures:
            fid = fx["fixture"]["id"]
            h_af = fx["teams"]["home"]["name"]
            a_af = fx["teams"]["away"]["name"]
            h = match_team(h_af, fd_teams)
            a = match_team(a_af, fd_teams)
            if not h or not a:
                print(f"Eslesmedi, atlandi: {h_af} / {a_af}")
                n_skipped += 1
                continue
            kickoff = fx["fixture"]["date"]
            rh = (today - rest_info[code].get(h, today - pd.Timedelta(days=7))).days
            ra = (today - rest_info[code].get(a, today - pd.Timedelta(days=7))).days
            pr = models[code].predict(h, a, rest_h=rh, rest_a=ra)
            if pr is None:
                n_skipped += 1
                continue
            rec = {
                "fixture_id": fid, "league_fd": code, "league_af": lid,
                "home": h, "away": a, "home_af": h_af, "away_af": a_af,
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
                rec["gerekce"] = gerekce_uret(rec, gemini_key)
                doc.set(rec)
                n_written += 1
            else:
                old = snap.to_dict()
                if abs(old.get("pH", 0) - rec["pH"]) > 0.03:
                    doc.collection("updates").add(rec)
    print(f"{n_written} yeni tahmin yazildi, {n_skipped} mac atlandi.")

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
        fx = af_get("/fixtures", {"id": d["fixture_id"]}, af_key)
        if fx and fx[0]["fixture"]["status"]["short"] == "FT":
            g = fx[0]["goals"]
            snap.reference.update({
                "result": {"FTHG": g["home"], "FTAG": g["away"]},
                "result_processed_at": firestore.SERVER_TIMESTAMP})
            n_results += 1
    print(f"{n_results} mac sonucu islendi.")


if __name__ == "__main__":
    main()

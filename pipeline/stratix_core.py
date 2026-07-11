# -*- coding: utf-8 -*-
"""
STRATIX - Asama 0 Cekirdegi
============================
Dixon-Coles gol modeli + kalibrasyon + kapanis oranlarina karsi value backtest.

Veri formati: football-data.co.uk CSV semasi
Gerekli kolonlar: Date, HomeTeam, AwayTeam, FTHG, FTAG
Oran kolonlari (varsa): PSCH/PSCD/PSCA (Pinnacle kapanis), B365CH/B365CD/B365CA,
                        B365H/B365D/B365A, PC>2.5 / PC<2.5, B365C>2.5 / B365C<2.5
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

MAX_GOALS = 10          # skor izgarasinin ust siniri
XI = 0.0019             # zaman sonumleme katsayisi (gun basina)
MIN_MATCHES = 6         # bir takim icin tahmin uretmeden once gereken min mac
EDGE_THRESHOLD = 0.05   # value esigi: model_olasiligi * oran > 1 + esik
REFIT_DAYS = 28         # backtest sirasinda modelin yeniden egitilme araligi


# ---------------------------------------------------------------- veri yukleme
def load_matches(csv_paths):
    """football-data.co.uk CSV'lerini tek DataFrame'de birlestirir."""
    frames = []
    for p in csv_paths:
        df = None
        for enc in ("utf-8-sig", "latin-1"):
            try:
                df = pd.read_csv(p, encoding=enc, on_bad_lines="skip")
                break
            except Exception:
                continue
        if df is None:
            continue
        # BOM / bosluk kalintilarini kolon adlarindan temizle
        df.columns = [str(c).replace("\ufeff", "").replace("ï»¿", "").strip()
                      for c in df.columns]
        if not {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"} <= set(df.columns):
            continue
        df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed",
                                    errors="coerce")
        df = df.dropna(subset=["Date"])
        if "Div" not in df.columns or df["Div"].isna().all():
            # son care: dosya adindan lig kodunu cikar (orn. data/E0_2526.csv -> E0)
            import re, os
            m = re.match(r"([A-Z]+\d*)", os.path.basename(str(p)))
            df["Div"] = m.group(1) if m else str(p)
        frames.append(df)
    out = pd.concat(frames, ignore_index=True).sort_values("Date")
    out[["FTHG", "FTAG"]] = out[["FTHG", "FTAG"]].astype(int)
    out = out.reset_index(drop=True)
    # dinlenme gunu ozelligi: takimin bir onceki macindan bu yana gecen gun
    out["RestH"] = 7.0
    out["RestA"] = 7.0
    last = {}
    for idx in out.index:
        d = out.at[idx, "Date"]
        ht, at_ = out.at[idx, "HomeTeam"], out.at[idx, "AwayTeam"]
        if ht in last:
            out.at[idx, "RestH"] = min((d - last[ht]).days, 14)
        if at_ in last:
            out.at[idx, "RestA"] = min((d - last[at_]).days, 14)
        last[ht] = d
        last[at_] = d
    return out


def pick_odds(row, names):
    """Verilen oncelik sirasina gore ilk gecerli orani dondurur."""
    for n in names:
        if n in row.index:
            v = row[n]
            if pd.notna(v) and v > 1.0:
                return float(v)
    return None


ODDS_1X2 = {
    "H": ["PSCH", "B365CH", "PSH", "B365H", "AvgH"],
    "D": ["PSCD", "B365CD", "PSD", "B365D", "AvgD"],
    "A": ["PSCA", "B365CA", "PSA", "B365A", "AvgA"],
}
ODDS_OU = {
    "O": ["PC>2.5", "B365C>2.5", "P>2.5", "B365>2.5", "Avg>2.5"],
    "U": ["PC<2.5", "B365C<2.5", "P<2.5", "B365<2.5", "Avg<2.5"],
}


# ---------------------------------------------------------------- Dixon-Coles
def _tau(hg, ag, lam, mu, rho):
    """Dixon-Coles dusuk skor duzeltmesi."""
    if hg == 0 and ag == 0:
        return 1 - lam * mu * rho
    if hg == 0 and ag == 1:
        return 1 + lam * rho
    if hg == 1 and ag == 0:
        return 1 + mu * rho
    if hg == 1 and ag == 1:
        return 1 - rho
    return 1.0


class DixonColes:
    """Zaman-agirlikli Dixon-Coles modeli (lig basina ayri egitilir).

    v2 eklentileri:
      - ridge (shrinkage): az macli takimlarin guc parametrelerini lig
        ortalamasina ceker; uc oranlardaki asiri ozguveni bastirir.
      - dinlenme gunu: iki takimin mac arasi dinlenme farki carpan olarak modele girer.
    """

    def __init__(self, xi=XI, ridge=6.0):
        self.xi = xi
        self.ridge = ridge
        self.teams = None
        self.params = None  # [atak..., defans..., ev_avantaji, rho, beta_dinlenme]

    def fit(self, df, as_of_date):
        d = df[df["Date"] < as_of_date]
        if len(d) < 100:
            return False
        self.teams = sorted(set(d["HomeTeam"]) | set(d["AwayTeam"]))
        idx = {t: i for i, t in enumerate(self.teams)}
        n = len(self.teams)
        h = d["HomeTeam"].map(idx).values
        a = d["AwayTeam"].map(idx).values
        hg = d["FTHG"].values
        ag = d["FTAG"].values
        days = (as_of_date - d["Date"]).dt.days.values
        w = np.exp(-self.xi * days)
        rest_delta = ((d["RestH"].values - d["RestA"].values) / 7.0
                      if "RestH" in d.columns else np.zeros(len(d)))
        ridge = self.ridge

        def nll(p):
            atk = p[:n] - p[:n].mean()          # kimlik kisiti
            dfn = p[n:2 * n]
            home, rho, brest = p[-3], p[-2], p[-1]
            lam = np.exp(atk[h] + dfn[a] + home + brest * rest_delta)
            mu = np.exp(atk[a] + dfn[h] - brest * rest_delta)
            ll = (poisson.logpmf(hg, lam) + poisson.logpmf(ag, mu))
            # dusuk skor duzeltmesi (vektorel)
            tau = np.ones_like(lam)
            m00 = (hg == 0) & (ag == 0)
            m01 = (hg == 0) & (ag == 1)
            m10 = (hg == 1) & (ag == 0)
            m11 = (hg == 1) & (ag == 1)
            tau[m00] = 1 - lam[m00] * mu[m00] * rho
            tau[m01] = 1 + lam[m01] * rho
            tau[m10] = 1 + mu[m10] * rho
            tau[m11] = 1 - rho
            tau = np.clip(tau, 1e-10, None)
            penalty = ridge * (np.sum(atk ** 2) + np.sum(dfn ** 2))
            return -np.sum(w * (ll + np.log(tau))) + penalty

        x0 = np.concatenate([np.zeros(n), np.zeros(n), [0.25, -0.05, 0.0]])
        bounds = [(-3, 3)] * (2 * n) + [(-1, 1), (-0.2, 0.2), (-0.3, 0.3)]
        res = minimize(nll, x0, method="L-BFGS-B", bounds=bounds,
                       options={"maxiter": 300})
        self.params = res.x
        self._idx = idx
        self._counts = pd.concat([d["HomeTeam"], d["AwayTeam"]]).value_counts()
        return res.success or res.fun < np.inf

    def predict(self, home, away, rest_h=7.0, rest_a=7.0):
        """Skor izgarasindan pazar olasiliklarini dondurur (veya None)."""
        if self.params is None or home not in self._idx or away not in self._idx:
            return None
        if (self._counts.get(home, 0) < MIN_MATCHES
                or self._counts.get(away, 0) < MIN_MATCHES):
            return None
        n = len(self.teams)
        atk = self.params[:n] - self.params[:n].mean()
        dfn = self.params[n:2 * n]
        home_adv, rho, brest = self.params[-3], self.params[-2], self.params[-1]
        i, j = self._idx[home], self._idx[away]
        delta = (min(rest_h, 14) - min(rest_a, 14)) / 7.0
        lam = np.exp(atk[i] + dfn[j] + home_adv + brest * delta)
        mu = np.exp(atk[j] + dfn[i] - brest * delta)
        g = np.arange(MAX_GOALS + 1)
        ph = poisson.pmf(g, lam)
        pa = poisson.pmf(g, mu)
        grid = np.outer(ph, pa)
        for hg_ in (0, 1):
            for ag_ in (0, 1):
                grid[hg_, ag_] *= _tau(hg_, ag_, lam, mu, rho)
        grid /= grid.sum()
        p_home = np.tril(grid, -1).sum()
        p_draw = np.trace(grid)
        p_away = np.triu(grid, 1).sum()
        totals = np.add.outer(g, g)
        p_over = grid[totals >= 3].sum()
        return {"pH": p_home, "pD": p_draw, "pA": p_away,
                "pO25": p_over, "pU25": 1 - p_over,
                "lam": lam, "mu": mu}


# ------------------------------------------------- capraz-lig (turnuva) tahmini
def predict_cross_league(model_h, home, model_a, away, strength_diff=0.0,
                         rest_h=7.0, rest_a=7.0):
    """Iki FARKLI lig modelinden turnuva maci tahmini (orn. Sampiyonlar Ligi).

    Mantik: her takimin atak/defans gucu kendi lig modelinden alinir; ligler
    arasindaki seviye farki 'strength_diff' (ev sahibinin ligi - deplasmanin
    ligi, log-gol olceginde) ile duzeltilir. Defans parametrelerinin lig ici
    ortalamasi (gol taban seviyesi) once cikarilir, iki ligin ortalamasi
    ortak taban olarak geri eklenir; boylece farkli liglerin gol ortalamalari
    karismaz. Ev avantaji iki ligin ortalamasidir.
    """
    for m, t in ((model_h, home), (model_a, away)):
        if m.params is None or t not in m._idx:
            return None
        if m._counts.get(t, 0) < MIN_MATCHES:
            return None
    nh, na = len(model_h.teams), len(model_a.teams)
    atk_h = model_h.params[:nh] - model_h.params[:nh].mean()
    dfn_h = model_h.params[nh:2 * nh]
    atk_a = model_a.params[:na] - model_a.params[:na].mean()
    dfn_a = model_a.params[na:2 * na]
    base_h, base_a = dfn_h.mean(), dfn_a.mean()
    base = 0.5 * (base_h + base_a)
    home_adv = 0.5 * (model_h.params[-3] + model_a.params[-3])
    rho = 0.5 * (model_h.params[-2] + model_a.params[-2])
    brest = 0.5 * (model_h.params[-1] + model_a.params[-1])
    i, j = model_h._idx[home], model_a._idx[away]
    delta = (min(rest_h, 14) - min(rest_a, 14)) / 7.0
    lam = np.exp(atk_h[i] + (dfn_a[j] - base_a) + base + home_adv
                 + strength_diff + brest * delta)
    mu = np.exp(atk_a[j] + (dfn_h[i] - base_h) + base
                - strength_diff - brest * delta)
    g = np.arange(MAX_GOALS + 1)
    grid = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    for hg_ in (0, 1):
        for ag_ in (0, 1):
            grid[hg_, ag_] *= _tau(hg_, ag_, lam, mu, rho)
    grid /= grid.sum()
    totals = np.add.outer(g, g)
    p_over = grid[totals >= 3].sum()
    return {"pH": np.tril(grid, -1).sum(), "pD": np.trace(grid),
            "pA": np.triu(grid, 1).sum(),
            "pO25": p_over, "pU25": 1 - p_over,
            "lam": lam, "mu": mu}


# ---------------------------------------------------------------- backtest
def run_backtest(df, test_start, edge=EDGE_THRESHOLD, refit_days=REFIT_DAYS,
                 max_odds=8.0):
    """
    test_start tarihinden itibaren tum maclar icin:
      - yuruyen yeniden egitim (refit_days araligi, lig basina)
      - tahmin gunlugu (degistirilemez kayit mantigi)
      - 1X2 ve U/A 2.5 icin value bahis simulasyonu (duz 1 birim)
    """
    test_start = pd.Timestamp(test_start)
    logs = []
    for div, dd in df.groupby("Div"):
        dd = dd.sort_values("Date").reset_index(drop=True)
        test = dd[dd["Date"] >= test_start]
        if test.empty:
            continue
        model = DixonColes()
        last_fit = None
        for _, row in test.iterrows():
            date = row["Date"]
            if last_fit is None or (date - last_fit).days >= refit_days:
                ok = model.fit(dd, as_of_date=date)
                last_fit = date if ok else last_fit
            pr = model.predict(row["HomeTeam"], row["AwayTeam"],
                               rest_h=row.get("RestH", 7.0),
                               rest_a=row.get("RestA", 7.0))
            if pr is None:
                continue
            rec = {"Div": div, "Date": date,
                   "Home": row["HomeTeam"], "Away": row["AwayTeam"],
                   "FTHG": row["FTHG"], "FTAG": row["FTAG"], **pr}
            for k, names in ODDS_1X2.items():
                rec[f"odds_{k}"] = pick_odds(row, names)
            for k, names in ODDS_OU.items():
                rec[f"odds_{k}"] = pick_odds(row, names)
            logs.append(rec)
    log = pd.DataFrame(logs)
    if log.empty:
        return log, {}

    # gercek sonuclar
    log["res_1x2"] = np.where(log.FTHG > log.FTAG, "H",
                     np.where(log.FTHG < log.FTAG, "A", "D"))
    log["res_o25"] = (log.FTHG + log.FTAG >= 3)

    # ---- kalibrasyon (Brier)
    briers = {}
    for k, col in [("H", "pH"), ("D", "pD"), ("A", "pA")]:
        y = (log["res_1x2"] == k).astype(float)
        briers[f"Brier_1X2_{k}"] = float(((log[col] - y) ** 2).mean())
    y = log["res_o25"].astype(float)
    briers["Brier_O25"] = float(((log["pO25"] - y) ** 2).mean())

    # ---- value bahis simulasyonu
    bets = []
    for _, r in log.iterrows():
        for k, pcol in [("H", "pH"), ("D", "pD"), ("A", "pA")]:
            o = r[f"odds_{k}"]
            if o and o <= max_odds and r[pcol] * o > 1 + edge:
                win = (r["res_1x2"] == k)
                bets.append({"market": "1X2", "sel": k, "odds": o,
                             "p": r[pcol], "pnl": (o - 1) if win else -1,
                             "Date": r["Date"], "Div": r["Div"]})
        for k, pcol, cond in [("O", "pO25", r["res_o25"]),
                              ("U", "pU25", not r["res_o25"])]:
            o = r[f"odds_{k}"]
            if o and o <= max_odds and r[pcol] * o > 1 + edge:
                bets.append({"market": "OU2.5", "sel": k, "odds": o,
                             "p": r[pcol], "pnl": (o - 1) if cond else -1,
                             "Date": r["Date"], "Div": r["Div"]})
    bets = pd.DataFrame(bets)
    summary = {"n_matches": int(len(log)), **briers}
    if not bets.empty:
        for m, bb in bets.groupby("market"):
            summary[f"{m}_n_bets"] = int(len(bb))
            summary[f"{m}_roi"] = float(bb["pnl"].mean())
            summary[f"{m}_hit"] = float((bb["pnl"] > 0).mean())
        summary["total_pnl"] = float(bets["pnl"].sum())
    return log, {"summary": summary, "bets": bets}


def calibration_table(log, col="pH", outcome=None, bins=10):
    """Kalibrasyon tablosu: model yuzdesi vs gercek siklik."""
    if outcome is None:
        outcome = (log["res_1x2"] == "H").astype(float)
    q = pd.cut(log[col], np.linspace(0, 1, bins + 1))
    tab = pd.DataFrame({"bin": q, "p": log[col], "y": outcome})
    g = tab.groupby("bin", observed=True).agg(
        model_ort=("p", "mean"), gercek=("y", "mean"), n=("y", "size"))
    return g[g["n"] >= 5].round(3)

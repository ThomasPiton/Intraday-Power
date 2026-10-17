# Intraday Power — Plan du projet

> App weekend pour la candidature **TotalEnergies – Short-term Power Intraday Trader** (Flexible Power Solutions).
> Hébergée sur GitHub Pages, mise à jour automatiquement par GitHub Actions, 100 % APIs gratuites sans inscription.

---

## 1. Pitch (30 secondes)

**Intraday Power** est un terminal de trading intraday électricité, style Bloomberg, qui tourne en continu :

1. **Forecast Revisions** : il mesure, pour FR / DE-LU / BE / NL / DK1, l'écart entre les prévisions éolien + solaire *intraday* et *day-ahead*. C'est le moteur principal des prix intraday.
2. **Bots** : deux bots papier (*Rule* contre *ML*) tradent la Belgique en **day-ahead contre prix de déséquilibre**, avec un P&L qui s'accumule en live depuis la date de lancement.
3. **Battery League** : chaque jour, il calcule le revenu maximal d'une batterie 1 MW / 2 MWh sur chaque zone et publie un classement.

Ce que ça prouve au recruteur : analyse quantitative, outils d'aide à la décision, conception de stratégie, **suivi de la performance et du P&L de robots**, compréhension des actifs flexibles. Ce sont les lignes de la fiche de poste.

---

## 2. Architecture

```
          ┌──────────────── GitHub Actions (cron */15) ────────────────┐
APIs ──►  │ fetch (incrémental) → data/raw/*.parquet                   │
          │ features → bots (decide / settle) → battery LP → export    │
          │ → docs/data/*.json   →  git commit & push                  │
          └────────────────────────────────────────────────────────────┘
                                   │
                         GitHub Pages (dossier /docs)
                   HTML + JS vanilla + ECharts (aucun build)
```

- **Aucun serveur.** Python tourne seulement dans Actions. Le front lit des JSON statiques.
- **Pas de CORS ni de clé** côté navigateur.
- **Historique** : Parquet partitionné par mois dans `data/`. Le front ne reçoit que des JSON compacts : fenêtre récente plus agrégats.

---

## 3. Sources de données (gratuites, sans inscription)

| Source | Usage | Endpoints / jeux | Contraintes |
|---|---|---|---|
| **Energy-Charts** (Fraunhofer ISE) `api.energy-charts.info` | Prix DA FR, DE-LU, BE, NL, DK1 ; prévisions solaire/éolien en `day-ahead` et `intraday` ; réalisé ; échanges transfrontaliers | `/v2/price`, `/v2/public_power_forecast`, `/v2/public_power`, `/v2/cbet` | Environ 2 requêtes/min par endpoint, respecter `Retry-After`. Prix CC BY 4.0 pour ces 5 zones (source SMARD), attribution obligatoire |
| **Elia Open Data** `opendata.elia.be` (Opendatasoft API v2.1) | Belgique : prix de déséquilibre (1 min et 15 min), déséquilibre système, prévision Elia du SI, prévisions éolien/PV | Historique : `ods134` (prix QH), `ods133` (prix 1 min), `ods126` (SI). Near-real-time : à identifier via le catalogue | Historique depuis le 22/05/2024 (MARI). Les identifiants doivent être vérifiés via le catalogue |
| *(option)* **SMARD** | Fallback prix DE | `smard.de/app/chart_data/...` | — |

**Pas de données du continu EPEX** : c'est assumé et expliqué dans l'onglet *Methodology*. Les proxys utilisés sont les révisions de prévisions et le prix de déséquilibre.

---

## 4. Méthodologie

### 4.1 Forecast Revisions
- `revision_MW[zone, QH] = (wind + solar)_intraday − (wind + solar)_day-ahead`
- Une révision positive signifie plus de renouvelables que prévu : le système est long et la pression intraday est baissière. Une révision négative pousse à la hausse.
- Affichage : heatmap zone × quart d'heure (J, J−1), plus un détail par zone. Pour BE, validation a posteriori en montrant la corrélation entre révision et (prix de déséquilibre − DA).

### 4.2 Bots (Belgique uniquement)
- **Trade** : position `p ∈ {−1, 0, +1}` MW sur le quart d'heure Q.
- **P&L** : `PnL = p × (ImbalancePrice_Q − DA_Q) × 0.25 − 0.5 €/MWh × |p| × 0.25` (coût fictif).
- **Anti-lookahead (non négociable)** : la décision pour Q n'utilise que l'information publiée avant le début de Q.
  - Backtest : features avec un décalage d'au moins 2 quarts d'heure sur les données temps réel (SI, prix de déséquilibre). Le DA de Q et les prévisions disponibles avant Q sont autorisés.
  - Live : chaque run décide pour les quarts d'heure qui n'ont pas encore commencé et les écrit dans un **journal append-only** horodaté. Le règlement se fait plus tard, quand le prix de déséquilibre est publié. Une décision n'est jamais réécrite.
- **Bot Rule** : seuil sur le dernier SI connu et/ou la prévision Elia du SI. Système court au-delà du seuil : acheteur ; long au-delà du seuil : vendeur ; sinon à plat. Le seuil est calibré une seule fois sur la première période de backtest, puis figé.
- **Bot ML** : LightGBM qui prédit `sign(ImbalancePrice − DA)`. Mêmes variables d'entrée plus le calendrier et les révisions BE. **Walk-forward** avec ré-entraînement hebdomadaire. Position seulement si la probabilité dépasse 0,6.
- **Métriques** : P&L cumulé, P&L par jour, hit ratio, P&L moyen par trade, Sharpe (journalier, annualisé), max drawdown, taux d'exposition, performance par heure.
- **Backtest et live strictement séparés** : la date de lancement est un trait vertical sur les courbes, avec des statistiques séparées.

### 4.3 Battery League
- Batterie 1 MW / 2 MWh, rendement aller-retour 88 % (√0,88 à la charge et à la décharge), au plus 2 cycles par jour (décharge ≤ 4 MWh/j), SoC initial = SoC final = 50 %.
- Programmation linéaire (`scipy.optimize.linprog`, HiGHS) sur les 96 prix DA au quart d'heure de chaque zone.
- **Revenu théorique en prévision parfaite**, présenté comme tel.
- Classement J, 7 jours, 30 jours, cumul depuis le backfill.

---

## 5. Structure du repo

```
intraday-power/
├── .github/workflows/
│   ├── update.yml          # cron */15 : fetch → bots → export → commit
│   ├── daily.yml           # 1x/j ~13:30 CET : prix DA J+1, battery LP, retrain ML (lundi)
│   └── backfill.yml        # workflow_dispatch : backfill complet
├── src/intraday_power/
│   ├── config.py           # zones, paramètres, chemins
│   ├── sources/
│   │   ├── energy_charts.py
│   │   └── elia.py
│   ├── storage.py          # parquet partitionné, upsert idempotent
│   ├── features.py
│   ├── bots/
│   │   ├── base.py         # interface decide()/settle(), journal append-only
│   │   ├── rule_bot.py
│   │   └── ml_bot.py
│   ├── battery.py
│   ├── metrics.py
│   ├── export.py           # → docs/data/*.json
│   └── cli.py              # python -m intraday_power <fetch|decide|settle|battery|train|export|backfill>
├── data/                   # parquet (historique), models/
├── docs/                   # GitHub Pages
│   ├── index.html
│   ├── assets/{app.js, style.css}
│   └── data/*.json
├── tests/
├── requirements.txt
└── README.md
```

---

## 6. Écrans (thème sombre et dense, façon Bloomberg, en anglais)

1. **Desk** : bandeau ticker (prix DA du quart d'heure courant pour les 5 zones), cartes P&L du jour pour les deux bots, dernier SI et dernier prix de déséquilibre BE, alertes (prix négatifs, révision supérieure à X MW, spread extrême), horodatage « last update » visible.
2. **Forecast Revisions** : heatmap zone × quart d'heure, courbes DA/ID/actual par zone, nuage de points révision BE contre (imbalance − DA).
3. **Bots** : courbes de P&L Rule contre ML (séparation backtest/live), tableau de statistiques, P&L par heure, journal des 50 derniers trades.
4. **Battery League** : classement (J / 7 j / 30 j / total), profil de dispatch du jour pour la zone sélectionnée.
5. **Methodology** : sources et licences, formules, règle anti-lookahead, limites. Écrite pour être lue par un trader.

Police mono (JetBrains Mono ou IBM Plex Mono), fond quasi noir, accents ambre et vert/rouge, tableaux serrés, chiffres alignés à droite, utilisable sur mobile.

---

## 7. Planning du weekend

| Bloc | Contenu |
|---|---|
| Samedi soir | Repo, squelette, vérification des endpoints, fetchers et stockage |
| Dimanche matin | Backfill, features, bots et backtest, battery LP, export JSON |
| Dimanche après-midi | Front complet, workflows Actions, activation de Pages, README |
| Dimanche soir | Contrôle anti-lookahead, vérification du site en ligne, lancement du live |

---

## 8. Risques et parades

- **Débit Energy-Charts** : requêtes espacées, cache, prévisions rafraîchies chaque heure (pas toutes les 15 min), prix DA une fois par jour.
- **Identifiants Elia qui changent** : découverte via l'API catalogue et fixation dans `config.py`.
- **Cron Actions en retard** : sans conséquence, car les décisions horodatées ne portent que sur des quarts d'heure futurs.
- **Workflows planifiés désactivés après 60 jours sans activité sur le repo** : à surveiller, le README explique comment les réactiver.
- **Taille du repo** : Parquet compressé, JSON du front limités aux fenêtres récentes et aux agrégats.

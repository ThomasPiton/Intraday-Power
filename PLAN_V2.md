# Intraday Power — Plan V2

> Objectif v2 : passer d'un terminal de trading dense à une **app épurée, pédagogique et riche en données**, qui permet d'observer comment la **météo** pilote la **demande** et l'**offre** d'électricité, puis les **prix**, en France et en Allemagne. Les bots et la batterie de la v1 sont conservés.

---

## 1. Décisions prises

| Sujet | Décision |
|---|---|
| Périmètre pays | **France + Allemagne** (DE-LU). Autres pays uniquement quand ils sont la seule source d'une donnée (BE, GB, NO, DK) |
| Approche | **Observer, pas modéliser** : des vues descriptives, aucun modèle de prévision maison en v2 |
| Design | Épuré, inspiré de Trade Republic (style uniquement, aucun élément de marque) |
| Pédagogie | Chaque graphique a 3 blocs dépliables : *What am I looking at? / Why does a trader care? / How is it computed?* |
| Data Hub | Les 9 catégories, dont la carte « Data gaps » |
| v1 | Bots et Battery League conservés sur une page *Flexibility*. Le journal live et la date LIVE ne sont jamais réinitialisés |

**Choix par défaut, modifiables** : interface en anglais (cohérent avec la v1 et la candidature) ; thème clair par défaut avec bascule sombre ; brief du jour généré par règles (pas de LLM).

---

## 2. Pages

1. **Today** : brief de marché en phrases générées par règles (« FR peak today 19:00 at 142 €/MWh; DE had 6 negative quarter-hours driven by solar; next week 3 °C colder than normal in France »). Gros chiffres clés et mini-courbes.
2. **Weather → Demand**
   - Température contre consommation : nuage de points en V, une couleur par saison, pente de thermosensibilité affichée.
   - Consommation réalisée contre prévisions RTE J-1 et J, avec la température superposée.
   - Anomalie de température prévue à 7-14 jours contre la normale, avec un ordre de grandeur indicatif en GW (règle RTE d'environ 2,4 GW/°C l'hiver, clairement présentée comme une règle empirique).
3. **Weather → Supply**
   - Courbe de puissance éolienne observée (vent à 100 m contre production), FR et DE.
   - Rayonnement contre production solaire, plus une comparaison journée ensoleillée / journée nuageuse.
   - Prévisions vent et soleil sur 7 jours avec bande d'ensemble, à côté des prévisions officielles de production.
   - Carte météo FR+DE : vent et couverture nuageuse sur la grille de points.
4. **Price Drivers**
   - « A day in the market » : 4 graphiques empilés (météo, demande/renouvelables, charge résiduelle, prix) avec sélecteur de date et journées remarquables présélectionnées.
   - Charge résiduelle contre prix : le merit order visible.
   - Détecteurs : heures à prix négatifs et épisodes de *Dunkelflaute*.
5. **Data Hub** : liste de lignes (valeur, variation, mini-courbe, fraîcheur), fiche détaillée au toucher, filtres par catégorie, tableau des APIs en bas de page.
   1. Demand · 2. Renewables · 3. Nuclear & thermal · 4. Hydro · 5. Grid · 6. Balancing · 7. Prices · 8. Gas & carbon · 9. Data gaps
6. **Flexibility** : bots v1 (Rule contre ML, Belgique) et Battery League (FR, DE-LU, BE, NL, DK1), dans le nouveau design.
7. **Learn** : glossaire d'environ 40 termes, méthodologie, sources et licences.

---

## 3. Sources (gratuites, sans inscription)

| Source | Données utilisées en v2 |
|---|---|
| **Open-Meteo** (CC BY 4.0, attribution obligatoire, environ 10 000 appels/j gratuits en usage non commercial) | Prévisions 16 j, ensembles, archive des prévisions passées (depuis 2021), réanalyse ERA5 (depuis 1940) : température 2 m, vent 10 m et 100 m, rafales, rayonnement GHI/direct/diffus, couverture nuageuse |
| **RTE éCO2mix via ODRÉ** (quota 50 000 appels/mois) | Consommation FR au quart d'heure, prévisions J-1 et J, production par filière, CO2, échanges ; nucléaire horaire depuis 2001 ; parc par filière |
| **Energy-Charts** | Production et prévisions FR/DE, capacités installées, prix DA, flux transfrontaliers, fréquence réseau à la seconde |
| **SMARD** | Données allemandes (charge, production, prévisions) en complément |
| **Elia** | Déséquilibre BE (bots v1 et Data Hub) |
| **Elexon Insights** | Demande GB, prix de déséquilibre, Market Index, REMIT (indisponibilités), disponibilité future par filière |
| **Energinet** | Déséquilibre DK, CO2 |
| **NVE** | Remplissage des réservoirs norvégiens (hebdomadaire), absence de clé à confirmer |
| **ENTSOG** | Flux de gaz physiques et nominations |
| **Elering, REE** | Prix Nordiques/Baltes et Espagne pour la ligne Prices du Data Hub |

**Data gaps affichés** : continu intraday EPEX, TTF, EUA/CO2, indisponibilités nucléaires FR unité par unité, ENTSO-E (token requis).

---

## 4. Points météo

- **Température** : 10 villes par pays, pondérées par la population (FR : Paris, Lyon, Marseille, Toulouse, Lille, Bordeaux, Nantes, Strasbourg, Nice, Rennes ; DE : Berlin, Hamburg, München, Köln, Frankfurt, Stuttgart, Düsseldorf, Leipzig, Dortmund, Bremen).
- **Vent** : une grille d'environ 15 points par pays, centrée sur les zones de parcs (nord de l'Allemagne, mer du Nord et Baltique pour l'offshore ; Hauts-de-France, Grand Est, Occitanie). La moyenne affichée est simple et documentée, sans pondération apprise.
- **Solaire** : une grille d'environ 10 points par pays (sud de l'Allemagne, sud de la France…).

---

## 5. Design

- **Clair** : fond #FFFFFF, texte #0A0A0A, gris secondaire #8A8A8E, séparateurs #EDEDED. **Sombre** : fond #000000, panneaux #111111.
- Une seule couleur d'accent ; vert et rouge réservés aux variations, toujours accompagnés d'un signe +/−.
- Police Inter. Gros chiffre de 40-56 px en tête de chaque vue. Rayon des cartes 16 px. Beaucoup d'espace.
- Graphiques : une ligne principale, pas de quadrillage, léger dégradé sous la courbe, curseur avec infobulle, pastilles de période 1D / 1W / 1M / 1Y / MAX.
- Mobile d'abord : barre d'onglets en bas sur mobile, barre latérale ou en-tête sur desktop.

---

## 6. Architecture (évolution de la v1)

- Même principe : GitHub Actions → Parquet → JSON compacts → GitHub Pages statique.
- Nouveaux fetchers : `open_meteo.py`, `eco2mix.py`, `smard.py`, `elexon.py`, `nve.py`, `entsog.py`, `energinet.py`, `elering.py`, `ree.py`.
- Les textes pédagogiques vivent dans `docs/content/*.json` : on les édite sans toucher au code.
- Un registre `datahub_registry.py` décrit chaque ligne du Data Hub (source, série, unité, fréquence, licence, textes) ; le front se construit à partir de ce registre.
- Cadences : Elia et éCO2mix toutes les 15 min ; Energy-Charts et Open-Meteo toutes les heures ; NVE chaque semaine ; capacités chaque mois.

---

## 7. Étapes

1. Step 0 : vérifier les nouvelles sources et produire un rapport.
2. Fetchers et backfill (12 mois minimum ; météo ERA5 et archive des prévisions).
3. Calculs des vues (agrégats, pentes, détecteurs, brief du jour).
4. Refonte du front : design system, puis pages une par une.
5. Contenus pédagogiques et glossaire.
6. Workflows, déploiement, vérification de chaque page sur mobile.

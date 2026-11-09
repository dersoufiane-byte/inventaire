# Inventaire Clinique — version 2.0

Le téléphone photographie et reconnaît les produits ; le PC reçoit par le Wi-Fi, enregistre et exporte au format Excel du modèle « Inventaire ».

## Contenu
- `android/` : application téléphone (APK) — caméra multi-photos, galerie, analyse IA groupée (Gemini gratuit ou Claude), produits sans étiquette, envoi au PC, file d'attente hors ligne.
- `pc/` : programme PC — `server.py` (réception, base de données, export Excel, sauvegardes, recherche automatique par le téléphone) et `web/index.html` (interface).
- `.github/workflows/build.yml` : compilation automatique sur GitHub.

## Obtenir le programme
1. Envoyez tout ce dossier dans un dépôt GitHub (y compris `.github`).
2. Onglet **Actions** → « Construire Inventaire Clinique » : deux étapes (téléphone puis PC), environ 10 minutes.
3. Quand c'est vert : en bas, **Artifacts** → **InventaireClinique-PC** : le zip contient `InventaireClinique.exe` (l'APK est inclus dedans) et `LISEZMOI.txt`.

## Tester sans compiler (PC avec Python)
`python pc/server.py` puis ouvrir http://localhost:8765

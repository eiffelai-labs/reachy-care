# Anker Soundcore PowerConf S3 — Hardware Reference

Enceinte retenue pour Reachy Care (remise Julie ). Décision actée .

---

## Specs produit

| Paramètre | Valeur |
|-----------|--------|
| Modèle | Anker Soundcore PowerConf S3 |
| Prix | ~65 € (Amazon FR) |
| Microphones | 6 (ignorés — XMOS XVF3800 garde la main) |
| Batterie | 24h |
| Connectivité | USB-C (UAC natif) + Bluetooth 5 |
| Son | Omni 360° |
| Auto-off | Non quand USB connecté (always-on ) |

---

## Intégration Pi

- **Connexion** : USB-C → port USB-C Pi (ou hub si occupé)
- **Rôle** : remplace le dongle Cabletime + câble jack (commit `de2e3bd`)
- **ALSA** : la carte sera reconnue sous un nom à confirmer à réception (`hw:CARD=PowerConfS3` ou similaire) — mettre à jour `resources/asound.conf` en conséquence
- **Capture** : `asound.conf` côté capture reste sur `dsnoop_xmos`. Les 6 mics de l'Anker sont ignorés.

---

## Pourquoi ce choix

- UAC natif sur Pi aarch64, zéro driver
- Omni 360° : rayonne dans toute la pièce depuis le socle fermé
- Batterie 24h : si infirmière débranche le câble USB, le son ne coupe pas
- Always-on quand USB alimenté
- AEC XMOS validé terrain(AUDIO_MGR_FAR_END_DSP_ENABLE=1) → pas besoin d'AEC matériel dans l'enceinte

---

## À faire à réception

1. Brancher en USB-C sur Pi, vérifier `aplay -l` → noter le nom exact de la carte ALSA
2. Mettre à jour `resources/asound.conf` (remplacer `hw:CARD=Device` par le nouveau nom)
3. Tester `aplay -D reachymini_audio_sink test.wav` → vérifier son omni depuis le socle
4. Mettre à jour `DECISIONS.md` et `STATE.md` avec le nom ALSA confirmé

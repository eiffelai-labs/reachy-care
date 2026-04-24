# Reachy Mini Lite — Hardware Reference

Source officielle : [HuggingFace Hardware Datasheet](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini_lite/hardware)

---

## Positionnement

| | Lite ($299) | Wireless ($449) — notre proto |
|---|---|---|
| Compute | PC externe (Mac/Linux via USB-C) | Pi 5 intégré (notre proto = Pi 4) |
| Connectivité | USB-C data only (pas de charge) | WiFi 6, Bluetooth 5.2 |
| Alimentation | Adaptateur externe 6.8-7.6V | Batterie rechargeable |
| Micros | 4 PDM MEMS, XMOS XVF3800 | 4 PDM MEMS, XMOS XVF3800 |
| Speaker | 5W @4Ω | 5W @4Ω |
| Camera | Raspberry Pi Cam v3 wide (Sony IMX708) | idem |
| IMU | non | oui |
| Usage cible | Développeur avec PC dédié | Autonome sans PC |

**Conclusion pour Reachy Care (septembre 2026) :** la Lite sera déployée avec un **mini PC externe** caché dans le socle, branché USB-C. Avantage vs Pi 5 embarqué : compute nettement supérieur pour TTS local, face reco lourde, LLM embarqué. Décision actée .

---

## Dimensions et mécanique

- **Dimensions :** 30 × 20 × 15.5 cm (étendu)
- **Masse :** 1.350 kg
- **Matériaux :** ABS, PC, Aluminium, Acier

### Degrés de liberté

| Partie | DOF | Détail |
|--------|-----|--------|
| Tête (Stewart Platform) | 6 | 3 rotations + 3 translations |
| Corps (base) | 1 | rotation 360° |
| Antennes | 2 × 1 | rotation indépendante |

### Moteurs

| Emplacement | Modèle | Quantité | Note |
|-------------|--------|----------|------|
| Base | Custom Dynamixel XC330-M288-PG | 1 | engrenage plastique |
| Antennes | Dynamixel XL330-M077-T | 2 | petit moteur fragile |
| Stewart Platform (tête) | Dynamixel XL330-M288-T | 6 | |

> Les XL330 antennes sont fragiles — ne pas dépasser les limites de couple. Les XC330 base sont costauds.

---

## Audio

### Microphones

- **Array :** Seeed Studio reSpeaker XMOS XVF3800
- **Nombre :** 4 PDM MEMS digitaux
- **Sample rate max :** 16 kHz
- **Sensibilité :** -26 dB FS
- **SNR :** 64 dBA
- **AEC hardware :** oui, via XMOS (contrôle HID : `AUDIO_MGR_FAR_END_DSP_ENABLE`, `AEC_FAR_EXTGAIN`)

**Important :** la Lite a le MÊME XMOS XVF3800 que la Wireless. Tout le travail AEC (session , `AUDIO_MGR_FAR_END_DSP_ENABLE=1`) est applicable aux deux versions.

### Speaker intégré

- **Puissance :** 5W @4Ω
- **Verdict :** inaudible en EHPAD (validé terrain). Toujours compléter avec une enceinte externe.
- **ALSA :** `hw:0,0` (carte interne Reachy Mini)

### Speaker externe (Reachy Care)

- **Actuel :** dongle USB-C DAC Cabletime (UAC natif, 24kHz validé Pi) → jack 3.5mm → enceinte externe
- **Cible remise Julie  :** enceinte active 20W+ 3.5mm (~40-60€)
- **Cible commerciale :** enceinte USB-C UAC (Anker PowerConf S3 ou équivalent), décision après test AEC 
- **ALSA :** carte `Device` (dongle USB-C, commit `de2e3bd` )

---

## Caméra

- **Modèle :** Raspberry Pi Camera v3 Wide Angle
- **Capteur :** Sony IMX708
- **Résolution :** 12 MP
- **Mise au point :** Autofocus
- **Interface :** DSI (CSI sur le controller board)
- **Angle :** 120°

---

## Alimentation

- **Tension d'entrée :** 6.8 - 7.6V
- **Fournie par :** Power Board interne (Lite = adaptateur externe)
- **USB-C :** data only, ne charge PAS le robot

---

## Controller board (Lite)

Gère :
- Motors Dynamixel (bus TTL)
- Camera (CSI)
- Mic array
- USB-C host interface

---

## SDK et connectivité

- **Langue principale :** Python (JS + Scratch prévus)
- **Connexion Lite :** USB-C vers PC (Mac/Linux)
- **SDK :** `reachy_mini` (pollen-robotics/reachy_mini)
- **Hugging Face Hub :** intégré pour les modèles IA

---

## Implications pour Reachy Care

| Sujet | Lite | Wireless (notre cible) |
|-------|------|------------------------|
| AEC XMOS | oui, même chip | oui |
| Audio pipeline | identique | identique |
| Déploiement EHPAD autonome | non (PC externe requis) | oui |
| Code conv_app_v2 | compatible | notre base de dev |
| Face reco / vision | identique (même camera) | identique |
| Wake word | identique (même XMOS) | identique |

**Conclusion :** développer sur Wireless = compatible Lite pour tout ce qui est audio/vision/moteurs. La seule divergence est la couche système (Pi vs PC hôte).

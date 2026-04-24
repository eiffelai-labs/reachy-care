# REACHY_MINI_ANTENNAS.md — Spécifications moteurs antennes

> Consolidé  soir, après session debug wake-word gesture.
> Source : recherche Researcher dans `pollen-robotics/reachy_mini` GitHub main.
> **À consulter avant toute nouvelle manipulation des antennes.**

---

## Hardware

- **Moteurs** : 2× Dynamixel XL-330-M288-T (mêmes que la tête, sauf plage)
- **IDs firmware** : 17 (right) et 18 (left)
- **Mode** : 3 (position continu)
- **Résolution** : 0.088°/tick (4096 ticks par tour complet = 360°)
- **Plage firmware** : `lower_limit: 0, upper_limit: 4095` (YAML `hardware_config.yaml:141-164`)
  → **0 à 360° continu, aucune butée logicielle firmware**
- **Shutdown error programmé** : `52` = OVERLOAD | ELECTRICAL_SHOCK | ENCODER | OVERHEATING

## API Python SDK

### `set_target_antenna_joint_positions(antennas: List[float])`
- Source : `reachy_mini.py:871-873`
- Unité : **radians**
- Ordre : `[right_angle, left_angle]`
- Longueur : 2 stricte (assert reachy_mini.py:841)
- Commande : **step instantané** (pas d'interpolation)
- **⚠️ AUCUN CLAMP** côté Python/Rust/firmware. Step > 2 rad depuis position courante → risque de `shutdown_error` silencieux → antenne gelée tant que non reset

### `goto_target(head, antennas=[r, l], duration=T)`
- Interpolation min-jerk progressive sur `duration`
- **Méthode recommandée pour grandes amplitudes** (±π/2 et plus)
- `goto_sleep` utilise cette API avec `[-3.05, 3.05]` (≈±174.7°) sur 2s

### `_goto_joint_positions(antennas_joint_positions=[r, l], duration=T)`
- Privé (préfixe `_`) mais appelable
- Joint-space direct, n'affecte pas la tête
- Même interpolation interne que `goto_target`

## Conversion rad ↔ ticks (info)

```
ticks = 4096 * (π + value_rad) / (2π)
```

| Radians | Degrés | Ticks |
|---------|--------|-------|
| −π      | −180°  | 0     |
| −1.5    | −86°   | 1071  |
| −0.175  | −10°   | 1934  |
| 0       | 0°     | 2048  |
| 0.175   | +10°   | 2162  |
| 1.5     | +86°   | 3025  |
| π       | +180°  | 4096  |
| 3.05    | +174.7°| 4038  |

## Constantes clés du SDK

- `INIT_ANTENNAS_JOINT_POSITIONS = [-0.1745, 0.1745]` — offset neutre anti-shaking (±10°). Les antennes ne reposent PAS à 0, elles ont un léger écartement par défaut (issue Pollen #951).
- `SLEEP_ANTENNAS_JOINT_POSITIONS = [-3.05, 3.05]` — position dodo (≈±174.7°, antennes **pendantes quasi-verticales vers le bas** de chaque côté).

## Règles pratiques (Reachy Care)

### Signes et sens (convention observé e terrain )
- **Même signe** sur les deux antennes → partent dans **le même sens** (sol. L'ALSA reveil)
- **Signes opposés** avec `[-X, +X]` → divergence **vers l'extérieur** (geste d'ouverture)
- **Signes opposés** avec `[+X, -X]` → convergence **vers l'intérieur** (croisement)

### Amplitude
- **Step instantané max sûr** via `set_target_antenna_joint_positions` : ≈ ±1.5 rad depuis neutre (±86°). Au-delà → shutdown.
- **Pour >1.5 rad** : utiliser `goto_target` avec `duration >= 0.2s` (interpolation).
- **Limite physique réelle** : ±π rad par antenne (plage firmware complète, jamais atteinte en pratique).

### Positions recommandées pour gestes
| Intention | Valeurs | Méthode |
|-----------|---------|---------|
| Accusé éveil (wake) | `[-π/2, +π/2]` divergence 90° | `goto_target(duration=0.15)` aller + `goto_target(duration=0.15)` retour |
| Surprise / alerte | `[-0.5, +0.5]` écart léger rapide | `set_target` direct |
| Joie / happy | `[0.5, 0.5]` même sens vers arrière | `set_target` direct |
| Tristesse | `[-0.3, -0.3]` légèrement tombantes | `set_target` direct |
| Dodo | `[-3.05, 3.05]` pendantes | `goto_target(duration=2.0)` (imposé par SDK) |

### Idées gestes par personnage mode histoire

- **Narrateur** : antennes neutres (`[-0.175, 0.175]`)
- **Héros** : antennes dressées droites (`[0.3, -0.3]`, légère convergence)
- **Méchant** : antennes écartées basses (`[-1.0, 1.0]`)
- **Magicien** : antennes ondulent doucement (boucle sin, low-amplitude)
- **Petit animal** : antennes frémissent (vibrato ±0.1 rad à 5 Hz)

À implémenter dans `tools_for_conv_app/switch_mode.py` ou via un tool LLM dédié `set_character_antennas(name)`.

## Gestion des erreurs

Si une antenne ne bouge plus après une commande grosse amplitude :
1. Lire `reachy.client.get_status()` pour détecter `Hardware_Error_Status` (addr 70)
2. Si un bit allumé : `reachy.enable_motors()` pour réactiver les moteurs
3. Puis ré-envoyer une cible **interpolée** (`goto_target` avec duration >= 0.5)

## Fichiers source (pollen-robotics/reachy_mini, branche main)

- `src/reachy_mini/reachy_mini.py` lignes 55-56, 557-587, 728-780, 871-873
- `src/reachy_mini/daemon/backend/abstract.py` lignes 375-385, 438-444, 867-917
- `src/reachy_mini/daemon/backend/robot/backend.py` lignes 195-212
- `src/reachy_mini/assets/config/hardware_config.yaml` lignes 141-164
- `src/reachy_mini/daemon/app/routers/move.py` (REST `/move/goto`)
- `pollen-robotics/rustypot/src/servo/dynamixel/xl330.rs` lignes 84-113

## Issues GitHub Pollen liées

- [#951](https://github.com/pollen-robotics/reachy_mini/issues/951) — offset neutre anti-shaking (appliqué )
- [#955](https://github.com/pollen-robotics/reachy_mini/issues/955), [#960](https://github.com/pollen-robotics/reachy_mini/issues/960) — antennes shaking
- [#63](https://github.com/pollen-robotics/reachy_mini/issues/63) — inversion Mujoco/réel

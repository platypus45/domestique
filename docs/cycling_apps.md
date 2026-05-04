# Cycling Apps with ZWO/FIT Import

Domestique v4.0.0-alpha is a planner + workout library + post-ride viewer.
You ride with one of the apps below, then import the FIT file back into
Domestique for analysis.

This table compares the free (or formerly free) cycling apps that accept
`.zwo` workout files and/or drive a Tacx Neo 2T smart trainer.

| App | Free tier? | ZWO import? | FIT import? | Structured workouts? | Tacx Neo 2T support? | Platform | Downsides |
|---|---|---|---|---|---|---|---|
| Zwift | No (14-day trial only; 25km/mo free ended Aug 2025) | Yes, drop .zwo in Documents/Zwift/Workouts/<userid>/ | No (FIT is export-only) | Yes, large library + custom | Yes, full ERG + virtual shifting | Win/Mac/iOS/Android/AppleTV | Paid-only now at $20/mo; no in-app workout editor |
| MyWhoosh | Yes, fully free forever | Yes, via web workout builder upload | No | Yes, plus builder | Yes, full ERG | Win/Mac/iOS/Android/AppleTV | Ad-supported; smaller course library than Zwift |
| Golden Cheetah | Yes, fully free/open-source | Yes (ERG/MRC/ZWO) | Yes (analysis) | Yes, via Train view ERG mode | Yes, via ANT+ FE-C (BLE limited) | Win/Mac/Linux | Analysis-first UI; clunky live-ride experience; no scenery |
| Tacx Training (Garmin) | Yes, free with Tacx hardware | No (feature-requested for years, not shipped) | No (only .gpx) | Yes (basic) | Yes, native | Win/Mac/iOS/Android | No ZWO/FIT import; premium locks plans/video |
| Rouvy | 7-day trial; no permanent free tier | Yes (.zwo/.erg/.mrc via Riders Portal) | No | Yes | Yes, full ERG + virtual shifting | Win/Mac/iOS/Android/AppleTV | Paid-only (~$15/mo); real-video focus not workout-focus |
| IndieVelo / TP Virtual | No (free period ended ~March 2025; now TP Premium) | Yes (.zwo/.erg/.mrc) | No | Yes | Yes | Win/Mac/iOS/Android | Now $20/mo bundled with TrainingPeaks Premium |
| Wahoo SYSTM | 14-day trial only | Workaround via email (legacy RGT path); no UI import | No | Yes, 4DP-based | Yes | Win/Mac/iOS/Android | Paid-only; no native workout builder; clunky import |
| TrainerRoad | No (30-day money-back only) | No native ZWO import (own .ert/workout builder) | No | Yes, best-in-class AI plans | Yes, full ERG | Win/Mac/iOS/Android | $22/mo; closed ecosystem, no ZWO interop |
| Kinomap | Freemium (public workouts only; your own custom = paid) | No | No | Yes in paid tier | Yes | Win/Mac/iOS/Android | Video-first; your own workouts locked behind sub (~€12/mo) |
| BKOOL | N/A — shut down, merged into Rouvy (Mar 2026) | N/A | N/A | N/A | N/A | N/A | Dead platform; use Rouvy |
| FulGaz | 14-day trial only | Yes (.zwo via Member's Page) | No | Yes (power-based ERG only) | Yes | Win/Mac/iOS/Android/AppleTV | Paid-only (~£10/mo); real-video focus, small workout UI |
| RGT Cycling / Wahoo RGT | N/A — shut down Oct 31, 2023 | N/A | N/A | N/A | N/A | N/A | Dead platform |
| Zwoffline (zwift-offline) | Yes, free (open-source) | Yes (runs real Zwift client; same .zwo folder) | No | Yes (via Zwift client) | Yes | Win/Mac/Linux (+Docker) | Unofficial, violates Zwift TOS; real-account ban risk; solo only |

## RECOMMENDED FOR YOUR USE CASE

1. **MyWhoosh** — only mainstream app that is genuinely free forever, supports .zwo upload via its web builder, drives the Neo 2T in full ERG, and works on every platform. Zero paywall for a user who wants to do structured rides.
2. **Golden Cheetah** — free, open-source, installs locally, accepts ZWO/ERG/MRC and drives Tacx Neo 2T in ERG over ANT+ FE-C. Ugly UI and no scenery, but zero vendor lock-in and perfect for a pure-training workflow; great backup/analysis tool alongside MyWhoosh.

---

*As of 2026-04-24.*

# DDB fixtures

- `search.json`: live, 19.10.2026; `search` for `"Koch und Kellermeisterey"`,
  rows 5.
- `item_EVZECNILXXNF2QLAQNBPEECKDKFYG5AZ.json`: baseline, 19.10.2026; item
  endpoint answer for the register record (SLUB links, no manifest).
- `item_O4JTHPWDL5LKMMXXO4R5236ZLTECHF5H.json`: baseline, 19.10.2026; item
  endpoint answer for the MDZ-held record (`isShownAt` names `bsb10923113`).
- `manifest_bsb10923113.json`: baseline, 19.10.2026; MDZ manifest built from
  that `isShownAt` link (200 canvases).
- `isshownby_challenge.html`: baseline, 19.10.2026; the bot-challenge page SLUB
  served for the register record's `isShownBy` image URL; client IP, challenge
  id, random data and rule hash set to `REDACTED`.

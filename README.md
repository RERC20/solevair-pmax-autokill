# SoleVair PMax Auto-Kill

The LolaLooks auto-kill engine (same rules) for **SoleVair**: its own Shopify store, Google Ads account
8050055596 (USD, America/New_York) and campaign ids - see `store_config.py`.

- **Built, not hand-edited:** `Desktop/store-autokill/build_engines.py` copies the live LolaLooks engine and applies
  count-checked store changes. Change a rule in the LolaLooks engine, rebuild, push.
- **Mode:** DRY (drafts / tags / moves nothing) until the repo variable `ENGINE_LIVE` = `1`.
- **Ladder sections** (Rising / Proven / Last Chance) are skipped until their campaign ids are in `stores.json`.
- **Money:** Google spend is converted USD -> GBP at the daily ECB rate before any rule
  (`store_guard.py`).
- **API budget:** Explorer access = 2,880 Google Ads operations a day; the engine counts its calls per Pacific day and
  stops at 2000. Runs every 20 min (17,37,57 * * * *).
- Credentials: repo Secrets only. Run logs: private Actions cache only, never committed.

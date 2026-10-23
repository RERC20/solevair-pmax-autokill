# store_guard.py - STORE KIT additions to the LolaLooks auto-kill engine (12 Oct 2026). No secrets; safe to commit.
#
# 1. GOOGLE ADS API DAILY BUDGET. This store's ad account has Explorer access = 2,880 operations a day (LolaLooks has
#    Basic access, 15,000). Every Google Ads call of the engine goes through requests.post, so this module wraps it:
#    each call to googleads.googleapis.com is counted per PACIFIC day (Google's quota day, resets 00:00 PT) in
#    _api_calls.json (kept between runs in the PRIVATE Actions cache). At DAILY_GOOGLE_API_BUDGET the call is refused
#    with a GOOGLE_QUOTA_EXHAUSTED error - the engine already treats that as "skip quietly, Shopify untouched".
# 2. CURRENCY. The ad account bills in USD, the store sells in GBP. The kill rules compare Google spend with Shopify
#    revenue and use GBP lines (GBP 5 gate, price/7 ...), so every metrics.costMicros the engine reads is converted to
#    GBP at that day's ECB rate (api.frankfurter.dev; weekends/holidays = the last business day). No rate = the run
#    stops with an error (a guessed rate is never used). A run with no conversion needed (same currency) is untouched.
# 3. RULE LINES (17 Oct 2026, Greta = EUR store). The rules' fixed money lines are GBP (GBP 4 no-sale cap, GBP 5 / GBP 10
#    spend gates). A store selling in another currency compares spend and revenue in ITS currency, so rule_fx() turns
#    each GBP line into that currency at the latest ECB GBP->store rate (1.0 for a GBP store = unchanged). Ratios (ROAS
#    2.4, price/7) need no conversion. No rate = the run stops (never a guessed rate).
import datetime
import json
import os

import requests

import store_config as SC

_PT = __import__("zoneinfo").ZoneInfo("America/Los_Angeles")
_CALLS_F = "_api_calls.json"
_FX_F = "_fx_cache.json"
_orig_post = requests.post
_RUN = {"calls": 0}


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                       # noqa: BLE001  (missing / unreadable = start fresh)
        return {}


def calls_today():
    return int(_read(_CALLS_F).get(datetime.datetime.now(_PT).date().isoformat(), 0))


def calls_this_run():
    return _RUN["calls"]


def _counting_post(url, *args, **kwargs):
    if "googleads.googleapis.com" in str(url):
        day = datetime.datetime.now(_PT).date().isoformat()
        n = int(_read(_CALLS_F).get(day, 0))
        if n >= SC.DAILY_GOOGLE_API_BUDGET:
            raise RuntimeError(f"GOOGLE_QUOTA_EXHAUSTED: {SC.STORE_LABEL} daily Google Ads API budget reached "
                               f"({n} of {SC.DAILY_GOOGLE_API_BUDGET} calls, Pacific day {day}); resets 00:00 PT")
        try:
            with open(_CALLS_F, "w", encoding="utf-8") as f:
                json.dump({day: n + 1}, f)          # only today is kept
        except OSError:
            pass
        _RUN["calls"] += 1
    return _orig_post(url, *args, **kwargs)


requests.post = _counting_post

_FX = {}


def _rates():
    """[(date, rate)] ascending, ADS_CURRENCY -> STORE_CURRENCY; None when no conversion is needed."""
    if "r" in _FX:
        return _FX["r"]
    if SC.ADS_CURRENCY == SC.STORE_CURRENCY:
        _FX["r"] = None
        return None
    end = datetime.date.today()
    start = end - datetime.timedelta(days=SC.FX_LOOKBACK_D)
    got, err = None, ""
    for attempt in range(3):
        try:
            r = requests.get(f"https://api.frankfurter.dev/v1/{start}..{end}",
                             params={"base": SC.ADS_CURRENCY, "symbols": SC.STORE_CURRENCY}, timeout=30)
            r.raise_for_status()
            got = {k: float(v[SC.STORE_CURRENCY]) for k, v in r.json()["rates"].items()}
            break
        except Exception as ex:              # noqa: BLE001
            err = f"{type(ex).__name__}: {str(ex)[:120]}"
            __import__("time").sleep(3 * (attempt + 1))
    cache = _read(_FX_F)
    if got:
        cache.update(got)
        try:
            with open(_FX_F, "w", encoding="utf-8") as f:
                json.dump(dict(sorted(cache.items())[-400:]), f)
        except OSError:
            pass
    elif not cache or max(cache) < (end - datetime.timedelta(days=7)).isoformat():
        raise RuntimeError(f"FX rate {SC.ADS_CURRENCY}->{SC.STORE_CURRENCY} unavailable ({err}) and no recent cached rate - "
                           f"run stopped (spend is never converted at a guessed rate)")
    rates = sorted((datetime.date.fromisoformat(k), v) for k, v in (cache if not got else {**cache, **got}).items())
    _FX["r"] = rates
    return rates


def rate_for(day=None):
    """The rate for one account day (YYYY-MM-DD or date): the last business-day rate on or before it; the latest rate
    when no day is given; the earliest one for a day older than the series."""
    rates = _rates()
    if rates is None:
        return 1.0
    if day is None:
        return rates[-1][1]
    d = day if isinstance(day, datetime.date) else datetime.date.fromisoformat(str(day)[:10])
    best = rates[0][1]
    for rd, v in rates:
        if rd <= d:
            best = v
        else:
            break
    return best


def fx_rows(rows):
    """Convert metrics.costMicros of Google Ads search rows to the STORE currency, in place (per row's segments.date when
    the row has one, else the latest rate). Returns the rows."""
    if _rates() is None:
        return rows
    for row in rows:
        m = row.get("metrics")
        if m and "costMicros" in m:
            day = (row.get("segments") or {}).get("date")
            m["costMicros"] = str(int(round(int(m["costMicros"]) * rate_for(day))))
    return rows


_RFX = {}


_RFX_F = "_fx_rule_cache.json"     # last good rule-line rate (Actions cache), used only when every live source is down


def _rule_rate_live(rc, sc):
    """rc -> sc spot rate from three independent free sources -> (rate, source) or (None, error summary).
    23 Oct 2026: frankfurter alone went down (HTTP 522) and stopped the Greta run - so it is no longer the only source."""
    lo = rc.lower(); so = sc.lower()
    srcs = (("frankfurter", "https://api.frankfurter.dev/v1/latest", {"base": rc, "symbols": sc}, lambda j: j["rates"][sc]),
            ("open.er-api", f"https://open.er-api.com/v6/latest/{rc}", None, lambda j: j["rates"][sc]),
            ("currency-api", f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{lo}.json",
             None, lambda j: j[lo][so]))
    errs = []
    for attempt in range(2):
        for name, url, params, pick in srcs:
            try:
                r = requests.get(url, params=params, timeout=20)
                r.raise_for_status()
                v = float(pick(r.json()))
                if 0.2 < v < 5.0:                # sanity: a garbage reply is never used as a rate
                    return v, name
                errs.append(f"{name}: implausible rate {v}")
            except Exception as ex:              # noqa: BLE001
                errs.append(f"{name}: {type(ex).__name__}: {str(ex)[:90]}")
        __import__("time").sleep(5)
    return None, "; ".join(errs[-3:])


def rule_fx():
    """GBP rule lines -> the store currency (1.0 when the store sells in GBP)."""
    if "r" in _RFX:
        return _RFX["r"]
    rc = getattr(SC, "RULE_CURRENCY", "GBP")
    mult = float(getattr(SC, "RULE_SPEND_MULT", 1.0) or 1.0)   # store's extra room on the money lines (owner 2026-10-18: US +20%)
    if SC.STORE_CURRENCY == rc:
        _RFX["r"] = 1.0 * mult
        return _RFX["r"]
    pair = f"{rc}->{SC.STORE_CURRENCY}"
    rate, info = _rule_rate_live(rc, SC.STORE_CURRENCY)
    today = datetime.date.today().isoformat()
    if rate is not None:
        try:
            with open(_RFX_F, "w", encoding="utf-8") as f:
                json.dump({"pair": pair, "date": today, "rate": rate, "source": info}, f)
        except OSError:
            pass
    else:
        c = _read(_RFX_F)
        fresh = (c.get("pair") == pair and c.get("rate")
                 and c.get("date", "") >= (datetime.date.today() - datetime.timedelta(days=7)).isoformat())
        if not fresh:
            raise RuntimeError(f"FX rate {pair} for the rule lines unavailable from all 3 sources ({info}) and no saved "
                               f"rate from the last 7 days - run stopped (rules never run at a guessed rate)")
        rate = float(c["rate"])
        print(f"  !! live FX sources down ({info}) - using the saved {pair} rate {rate:.4f} from {c['date']}")
    _RFX["r"] = rate * mult
    return _RFX["r"]


def money(text):
    """Report text in the store currency symbol (the engine's messages were written with GBP signs)."""
    if SC.STORE_CURRENCY != "EUR":
        return text
    # 23 Oct 2026: never rewrite a currency PAIR ("GBP->EUR") or a URL parameter ("base=GBP") - an error message rewritten
    # to "EUR->EUR" hid what had actually failed
    return __import__("re").sub(r"(?<![=/A-Za-z])GBP(?!->|[A-Za-z])", "EUR", text.replace("£", "€"))


def orders_in_30d():
    """Shopify order count of the last 30 days, or None when it cannot be read. A NEW store can truly have no sales: the
    LolaLooks glitch guard ("GBP 0 revenue across every product = a failed orders pull") only aborts a run here when the
    store HAS (non-cancelled) orders (or the count fails) - never because the store is new."""
    from kill_engine_v4 import SHOP, SHOP_API, shopify_token
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        j = _orig_post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                       headers={"X-Shopify-Access-Token": shopify_token(), "Content-Type": "application/json"},
                       json={"query": '{ ordersCount(query:"created_at:>=%s -status:cancelled", limit:null){ count } }' % since[:10]},
                       timeout=60).json()   # 17 Oct 2026: cancelled orders excluded (= the engine's revenue pull); date-only filter
        return int(j["data"]["ordersCount"]["count"])
    except Exception:                        # noqa: BLE001
        return None


def status_line():
    return (f"Google API calls: {calls_this_run()} this run | {calls_today()} of {SC.DAILY_GOOGLE_API_BUDGET} today (PT day)"
            + (f" | spend converted {SC.ADS_CURRENCY}->{SC.STORE_CURRENCY} at {rate_for():.4f}" if _rates() else "")
            + (f" | GBP rule lines x{rule_fx():.4f} -> {SC.STORE_CURRENCY}" if rule_fx() != 1.0 else ""))

"""Daily P&L row for this store -> Google Sheet (via the PnL Apps Script web app).
Runs after the engine. Acts only on the first engine run of local hour 00 (first fill) and 06 (recheck, Google Ads
data final), and writes YESTERDAY in the store's own timezone. Manual: python pnl_daily.py 2026-10-18 [--dry]
Writes only: Orders, QTY, Projected Spend, Revenue, Adspend Google, Transaction fee, CVR, AOV, CPC (USD)."""
import sys, os, json, datetime, collections, requests
from zoneinfo import ZoneInfo
import kill_engine_google_auto as K, google_ads_connect as ga
import re as _re
ga.ADS_BASE = _re.sub(r'/v\d+$', '/v25', ga.ADS_BASE.rstrip('/'))   # some stores' connector still points at a retired API version

STORE = os.environ.get('PNL_STORE', '').strip()
URL = os.environ.get('PNL_URL', '').strip()
SECRET = os.environ.get('PNL_SECRET', '').strip()

def compute(D):
    tok = K.shopify_token()
    gq = lambda q, v=None: K._shopify_read(tok, q, v or {}, timeout=90)
    shop = gq('{shop{ianaTimezone currencyCode}}')['data']['shop']; TZ = ZoneInfo(shop['ianaTimezone']); CUR = shop['currencyCode']
    # FX: ECB rate for the day (walk back over weekends/holidays)
    def usd_rate(cur):
        if cur == 'USD': return 1.0
        r = requests.get(f'https://api.frankfurter.app/{(D-datetime.timedelta(days=7)).isoformat()}..{D.isoformat()}', params={'from': cur, 'to': 'USD'}, timeout=30).json()
        rates = {datetime.date.fromisoformat(k): v['USD'] for k, v in r['rates'].items()}
        return next(rates[D - datetime.timedelta(days=i)] for i in range(8) if D - datetime.timedelta(days=i) in rates)
    rate = usd_rate(CUR)
    # Orders (store-timezone day): exclude test, cancelled, £0, exchange/replacement/resend tags
    start = datetime.datetime.combine(D, datetime.time(), TZ); end = start + datetime.timedelta(days=1)
    Q = '''query($c:String,$q:String){orders(first:100,after:$c,query:$q){pageInfo{hasNextPage endCursor} edges{node{name createdAt test cancelledAt tags
     totalPriceSet{shopMoney{amount}} lineItems(first:100){edges{node{quantity discountedTotalSet{shopMoney{amount}}}}}
     transactions(first:20){kind status fees{amount{amount}}}}}}}'''
    orders = qty = 0; rev = fee = 0.0; excluded = []; cur = None
    while True:
        j = gq(Q, {'c': cur, 'q': f'created_at:>={start.astimezone(datetime.timezone.utc).isoformat()} created_at:<{end.astimezone(datetime.timezone.utc).isoformat()}'})['data']['orders']
        for e in j['edges']:
            o = e['node']; d = datetime.datetime.fromisoformat(o['createdAt'].replace('Z', '+00:00')).astimezone(TZ).date()
            if d != D: continue
            tot = float(o['totalPriceSet']['shopMoney']['amount']); tags = [t.lower() for t in o['tags']]
            why = ('test' if o['test'] else 'cancelled' if o['cancelledAt'] else 'zero' if tot < 0.01 else
                   'exchange/replacement' if any(k in t for t in tags for k in ('exchange', 'replacement', 'resend', 're-send')) else None)
            if why: excluded.append(f"{o['name']}:{why}"); continue
            orders += 1; rev += tot * rate
            qty += sum(li['node']['quantity'] for li in o['lineItems']['edges'] if float(li['node']['discountedTotalSet']['shopMoney']['amount']) > 0)
            fee += sum(float(f['amount']['amount']) for t in o['transactions'] if t['status'] == 'SUCCESS' and t['kind'] in ('SALE', 'CAPTURE') for f in (t['fees'] or [])) * rate
        if j['pageInfo']['hasNextPage']: cur = j['pageInfo']['endCursor']
        else: break
    # Google Ads: hourly spend/clicks in the account's timezone, regrouped into the store's day
    gt = ga.get_access_token()
    def ads(q):
        out = []; pt = None
        while True:
            b = {'query': q}
            if pt: b['pageToken'] = pt
            r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/googleAds:search", headers=ga._headers(gt), json=b, timeout=90).json()
            if 'error' in r: raise SystemExit(json.dumps(r)[:600])
            out += r.get('results', []); pt = r.get('nextPageToken')
            if not pt: return out
    acc = ads("SELECT customer.time_zone, customer.currency_code FROM customer")[0]['customer']; ATZ = ZoneInfo(acc['timeZone'])
    arate = usd_rate(acc['currencyCode'])   # ad account money -> USD (LolaLooks' account is GBP)
    spend = 0.0; clicks = 0
    for x in ads(f"SELECT segments.date, segments.hour, metrics.cost_micros, metrics.clicks FROM customer WHERE segments.date BETWEEN '{(D-datetime.timedelta(days=1)).isoformat()}' AND '{(D+datetime.timedelta(days=1)).isoformat()}'"):
        t = datetime.datetime.combine(datetime.date.fromisoformat(x['segments']['date']), datetime.time(int(x['segments']['hour'])), ATZ).astimezone(TZ).date()
        if t == D: spend += int(x['metrics'].get('costMicros', 0)) / 1e6 * arate; clicks += int(x['metrics'].get('clicks', 0))
    # Projected spend: enabled campaigns' daily budgets as at 12:00 store time (or at creation if created later that day)
    camps = ads("SELECT campaign.id, campaign.status, campaign_budget.amount_micros FROM campaign WHERE campaign.status != 'REMOVED'")
    bud = {c['campaign']['id']: int(c['campaignBudget']['amountMicros']) / 1e6 * arate for c in camps}; st = {c['campaign']['id']: c['campaign']['status'] for c in camps}
    cdt = lambda v: datetime.datetime.strptime(v.split('.')[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ATZ)
    ev = []
    for x in ads(f"SELECT change_event.change_date_time, change_event.change_resource_type, change_event.changed_fields, change_event.old_resource, change_event.campaign, change_event.resource_change_operation FROM change_event WHERE change_event.change_date_time >= '{(D-datetime.timedelta(days=1)).isoformat()}' AND change_event.change_date_time <= '{(datetime.date.today()+datetime.timedelta(days=1)).isoformat()}' AND change_event.change_resource_type IN ('CAMPAIGN_BUDGET','CAMPAIGN') ORDER BY change_event.change_date_time DESC LIMIT 5000"):
        e = x['changeEvent']; ev.append(((e.get('campaign') or '').split('/')[-1], e))
    noon = datetime.datetime.combine(D, datetime.time(12), TZ); eod = end
    created = {cid: cdt(e['changeDateTime']) for cid, e in ev if e.get('resourceChangeOperation') == 'CREATE' and e['changeResourceType'] == 'CAMPAIGN' and cid}
    proj = 0.0
    for cid in bud:
        c = created.get(cid)
        if c and c >= eod: continue
        at = max(noon, c + datetime.timedelta(minutes=2)) if c else noon
        b = bud[cid]; s = st[cid]
        for k, e in ev:  # newest first: undo changes made after `at`
            if k != cid or cdt(e['changeDateTime']) <= at: continue
            f = e.get('changedFields', ''); o = e.get('oldResource', {})
            if 'amountMicros' in f and o.get('campaignBudget', {}).get('amountMicros'): b = int(o['campaignBudget']['amountMicros']) / 1e6 * arate
            if 'status' in f and o.get('campaign', {}).get('status') and e.get('resourceChangeOperation') != 'CREATE': s = o['campaign']['status']
        if s == 'ENABLED': proj += b
    spend = round(spend, 2)
    return dict(orders=orders, qty=qty, projected=round(proj, 2), revenue=round(rev, 2), spend=spend, fee=round(fee, 2),
                cvr=round(orders / clicks, 4) if clicks else 0, aov=round(rev / orders, 2) if orders else 0,  # sheet convention: 0% / $0 when none
                cpc=round(spend / clicks, 2) if clicks else None), dict(tz=str(TZ), ads_tz=str(ATZ), ads_cur=acc['currencyCode'], ads_fx=arate, clicks=clicks, fx=rate, excluded=excluded)

def post(D, vals):
    r = requests.post(URL, json={'secret': SECRET, 'store': STORE, 'date': D.isoformat(), 'values': vals}, timeout=60, allow_redirects=True)
    return r.text[:300]

def main(argv):
    dry = '--dry' in argv; args = [a for a in argv if not a.startswith('--')]
    if args: D = datetime.date.fromisoformat(args[0])
    else:
        tok = K.shopify_token(); TZ = ZoneInfo(K._shopify_read(tok, '{shop{ianaTimezone}}', {}, timeout=60)['data']['shop']['ianaTimezone'])
        now = datetime.datetime.now(TZ)
        if not (now.hour in (0, 6) and now.minute < 22): return print(f'[pnl] {now:%H:%M} {TZ} - not a fill time, skip')
        D = now.date() - datetime.timedelta(days=1)
    vals, info = compute(D)
    print(f'[pnl] {STORE} {D} {json.dumps(vals)} {json.dumps(info)}')
    if dry: return
    if not (URL and SECRET and STORE): return print('[pnl] PNL_URL/PNL_SECRET/PNL_STORE not set - nothing written')
    print('[pnl] sheet:', post(D, vals))

if __name__ == '__main__':
    try: main(sys.argv[1:])
    except Exception as ex: print('[pnl] ERROR', repr(ex)[:500])

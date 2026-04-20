import sqlite3
import datetime

conn = sqlite3.connect('data/db/data.db')
cur = conn.cursor()

cur.execute("""SELECT DISTINCT ea.epoch FROM executed_allocations ea
               WHERE EXISTS (SELECT 1 FROM boundary_reward_snapshots brs WHERE brs.epoch=ea.epoch)
               AND EXISTS (SELECT 1 FROM live_reward_token_samples lrs WHERE lrs.vote_epoch=ea.epoch)
               ORDER BY ea.epoch DESC""")
TARGET_EPOCHS = [r[0] for r in cur.fetchall()]


def get_price_map(epoch):
    cur.execute("""SELECT lower(token_address), usd_price FROM historical_token_prices h
                   WHERE timestamp=(SELECT MAX(h2.timestamp) FROM historical_token_prices h2
                                    WHERE lower(h2.token_address)=lower(h.token_address)
                                    AND h2.timestamp<=?)""", (epoch,))
    pm = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("SELECT lower(token_address), usd_price FROM token_prices")
    for r in cur.fetchall():
        if r[0] not in pm:
            pm[r[0]] = r[1]
    return pm


existing_denylist = {
    "0x25c10987091f98bff0f48a5bd24d7b3bf3419c52",
    "0x5d08b7cdb98ad2db2c5b24c32f7c32ad7ff19379",
    "0x42b49967d38da5c4070336ce1cca91a802a11e8c",
    "0x46bba290006233b0eda8fc6d6b4e66eb02115774",
    "0xe63cd99406e98d909ab6d702b11dd4cd31a425a2",
}

# gauge -> list of (epoch, predicted_usd, actual_usd, ratio)
disappointments = {}

for EPOCH in TARGET_EPOCHS:
    dt = datetime.datetime.utcfromtimestamp(EPOCH).isoformat() + "Z"

    cur.execute("""SELECT avr.id, avr.tx_hash FROM auto_vote_runs avr
                   JOIN executed_allocations ea ON ea.tx_hash = avr.tx_hash
                   WHERE ea.epoch=? AND avr.status='tx_success'
                   ORDER BY avr.vote_sent_at DESC LIMIT 1""", (EPOCH,))
    row = cur.fetchone()
    if not row:
        continue
    run_id, tx_hash = row

    cur.execute("""SELECT lower(gauge_address), executed_votes
                   FROM executed_allocations WHERE epoch=? AND tx_hash=?""", (EPOCH, tx_hash))
    exec_alloc = {r[0]: r[1] for r in cur.fetchall()}
    if not exec_alloc:
        continue

    price_map = get_price_map(EPOCH)

    # Pre-boundary predicted USD (latest live snapshot for this epoch)
    cur.execute("""SELECT lower(gauge_address), lower(reward_token), rewards_normalized
                   FROM live_reward_token_samples
                   WHERE vote_epoch=?
                   AND snapshot_ts=(SELECT MAX(snapshot_ts) FROM live_reward_token_samples WHERE vote_epoch=?)""",
                (EPOCH, EPOCH))
    predicted_usd = {}
    for gauge, token, norm in cur.fetchall():
        price = float(price_map.get(token, 0.0))
        predicted_usd[gauge] = predicted_usd.get(gauge, 0.0) + norm * price

    # Actual boundary USD
    cur.execute("""SELECT lower(gauge_address), lower(reward_token), rewards_raw, token_decimals
                   FROM boundary_reward_snapshots WHERE epoch=? AND active_only=1""", (EPOCH,))
    actual_usd = {}
    for gauge, token, raw, decimals in cur.fetchall():
        if not raw:
            continue
        try:
            dec = int(decimals) if decimals else 18
            amount = int(str(raw)) / (10 ** max(0, dec))
        except Exception:
            continue
        price = float(price_map.get(token, 0.0))
        actual_usd[gauge] = actual_usd.get(gauge, 0.0) + amount * price

    for gauge in exec_alloc:
        if gauge in existing_denylist:
            continue
        pred = predicted_usd.get(gauge, 0.0)
        actual = actual_usd.get(gauge, 0.0)
        if pred < 1.0:
            continue  # skip pools where prediction was noise
        ratio = actual / pred if pred > 0 else 0.0
        if ratio < 0.50:
            disappointments.setdefault(gauge, []).append((EPOCH, pred, actual, ratio))

print("Pools that delivered <50% of predicted reward (excluding already-denylisted):")
print("")
header = "{:<46} {:>4}  {:>9}  {}".format("Gauge", "Occ", "AvgRatio", "Detail")
print(header)
print("-" * 120)

for gauge, events in sorted(disappointments.items(), key=lambda kv: (-len(kv[1]), sum(e[3] for e in kv[1]))):
    avg_ratio = sum(e[3] for e in events) / len(events)
    parts = []
    for e, p, a, r in events:
        date = datetime.datetime.utcfromtimestamp(e).strftime("%m-%d")
        parts.append("{} pred=${:.0f} act=${:.0f} ({:.0%})".format(date, p, a, r))
    detail = ", ".join(parts)
    print("  {:<46} {:>4}  {:>8.0%}  {}".format(gauge, len(events), avg_ratio, detail))

print("")
print("Total unique pools: {}".format(len(disappointments)))
conn.close()

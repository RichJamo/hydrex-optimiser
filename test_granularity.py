from src.database import Database, HistoricalTokenPrice

db = Database('data.db')
session = db.get_session()

sample = session.query(HistoricalTokenPrice).filter_by(timestamp=1767830400).first()
if sample:
    print(f'Granularity stored in DB: {sample.granularity}')
    print(f'Token: {sample.token_address[:10]}...')
    print(f'Price: ${sample.usd_price}')
    
    # Test both
    prices_day = db.get_historical_token_prices([sample.token_address], 1767830400, 'day')
    prices_hour = db.get_historical_token_prices([sample.token_address], 1767830400, 'hour')
    
    print(f'\nDay query result: {len(prices_day)} tokens')
    print(f'Hour query result: {len(prices_hour)} tokens')
    
    if prices_hour:
        print(f'Hour price: ${prices_hour[sample.token_address]}')

session.close()

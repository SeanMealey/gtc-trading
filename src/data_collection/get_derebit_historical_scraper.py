import requests
import pandas as pd
from datetime import datetime
import time

# 1. Configuration
BASE_URL = "https://history.deribit.com/api/v2" # Note the 'history' subdomain
CURRENCY = "BTC"
START_TS = int(datetime(2026, 2, 24).timestamp() * 1000)
END_TS = int(datetime(2026, 3, 12).timestamp() * 1000)

def fetch_historical_trades(start_ms, end_ms):
    all_trades = []
    current_start = start_ms
    
    print(f"Fetching trades from {datetime.fromtimestamp(start_ms/1000)}...")

    while current_start < end_ms:
        params = {
            "currency": CURRENCY,
            "kind": "option",
            "start_timestamp": current_start,
            "end_timestamp": end_ms,
            "count": 1000, # Max allowed per request
            "sorting": "asc"
        }
        
        response = requests.get(f"{BASE_URL}/public/get_last_trades_by_currency", params=params).json()
        
        if 'result' not in response or not response['result']['trades']:
            break
            
        batch = response['result']['trades']
        all_trades.extend(batch)
        
        # Update the cursor to the timestamp of the last trade in the batch + 1ms
        current_start = batch[-1]['timestamp'] + 1
        
        print(f"Collected {len(all_trades)} trades. Last timestamp: {datetime.fromtimestamp(current_start/1000)}")
        time.sleep(0.1) # Respect rate limits
        
    return pd.DataFrame(all_trades)

# 2. Execution
df_trades = fetch_historical_trades(START_TS, END_TS)

# 3. Quick Clean for Quant Analysis
if not df_trades.empty:
    # Expand instrument name into Strike and Expiry
    # Format: BTC-27MAR26-65000-C
    df_trades['strike'] = df_trades['instrument_name'].str.split('-').str[2].astype(float)
    df_trades['expiry'] = df_trades['instrument_name'].str.split('-').str[1]
    df_trades['type'] = df_trades['instrument_name'].str.split('-').str[3]
    
    df_trades.to_csv("data/deribit/deribit_btc_options_feb_mar_2026.csv", index=False)
    print("Download complete.")
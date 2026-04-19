import yfinance as yf
df = yf.download(tickers='GC=F', period='1d', interval='1m')
print("GC=F length:", len(df))
df2 = yf.download(tickers='XAUUSD=X', period='1d', interval='1m')
print("XAUUSD=X length:", len(df2))
if len(df2) > 0:
    print(df2.tail())

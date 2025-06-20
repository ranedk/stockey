# Bhavcopy

The daily bhavcopy has daily market data about price, volumes and trades.
The script downloads all bhavcopy zip from NSE.

# How to run:

## Run with real chrome and cdp

You will have to stop all running instances of chrome to be able to run the new instance in debugging mode. You can use `pkill chrome` to kill all instances.

OSX: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

OR

Ubuntu: `/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

## Run the bhavcopy downloader

```shell
python bhavcopy_downloader.py
```

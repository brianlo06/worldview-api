# Ops — launchd services

> Paths below use `/PATH/TO/worldview-api` as a placeholder. Replace it with the absolute
> path to your own checkout before copying the plists into `~/Library/LaunchAgents/`.


Two LaunchAgents run the backend hands-off:

| Label                   | Job                                                           |
|-------------------------|---------------------------------------------------------------|
| `com.worldview.api`     | FastAPI server on `127.0.0.1:8088`, KeepAlive (auto-restart). |
| `com.worldview.ingest`  | `scripts/run_all.py` every 15 minutes (GDELT + enrichment + weather + markets + currencies). |

## Install both

```bash
cp /PATH/TO/worldview-api/ops/com.worldview.api.plist    ~/Library/LaunchAgents/
cp /PATH/TO/worldview-api/ops/com.worldview.ingest.plist ~/Library/LaunchAgents/

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.worldview.api.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.worldview.ingest.plist
```

Both fire once on load, then `api` stays running forever and `ingest` re-fires every 900 s.

## Check status

```bash
launchctl print gui/$(id -u)/com.worldview.api    | head -30
launchctl print gui/$(id -u)/com.worldview.ingest | head -30

tail -f /PATH/TO/worldview-api/logs/api.log
tail -f /PATH/TO/worldview-api/logs/ingest.log
```

## Force an immediate run

```bash
launchctl kickstart -k gui/$(id -u)/com.worldview.ingest
launchctl kickstart -k gui/$(id -u)/com.worldview.api   # restart the API
```

## Stop / uninstall

```bash
launchctl bootout gui/$(id -u)/com.worldview.api
launchctl bootout gui/$(id -u)/com.worldview.ingest
rm ~/Library/LaunchAgents/com.worldview.api.plist
rm ~/Library/LaunchAgents/com.worldview.ingest.plist
```

## Manual run (without launchd)

```bash
cd /PATH/TO/worldview-api
.venv/bin/python scripts/serve.py        # API server
.venv/bin/python scripts/run_all.py      # one-shot ingestion
```

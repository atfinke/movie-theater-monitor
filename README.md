# Movie Theater Monitor

Polls a theater's showtime source and emails one alert when new showtimes matching a configured format appear. The initial deployment monitors IMAX listings at AMC Metreon 16, but the configuration is theater-agnostic.

## Configure

Set the theater name, listing source and URL, format filter, booking URL, state location, and SNS email topic in the deployment configuration. The monitor keeps its own state so each showtime is alerted once.

## Deploy locally

```sh
./scripts/deploy.sh \
  movie-theater-monitor \
  movie-theater-monitor-every-minute \
  movie-theater-monitor-hourly-full-rescan
```

The script packages and updates only this monitor's AWS resources. It requires an authenticated AWS CLI profile with access to the monitor's Lambda, scheduler, state store, and SNS topic.

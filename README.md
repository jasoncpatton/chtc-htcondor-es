
ElasticSearch upload for HTCondor data
----------------------------------------------

This package contains a set of scripts that assist in uploading data from
a HTCondor pool to ElasticSearch.  It queries for historical
job ClassAds, converts them to a JSON document, and uploads them to an
ElasticSearch instance.

## Configuration

The script reads from (in order) the current HTCondor configuration,
environment variables (prepended with `ES_PUSH_`), and the following
command-line arguments:

```plain
usage: es_push.py [-h]
                  [--checkpoint_file CHECKPOINT_FILE]
                  [--log_file LOG_FILE] [--log_level LOG_LEVEL]
                  [--threads THREADS] [--collectors COLLECTORS]
                  [--schedds SCHEDDS] [--startds STARTDS]
				  [--schedd_history] [--startd_history]
                  [--schedd_history_max_ads SCHEDD_HISTORY_MAX_ADS]
                  [--startd_history_max_ads STARTD_HISTORY_MAX_ADS]
                  [--schedd_history_timeout SCHEDD_HISTORY_TIMEOUT]
                  [--startd_history_timeout STARTD_HISTORY_TIMEOUT]
                  [--es_host ES_HOST] [--es_username ES_USERNAME]
                  [--es_use_https] [--es_timeout ES_TIMEOUT]
                  [--es_bunch_size ES_BUNCH_SIZE]
                  [--es_index_name ES_INDEX_NAME]
				  [--read_only] [--dry_run]

```

#!/usr/bin/env python
"""
Script for processing the contents of the CHTC pool.
"""

import time
import signal
import logging
import argparse
import multiprocessing

from . import history, queues, utils


def main_driver(args):
    """
    Driver method for the spider script.
    """
    starttime = time.time()

    signal.alarm(utils.TIMEOUT_MINS * 60 + 60)

    # Get all the schedd ads
    if args.process_schedd_history or args.process_schedd_queue:
        schedd_ads = []
        schedd_ads = utils.get_schedds(args)
        logging.warning("&&& There are %d schedds to query.", len(schedd_ads))

    # Get all the startd ads
    if args.process_startd_history:
        startd_ads = []
        startd_ads = utils.get_startds(args)
        logging.warning("&&& There are %d startds to query.", len(startd_ads))

    with multiprocessing.Pool(processes=args.process_parallel_queries, maxtasksperchild=1) as pool:
        metadata = utils.collect_metadata()

        if args.process_schedd_history:
            history.process_histories(
                schedd_ads=schedd_ads,
                starttime=starttime,
                pool=pool,
                args=args,
                metadata=metadata,
            )

        # Now that we have the fresh history, process the queues themselves.
        if args.process_schedd_queue:
            queues.process_queues(
                schedd_ads=schedd_ads,
                starttime=starttime,
                pool=pool,
                args=args,
                metadata=metadata,
            )

        if args.process_startd_history:
            history.process_histories(
                startd_ads=startd_ads,
                starttime=starttime,
                pool=pool,
                args=args,
                metadata=metadata,
            )

    logging.warning(
        "@@@ Total processing time: %.2f mins", ((time.time() - starttime) / 60.0)
    )

    return 0


def main():
    """
    Main method for the spider script.

    Parses arguments and invokes main_driver
    """
    defaults = utils.default_config()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config_file",
        dest="config_file",
        help=(
            "File containing configuration for the spider script. "
            "Config file values will be overridden by commandline arguments."
        ),
    )
    parser.add_argument(
        "--collectors",
        dest="collectors",
        help="Comma-separated list of Collector addresses used to locate Schedds/Startds",
    )
    parser.add_argument(
        "--schedds",
        dest="schedds",
        help=(
            "Comma-separated list of Schedd names to process "
            "[default is to process all Schedds located by Collectors]"
        ),
    )
    parser.add_argument(
        "--startds",
        dest="startds",
        help=(
            "Comma-separated list of Startd machines to process "
            "[default is to process all Startds located by Collectors]"
        ),
    )
    parser.add_argument(
        "--process_schedd_history",
        action="store_const",
        const=False,
        dest="process_schedd_history",
        help="Process Schedd history"
    )
    parser.add_argument(
        "--process_schedd_queue",
        action="store_const",
        const=True,
        dest="process_schedd_queue",
        help="Process Schedd queue (Running/Idle/Pending jobs)",
    )
    parser.add_argument(
        "--process_startd_history",
        action="store_const",
        const=True,
        dest="process_startd_history",
        help="Process Startd history"
    )
    parser.add_argument(
        "--process_max_documents",
        type=int,
        dest="process_max_documents",
        help=(
            "Abort after this many documents (per Schedd). "
            f"[default: {defaults['process_max_documents']} (process all)]"
        ),
    )
    parser.add_argument(
        "--process_parallel_queries",
        type=int,
        dest="process_parallel_queries",
        help=(
            "Number of parallel processes for querying "
            f"[default: {defaults['process_parallel_queries']}]"
        ),
    )
    parser.add_argument(
        "--es_host",
        dest="es_host",
        help=(
            "Host of the Elasticsearch instance to be used "
            f"[default: {defaults['es_host']}]"
        ),
    )
    parser.add_argument(
        "--es_port",
        type=int,
        dest="es_port",
        help=(
            "Port of the Elasticsearch instance to be used "
            f"[default: {defaults['es_port']}]"
        )
    )
    parser.add_argument(
        "--es_bunch_size",
        type=int,
        dest="es_bunch_size",
        help=(
            "Send docs to ES in bunches of this number "
            f"[default: {defaults['es_bunch_size']}]"
        )
    )
    parser.add_argument(
        "--es_feed_schedd_history",
        action="store_const",
        const=True,
        dest="es_feed_schedd_history",
        help=(
            "Feed Schedd history to Elasticsearch "
            f"[default: {defaults['es_feed_schedd_history']}]"
        )
    )
    parser.add_argument(
        "--es_feed_schedd_queue",
        action="store_const",
        const=True,
        dest="es_feed_schedd_queue",
        help=(
            "Feed Schedd queue to Elasticsearch "
            f"[default: {defaults['es_feed_schedd_queue']}]"
        )
    )
    parser.add_argument(
        "--es_feed_startd_history",
        action="store_const",
        const=True,
        dest="es_feed_startd_history",
        help=(
            "Feed Startd history to Elasticsearch "
            f"[default: {defaults['es_feed_startd_history']}]"
        )
    )
    parser.add_argument(
        "--es_index_name",
        dest="es_index_name",
        help=(
            "Trunk of Elasticsearch index name "
            f"[default: {defaults['es_index_name']}]"
        ),
    )
    parser.add_argument(
        "--es_index_date_attr",
        dest="es_index_date_attr",
        help=(
            "Job attribute to use as date for Elasticsearch index name "
            "[default: {defaults['es_index_date_attr']}]"
        ),
    )

    parser.add_argument(
        "--log_dir",
        default="log/",
        type=str,
        dest="log_dir",
        help=(
            "Directory for logging information "
            "[default: %(default)s]"
        ),
    )
    parser.add_argument(
        "--log_level",
        default="WARNING",
        type=str,
        dest="log_level",
        help=(
            "Log level (CRITICAL/ERROR/WARNING/INFO/DEBUG) "
            "[default: %(default)s]"
        ),
    )
    parser.add_argument(
        "--email_alerts",
        default=[],
        action="append",
        dest="email_alerts",
        help=(
            "Email addresses for alerts "
            "[default: none]"
        ),
    )
    parser.add_argument(
        "--read_only",
        action="store_true",
        dest="read_only",
        help="Only read the info, don't submit it.",
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        dest="dry_run",
        help=(
            "Don't even read info, just pretend to. (Still "
            "query the collector for the Schedds though.)"
        ),
    )

    args = parser.parse_args()
    args = utils.load_config(args)
    utils.set_up_logging(args)

    # --dry_run implies read_only
    args.read_only = args.read_only or args.dry_run

    main_driver(args)


if __name__ == "__main__":
    main()

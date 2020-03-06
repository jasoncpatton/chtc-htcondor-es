"""
Methods for processing the history in a schedd queue.
"""

import json
import time
import logging
import datetime
import traceback
import multiprocessing

import classad
import htcondor
import elasticsearch

from . import elastic, utils, convert


def process_schedd(
    start_time, last_completion, checkpoint_queue, schedd_ad, args, metadata=None
):
    """
    Given a schedd, process its entire set of history since last checkpoint.
    """
    my_start = time.time()
    if utils.time_remaining(start_time) < 0:
        message = (
            "No time remaining to process %s history; exiting." % schedd_ad["Name"]
        )
        logging.error(message)
        utils.send_email_alert(
            args.email_alerts, "spider history timeout warning", message
        )
        return last_completion

    metadata = metadata or {}
    schedd = htcondor.Schedd(schedd_ad)
    history_query = classad.ExprTree(f"( EnteredCurrentStatus >= {int(last_completion)} )")
    logging.info(
        "Querying %s for history: %s.  " "%.1f minutes of ads",
        schedd_ad["Name"],
        history_query,
        (time.time() - last_completion) / 60.0,
    )
    buffered_ads = {}
    count = 0
    total_upload = 0
    sent_warnings = False
    timed_out = False
    if not args.read_only and args.feed_es:
        es = elastic.get_server_handle(args)
    try:
        if not args.dry_run:
            history_iter = schedd.history(history_query, [], 10000)
        else:
            history_iter = []

        for job_ad in history_iter:
            try:
                dict_ad = convert.to_json(job_ad, return_dict=True)
            except Exception as e:
                message = f"Failure when converting document on {schedd_ad['Name']} history: {e}"
                exc = traceback.format_exc()
                message += f"\n{exc}"
                logging.warning(message)
                if not sent_warnings:
                    utils.send_email_alert(
                        args.email_alerts,
                        "spider history document conversion error",
                        message,
                    )
                    sent_warnings = True

                continue

            idx = elastic.get_index(
                job_ad["QDate"],
                template=args.es_index_template,
                update_es=(args.feed_es and not args.read_only),
            )
            ad_list = buffered_ads.setdefault(idx, [])
            ad_list.append((convert.unique_doc_id(dict_ad), dict_ad))

            if len(ad_list) == args.es_bunch_size:
                st = time.time()
                if not args.read_only and args.feed_es:
                    elastic.post_ads(es.handle, idx, ad_list, metadata=metadata)
                logging.debug(
                    "...posting %d ads from %s (process_schedd)",
                    len(ad_list),
                    schedd_ad["Name"],
                )
                total_upload += time.time() - st
                buffered_ads[idx] = []

            count += 1

            # Find the most recent job and use that date as the new
            # last_completion date
            job_completion = job_ad.get("EnteredCurrentStatus")
            if job_completion > last_completion:
                last_completion = job_completion

            if utils.time_remaining(start_time) < 0:
                message = f"History crawler on {schedd_ad['Name']} has been running for more than {utils.TIMEOUT_MINS:d} minutes; exiting."
                logging.error(message)
                utils.send_email_alert(
                    args.email_alerts, "spider history timeout warning", message
                )
                timed_out = True
                break

            if args.max_documents_to_process and count > args.max_documents_to_process:
                logging.warning(
                    "Aborting after %d documents (--max_documents_to_process option)"
                    % args.max_documents_to_process
                )
                break

    except RuntimeError:
        message = "Failed to query schedd for job history: %s" % schedd_ad["Name"]
        exc = traceback.format_exc()
        message += f"\n{exc}"
        logging.error(message)

    except Exception as exn:
        message = f"Failure when processing schedd history query on {schedd_ad['Name']}: {str(exn)}"
        exc = traceback.format_exc()
        message += f"\n{exc}"
        logging.exception(message)
        utils.send_email_alert(
            args.email_alerts, "spider schedd history query error", message
        )

    # Post the remaining ads
    for idx, ad_list in list(buffered_ads.items()):
        if ad_list:
            logging.debug(
                "...posting remaining %d ads from %s " "(process_schedd)",
                len(ad_list),
                schedd_ad["Name"],
            )
            if not args.read_only:
                if args.feed_es:
                    elastic.post_ads(es.handle, idx, ad_list, metadata=metadata)

    total_time = (time.time() - my_start) / 60.0
    total_upload /= 60.0
    last_formatted = datetime.datetime.fromtimestamp(last_completion).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    logging.warning(
        "Schedd %-25s history: response count: %5d; last completion %s; query time %.2f min; upload time %.2f min",
        schedd_ad["Name"],
        count,
        last_formatted,
        total_time - total_upload,
        total_upload,
    )

    # If we got to this point without a timeout, all these jobs have
    # been processed and uploaded, so we can update the checkpoint
    if not timed_out:
        checkpoint_queue.put((schedd_ad["Name"], last_completion))

    return last_completion


def load_checkpoint():
    try:
        with open("checkpoint.json", "r") as fd:
            checkpoint = json.load(fd)
    except IOError:
        checkpoint = {}

    return checkpoint


def update_checkpoint(name, completion_date):
    checkpoint = load_checkpoint()

    checkpoint[name] = completion_date

    with open("checkpoint.json", "w") as fd:
        json.dump(checkpoint, fd)


def process_histories(schedd_ads, starttime, pool, args, metadata=None):
    """
    Process history files for each schedd listed in a given
    multiprocessing pool
    """
    checkpoint = load_checkpoint()

    futures = []
    metadata = metadata or {}
    metadata["spider_source"] = "condor_history"

    manager = multiprocessing.Manager()
    checkpoint_queue = manager.Queue()

    for schedd_ad in schedd_ads:
        name = schedd_ad["Name"]

        # Check for last completion time
        # If there was no previous completion, get last 12 h
        last_completion = checkpoint.get(name, time.time() - 12 * 3600)

        future = pool.apply_async(
            process_schedd,
            (starttime, last_completion, checkpoint_queue, schedd_ad, args, metadata),
        )
        futures.append((name, future))

    def _chkp_updater():
        while True:
            try:
                job = checkpoint_queue.get()
                if job is None:  # Swallow poison pill
                    break
            except EOFError as error:
                logging.warning(
                    "EOFError - Nothing to consume left in the queue %s", error
                )
                break
            update_checkpoint(*job)

    chkp_updater = multiprocessing.Process(target=_chkp_updater)
    chkp_updater.start()

    # Check whether one of the processes timed out and reset their last
    # completion checkpoint in case
    timed_out = False
    for name, future in futures:
        if utils.time_remaining(starttime) > -10:
            try:
                future.get(utils.time_remaining(starttime) + 10)
            except multiprocessing.TimeoutError:
                # This implies that the checkpoint hasn't been updated
                message = "Schedd %s history timed out; ignoring progress." % name
                exc = traceback.format_exc()
                message += f"\n{exc}"
                logging.error(message)
                utils.send_email_alert(
                    args.email_alerts, "spider history timeout warning", message
                )
            except elasticsearch.exceptions.TransportError:
                message = (
                    "Transport error while sending history data of %s; ignoring progress."
                    % name
                )
                exc = traceback.format_exc()
                message += f"\n{exc}"
                logging.error(message)
                utils.send_email_alert(
                    args.email_alerts, "spider history transport error warning", message
                )
        else:
            timed_out = True
            break
    if timed_out:
        pool.terminate()

    checkpoint_queue.put(None)  # Send a poison pill
    chkp_updater.join()

    logging.warning(
        "Processing time for history: %.2f mins", ((time.time() - starttime) / 60.0)
    )
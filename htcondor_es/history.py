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

_LAUNCH_TIME = int(time.time())


def index_time(index_attr, ad):
    """
    Returns
        - user-preferred timestamp attr if present
        - else EnteredCurrentStatus if present
        - else QDate if present
        - else fall back to launch time
    """
    try:
        if int(ad.get(index_attr, 0)) > 0:
            return ad[index_attr]
    except (ValueError, TypeError):
        logging.error(f"The value of {index_attr} is not numeric and cannot be used as a timestamp, falling back to EnteredCurrentStatus")

    if ad.get("EnteredCurrentStatus", 0) > 0:
        return ad["EnteredCurrentStatus"]

    if ad.get("QDate", 0) > 0:
        return ad["QDate"]

    return _LAUNCH_TIME


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
    if not args.read_only and args.es_feed_schedd_history:
        es = elastic.get_server_handle(args)
    try:
        if not args.dry_run:
            history_iter = schedd.history(history_query, [], max(10000, args.process_max_documents))
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
                index_time(args.es_index_date_attr, job_ad),
                template=args.es_index_name,
                update_es=(args.es_feed_schedd_history and not args.read_only),
            )
            ad_list = buffered_ads.setdefault(idx, [])
            ad_list.append((convert.unique_doc_id(dict_ad), dict_ad))

            if len(ad_list) == args.es_bunch_size:
                st = time.time()
                if not args.read_only and args.es_feed_schedd_history:
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

            if args.process_max_documents and count > args.process_max_documents:
                logging.warning(
                    "Aborting after %d documents (--process_max_documents option)"
                    % args.process_max_documents
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
                if args.es_feed_schedd_history:
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

def process_startd(
    start_time, since, checkpoint_queue, startd_ad, args, metadata=None
):
    """
    Given a startd, process its entire set of history since last checkpoint.
    """
    last_completion = since["EnteredCurrentStatus"]
    since_str = f"""(GlobalJobId == "{since['GlobalJobId']}") && (EnteredCurrentStatus == {since['EnteredCurrentStatus']})"""
    my_start = time.time()
    if utils.time_remaining(start_time) < 0:
        message = (
            "No time remaining to process %s history; exiting." % startd_ad["Machine"]
        )
        logging.error(message)
        utils.send_email_alert(
            args.email_alerts, "spider history timeout warning", message
        )
        return since

    metadata = metadata or {}
    startd = htcondor.Startd(startd_ad)
    logging.info(
        "Querying %s for history",
        startd_ad["Machine"]
    )
    buffered_ads = {}
    count = 0
    total_upload = 0
    sent_warnings = False
    timed_out = False
    if not args.read_only and args.es_feed_startd_history:
        es = elastic.get_server_handle(args)
    try:
        if not args.dry_run:
            history_iter = startd.history("True", [], since=since_str)
        else:
            history_iter = []

        for job_ad in history_iter:
            try:
                dict_ad = convert.to_json(job_ad, return_dict=True)
            except Exception as e:
                message = f"Failure when converting document on {startd_ad['Machine']} history: {e}"
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
                index_time(args.es_index_date_attr, job_ad),
                template=args.es_index_name,
                update_es=(args.es_feed_startd_history and not args.read_only),
            )
            ad_list = buffered_ads.setdefault(idx, [])
            ad_list.append((convert.unique_doc_id(dict_ad), dict_ad))

            if len(ad_list) == args.es_bunch_size:
                st = time.time()
                if not args.read_only and args.es_feed_startd_history:
                    elastic.post_ads(es.handle, idx, ad_list, metadata=metadata)
                logging.debug(
                    "...posting %d ads from %s (process_startd)",
                    len(ad_list),
                    startd_ad["Machine"],
                )
                total_upload += time.time() - st
                buffered_ads[idx] = []

            count += 1

            job_completion = job_ad.get("EnteredCurrentStatus")
            if job_completion > last_completion:
                last_completion = job_completion
                since = {
                    "GlobalJobId": job_ad.get("GlobalJobId"),
                    "EnteredCurrentStatus": job_ad.get("EnteredCurrentStatus"),
                }

            if utils.time_remaining(start_time) < 0:
                message = f"History crawler on {startd_ad['Machine']} has been running for more than {utils.TIMEOUT_MINS:d} minutes; exiting."
                logging.error(message)
                utils.send_email_alert(
                    args.email_alerts, "spider history timeout warning", message
                )
                timed_out = True
                break

            if args.process_max_documents and count > args.process_max_documents:
                logging.warning(
                    "Aborting after %d documents (--process_max_documents option)"
                    % args.process_max_documents
                )
                break

    except RuntimeError:
        message = "Failed to query startd for job history: %s" % startd_ad["Machine"]
        exc = traceback.format_exc()
        message += f"\n{exc}"
        logging.error(message)

    except Exception as exn:
        message = f"Failure when processing startd history query on {startd_ad['Machine']}: {str(exn)}"
        exc = traceback.format_exc()
        message += f"\n{exc}"
        logging.exception(message)
        utils.send_email_alert(
            args.email_alerts, "spider startd history query error", message
        )

    # Post the remaining ads
    for idx, ad_list in list(buffered_ads.items()):
        if ad_list:
            logging.debug(
                "...posting remaining %d ads from %s " "(process_startd)",
                len(ad_list),
                startd_ad["Machine"],
            )
            if not args.read_only:
                if args.es_feed_startd_history:
                    elastic.post_ads(es.handle, idx, ad_list, metadata=metadata)

    total_time = (time.time() - my_start) / 60.0
    total_upload /= 60.0
    last_formatted = datetime.datetime.fromtimestamp(last_completion).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    logging.warning(
        "Startd %-25s history: response count: %5d; last completion %s; query time %.2f min; upload time %.2f min",
        startd_ad["Machine"],
        count,
        last_formatted,
        total_time - total_upload,
        total_upload,
    )

    # If we got to this point without a timeout, all these jobs have
    # been processed and uploaded, so we can update the checkpoint
    if not timed_out:
        checkpoint_queue.put((startd_ad["Machine"], since))

    return since


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
        json.dump(checkpoint, fd, indent=4)


def process_histories(schedd_ads = [], startd_ads = [],
                          starttime = None, pool = None, args = None, metadata = None):
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

    if len(schedd_ads) > 0:
        for schedd_ad in schedd_ads:
            name = schedd_ad["Name"]

            # Check for last completion time
            # If there was no previous completion, get full history
            last_completion = checkpoint.get(name, 0)

            future = pool.apply_async(
                process_schedd,
                (starttime, last_completion, checkpoint_queue, schedd_ad, args, metadata),
            )
            futures.append((name, future))

    if len(startd_ads) > 0:
        for startd_ad in startd_ads:
            machine = startd_ad["Machine"]

            # Check for last completion time ("since")
            since = checkpoint.get(machine, {"GlobalJobId": "Unknown", "EnteredCurrentStatus": 0})

            future = pool.apply_async(
                process_startd,
                (starttime, since, checkpoint_queue, startd_ad, args, metadata),
            )
            futures.append((machine, future))
            

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

    # Check if the entire pool and/or one of the processes has timed out
    # Timeout is currently hardcoded to 11 minutes in utils.py
    timed_out = False
    for name, future in futures:
        # Allow a 30 second buffer for processes to finish
        if utils.time_remaining(starttime, positive=False) > -30:
            try:
                # Each process gets a minimum of 10 seconds to produce output
                future.get(utils.time_remaining(starttime) + 10)
            except multiprocessing.TimeoutError:
                # This implies that the checkpoint hasn't been updated
                message = "Daemon %s history timed out; ignoring progress." % name
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
            logging.error("Processing the entire queue took too long, stopping early")
            break
    if timed_out:
        pool.terminate()

    checkpoint_queue.put(None)  # Send a poison pill
    chkp_updater.join()

    logging.warning(
        "Processing time for history: %.2f mins", ((time.time() - starttime) / 60.0)
    )

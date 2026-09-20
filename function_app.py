import logging

import azure.functions as func

import flats

app = func.FunctionApp()

# 2.5 min needs two expressions: NCRONTAB cannot express a 150-second period.
# One fires on the minute every 5, the other 2m30s later. flats.main() no-ops
# during the snooze window, so these run all day and stay quiet overnight.
ON_THE_MINUTE = "0 */5 * * * *"
ON_THE_HALF = "30 2,7,12,17,22,27,32,37,42,47,52,57 * * * *"


def _run(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("timer past due - catching up a slot missed while the host was down")
    flats.main()


@app.timer_trigger(schedule=ON_THE_MINUTE, arg_name="timer", use_monitor=True)
def poll(timer: func.TimerRequest) -> None:
    _run(timer)


@app.timer_trigger(schedule=ON_THE_HALF, arg_name="timer", use_monitor=True)
def poll_offset(timer: func.TimerRequest) -> None:
    _run(timer)

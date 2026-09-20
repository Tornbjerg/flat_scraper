import logging

import azure.functions as func

import flats

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */10 * * * *", arg_name="timer", use_monitor=True)
def poll(timer: func.TimerRequest) -> None:
    """Every 10 min. main() also sends the daily digest once past the cutoff."""
    if timer.past_due:
        logging.warning("timer past due - catching up a slot missed while the host was down")
    flats.main()

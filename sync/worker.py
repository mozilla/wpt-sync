import celery
from celery.schedules import crontab
from celery.signals import after_setup_logger


@after_setup_logger.connect
def config_loggers(*args, **kwags):
    # This prevents celery reconfiguring the logging
    from . import log
    log.setup(force=True)


beat_schedule = {
    # Try to retrigger anything we missed once a day
    'retrigger': {
        "task": "sync.tasks.retrigger",
        "schedule": crontab(hour="8", minute="0"),
    },
    # Try to cleanup once an hour
    'cleanup': {
        "task": "sync.tasks.cleanup",
        "schedule": 3600,
    },
    # Try to update metadata once a day
    'update_bugs': {
        "task": "sync.tasks.update_bugs",
        "schedule": crontab(hour="9", minute="0"),
    }
}

worker = celery.Celery('sync',
                       broker='pyamqp://guest:guest@rabbitmq',
                       include=['sync.tasks'])

worker.conf.beat_schedule = beat_schedule
worker.conf.broker_transport_options = {
    "max_retries": 1,
}

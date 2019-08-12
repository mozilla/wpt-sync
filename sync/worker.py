import celery
from celery.beat import crontab

beat_schedule = {
    # Try to retrigger anything we missed once a day
    'retrigger': {
        "task": "sync.tasks.retrigger",
        "schedule": crontab(hour=8, minute=0),
    },
    # Try to cleanup once an hour
    'cleanup': {
        "task": "sync.tasks.cleanup",
        "schedule": 3600,
    }
}

worker = celery.Celery('sync',
                       broker='pyamqp://guest:guest@rabbitmq',
                       include=['sync.tasks'])

worker.conf.worker_hijack_root_logger = False
worker.conf.beat_schedule = beat_schedule
worker.conf.broker_transport_options = {
    "max_retries": 1,
}

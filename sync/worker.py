import celery
from celery.schedules import crontab

import repos
import settings
import repos

beat_schedule = {
    'attempt-landing': {
        "task": "sync.tasks.land",
        "schedule": crontab(hour=10, minute=30),
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

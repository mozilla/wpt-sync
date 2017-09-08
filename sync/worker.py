import celery
from celery.schedules import crontab

import settings

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

config = settings.load()
worker = celery.Celery('sync',
                       broker='pyamqp://guest:guest@rabbitmq',
                       include=['sync.tasks'])


worker.conf.beat_schedule = beat_schedule
worker.conf.update(**config["celery"])

if __name__ == "__main__":
    worker.start()

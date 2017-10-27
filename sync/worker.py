import celery
from celery.schedules import crontab

import repos
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

worker = celery.Celery('sync',
                       broker='pyamqp://guest:guest@rabbitmq',
                       include=['sync.tasks'])
worker.conf.beat_schedule = beat_schedule

if __name__ == "__main__":
    config = settings.load()
    worker.conf.update(**config["celery"])
    repos.configure(config)
    worker.start()

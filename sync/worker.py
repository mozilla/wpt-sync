import celery

beat_schedule = {
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

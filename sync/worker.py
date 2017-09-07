import celery

worker = celery.Celery('sync',
                       broker='pyamqp://guest:guest@rabbitmq',
                       include=['sync.tasks'])


if __name__ == "__main__":
    worker.start()

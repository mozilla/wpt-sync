import Queue
import threading


class Worker(threading.Thread):
    def __init__(self, queue, init_fn, work_fn, errors):
        super(Worker, self).__init__()
        self.daemon = True
        self.queue = queue
        self.init_fn = init_fn
        self.work_fn = work_fn
        self.errors = errors

    def run(self):
        if self.init_fn:
            init_data = self.init_fn()
        else:
            init_data = {}

        while True:
            try:
                task_data = self.queue.get(False)
            except Queue.Empty:
                return

            if task_data is None:
                return

            args, task_kwargs = task_data
            kwargs = init_data.copy() if init_data is not None else {}
            kwargs.update(task_kwargs)
            try:
                self.work_fn(*args, **kwargs)
            except Exception as e:
                self.errors.append(e)
            finally:
                self.queue.task_done()


class ThreadExecutor(object):
    """Simple executor that runs a single function on multiple threads with
    a list of arguments.

    :param thread_count: Number of threads to use
    :param work_fn: Callable that does the actual work. This is called once per data item.
    :param init_fn: Optional function that's called once per thread. The return value can
                    be a dict of values to pass in to the work_fn."""

    def __init__(self, thread_count, work_fn, init_fn=None):
        self.thread_count = thread_count
        self.work_fn = work_fn
        self.init_fn = init_fn

    def run(self, data):
        """Run the executor with the given data. Returns a list of exceptions that
        occured

        :param data: List of (args, kwargs) to pass to the work_fn, where args is a
        tuple and kwargs is a dict."""
        work_queue = Queue.Queue()
        for item in data:
            work_queue.put(item)

        errors = []

        workers = []
        for i in xrange(self.thread_count):
            workers.append(Worker(work_queue,
                                  self.init_fn,
                                  self.work_fn,
                                  errors))
            workers[-1].start()
        for item in workers:
            item.join()

        return errors

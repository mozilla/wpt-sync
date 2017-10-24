import inspect
import traceback

import log
from model import session_scope

logger = log.get_logger(__name__)


class AbortError(Exception):
    def __init__(self, msg, cleanup=None, set_flag=None):
        Exception.__init__(self, msg)
        self.cleanup = cleanup
        self.set_flag = set_flag


class MultipleExceptions(Exception):
    def __init__(self, items):
        self.items = items


def pipeline(f):
    def inner(config, session, *args, **kwargs):
        try:
            try:
                f(config, session, *args, **kwargs)
            except MultipleExceptions as e:
                exceptions = e.items
            except Exception as e:
                exceptions = [e]
            else:
                session.commit()
                return True

            session.rollback()

            unhandled = []
            for e in exceptions:
                if isinstance(e, AbortError):
                    logger.debug(traceback.format_exc(e))
                else:
                    logger.critical(traceback.format_exc(e))
                    unhandled.append(e)
            if unhandled:
                raise exceptions[0]
            return False
        finally:
            session.expire_all()

    return inner


class step(object):
    def __init__(self, state_arg=None, skip_if=None):
        assert not callable(state_arg)
        self.state_arg = state_arg
        self.skip_if = skip_if

    def __call__(self, f):
        self.inner = f

        f_args = inspect.getargspec(f).args
        if self.state_arg:
            assert self.state_arg in f_args
        if self.skip_if:
            assert self.state_arg is not None

        assert f_args[0] == "config"
        assert f_args[1] == "session"

        return self.run

    def run(self, config, session, *args, **kwargs):
        logger.debug("Running step %s" % self.inner.__name__)
        state_obj = (self.get_state_obj(*args, **kwargs)
                     if self.state_arg else None)
        if self.skip_if and self.skip_if(state_obj):
            return

        with session_scope(session):
            try:
                rv = self.inner(config, session, *args, **kwargs)
                return rv
            except AbortError as e:
                # Simplistic approach to cleanup, just provide a
                # list of objects that can be deleted
                session.rollback()
                if e.set_flag:
                    assert state_obj
                    if not isinstance(e.set_flag, list):
                        flags = [e.set_flag]
                    else:
                        flags = e.set_flag
                    for flag in flags:
                        if isinstance(flag, tuple):
                            flag, value = flag
                        else:
                            value = True
                        setattr(state_obj, flag, value)
                if e.cleanup:
                    for item in e.cleanup:
                        session.delete(item)
                session.commit()
                raise

    def get_state_obj(self, *args, **kwargs):
        called = inspect.getargvalues(inspect.currentframe()).locals
        func_args = inspect.getargspec(self.inner).args
        called_args = called["args"]
        called_kwargs = called["kwargs"]

        if self.state_arg in called_kwargs:
            return called_kwargs[self.state_arg]

        # Otherwise:
        # Chop off [config, session]
        func_args = func_args[2:]
        # Remove all the args that were called as kwargs
        for item in called_kwargs.iterkeys():
            func_args.remove(item)

        return called_args[func_args.index(self.state_arg)]

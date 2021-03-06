import os
import logging
import resource
import subprocess
import sys
import time

from django.db import transaction
from django.utils.timezone import now as utcnow

from . import models

QUEUE_LIMIT = 1
LIMIT_SEC = 60

logger = logging.getLogger(__name__)

def run(which=None):
    """Look through queue and run anything that needs to be.

    This is the queuerun management command."""
    if which:
        spawn_queue()
        return False

    while True:

        if queue_full():
            # Queue is full.  According to our semantics, we don't
            # need to poll or anyting.  After every job finishes, it
            # will start the next job.
            logger.debug("queue.py: Queue is full")
            return False

        # Find the job that should be run next.
        next = queue_next()
        if next is None:
            # No objects left to run.
            logger.debug("queue.py: No objects are queued")
            return False

        if which:
            # We have to run only one item.  Return after attempting
            # to run this item, True if run and False if not.
            # Regardless, after this run, if we are at this point then
            # we have the ability to run something in the queue.
            # Spawn a child process to run it.
            logger.debug("queue.py: Requested to run %s", which)
            if which != next:
                logger.debug("queue.py: Requested CD %s is not next in queue, spawning queuerunner", which)
                spawn_queue()
                return False

            # Run `which` in current process and block for its completion.
            logger.debug("queue.py: Running requested CD %s.", which)
            ret = runCD(which)
            logger.debug("queue.py: Done running requested CD %s.", which)

            # Done running the passed object.  Fork to start new queue runner, and return
            logger.debug("queue.py: Spawning queuerunner.")
            if not queue_empty():
                spawn_queue()
            return ret

        runCD(next)

def queue_full():
    """Return True if queue is full (no more runners should be launched)"""
    if models.CD.objects.filter(state='R').count() < QUEUE_LIMIT:
        return False
    return True
def queue_empty():
    """Return True if queue is full (no more runners should be launched)"""
    if models.CD.objects.filter(state='R').exists():
        return True
    return False
def queue_next():
    """Return the next object in the queue"""
    #CD.objects.filter(state='Q').order_by('qtime').first()  # django 1.6 feature
    try:
        return models.CD.objects.filter(state='Q').order_by('qtime')[0]
    except IndexError:
        return None
def run_queue_command():
    """Return the command that must be run in order to initialize a queue runner."""
    return [sys.executable, sys.executable,
            '/srv/jako/manage.py', 'queuerun']
def spawn_queue():
    return os.spawnl(os.P_NOWAIT, *run_queue_command())


def runCD(cd):
    """Run a single CD method and return.

    Use `run()` as the main entry point."""
    logger.debug("runCD: %s", cd)
    def preexec():
        resource.setrlimit(resource.RLIMIT_CPU, (LIMIT_SEC, LIMIT_SEC))

    #with transaction.atomic():
    if cd.state != 'Q':
        raise RuntimeError("This CD is not in state queued! (%s)"%cd)
    cd.state = 'R'
    cd.save()

    # Fork and run in child process, for safety (any Python segfaults
    # or exceptions are encapsulated)
    pid = os.fork()
    if pid == 0:
        # child process
        preexec()
        try:
            cd._run()
        except:
            type, value, tb = sys.exc_info()
            import traceback
            logger.error("queue.py: printing exception")
            logger.error("%s"%type)
            logger.error("%s"%value)
            logger.error("\n".join(traceback.format_tb(tb)))
            f = open(cd.basedir+'/jako-queue.stdout', 'w')
            #print >> f, "%s(%r)"%(type.__name__, value)
            print >> f, "%r"%value
            print >> f
            print >> f, "\n".join(traceback.format_tb(tb))
            f.close()
            cd.state = 'X'
            cd.dtime = utcnow()
            cd.save()
            os._exit(1)
        os._exit(0)  # exit after the fork
    # parent process
    _waited_pid, status = os.waitpid(pid, 0)
    signal = status % 256
    exitstatus = status // 256

    logger.debug("runCD: done running %s %s %s %s", cd, _waited_pid, signal, exitstatus)

    # We must get updated values from the database since it has been
    # modefied in another process.
    cd = models.CD.objects.get(id=cd.id)
    if exitstatus == 0:
        cd.state = 'D'
        cd.save()
        return True
    else:
        cd.state = 'X'
        cd.dtime = utcnow()
        cd.save()
        message = 'CD(%s) died, signal=%s, exitstatus=%s'%(cd.id, signal, exitstatus)
        #print message
        return False


if __name__ == '__main__':
    if sys.argv[1] == 'queuerun':
        run()

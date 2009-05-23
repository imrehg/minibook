#!/usr/bin/python

import time


# Adapted from
#  http://code.djangoproject.com/browser/django/trunk/django/utils/timesince.py
#  Modified that it takes GMT-zone UNIX time in seconds

def pluralize(singular, plural, count):
    if count == 1:
        return singular
    else:
        return plural


def timesince(d, now=None):
    """
    Takes two datetime objects and returns the time between then and now
    as a nicely formatted string, e.g "10 minutes"
    Adapted from http://blog.natbat.co.uk/archive/2003/Jun/14/time_since
    (Can check it in the Internet Archive)
    """
    chunks = (
        (60 * 60 * 24 * 365, lambda n: pluralize('year', 'years', n)),
        (60 * 60 * 24 * 30, lambda n: pluralize('month', 'months', n)),
        (60 * 60 * 24 * 7, lambda n: pluralize('week', 'weeks', n)),
        (60 * 60 * 24, lambda n: pluralize('day', 'days', n)),
        (60 * 60, lambda n: pluralize('hour', 'hours', n)),
        (60, lambda n: pluralize('minute', 'minutes', n)))
    # Convert time to UNIX seconds format for comparison
    if d.__class__ is not int:
        d = int(d)
    if not now:
        now = int(time.time())

    since = now - d
    if since <= 0:
        return 'moments'

    for i, (seconds, name) in enumerate(chunks):
        count = since / seconds
        if count != 0:
            break

    if count <= 0:
        return 'less then a minute'

    s = '%d %s' % (count, name(count))
    if i + 1 < len(chunks):
        # Now get the second item
        seconds2, name2 = chunks[i + 1]
        count2 = (since - (seconds * count)) / seconds2
        if count2 != 0:
            s += ', %d %s' % (count2, name2(count2))
    return s


def timeuntil(d, now=None):
    """
    Like timesince, but returns a string measuring the time until
    the given time.
    """
    if now == None:
        now = int(time.time())
    return timesince(now, d)

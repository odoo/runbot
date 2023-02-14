# -*- coding: utf-8 -*-
import itertools
import time


def shorten(text_ish, length):
    """ If necessary, cuts-off the text or bytes input and appends ellipsis to
    signal the cutoff, such that the result is below the provided length
    (according to whatever "len" means on the text-ish so bytes or codepoints
    or code units).
    """
    if len(text_ish or ()) <= length:
        return text_ish

    cont = '...'
    if isinstance(text_ish, bytes):
        cont = cont.encode('ascii') # whatever
    # add enough room for the ellipsis
    return text_ish[:length-3] + cont

BACKOFF_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6)
def backoff(func=None, *, delays=BACKOFF_DELAYS, exc=Exception):
    if func is None:
        return lambda func: backoff(func, delays=delays, exc=exc)

    for delay in itertools.chain(delays, [None]):
        try:
            return func()
        except exc:
            if delay is None:
                raise
            time.sleep(delay)

def make_message(pr_dict):
    title = pr_dict['title'].strip()
    body = (pr_dict.get('body') or '').strip()
    return f'{title}\n\n{body}' if body else title

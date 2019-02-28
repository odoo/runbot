# -*- coding: utf-8 -*-

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

class MergeError(Exception):
    pass
class FastForwardError(Exception):
    pass
class Skip(MergeError):
    pass

import collections
import hashlib

def make_obj(t, contents):
    assert t in ('blob', 'tree', 'commit')
    obj = b'%s %d\0%s' % (t.encode('utf-8'), len(contents), contents)
    return hashlib.sha1(obj).hexdigest(), obj

def make_blob(contents):
    return make_obj('blob', contents)

def make_tree(store, objs):
    """ objs should be a mapping or iterable of (name, object)
    """
    if isinstance(objs, collections.Mapping):
        objs = objs.items()

    return make_obj('tree', b''.join(
        b'%s %s\0%s' % (
            b'040000' if isinstance(obj, collections.Mapping) else b'100644',
            name.encode('utf-8'),
            h.encode('utf-8'),
        )
        for name, h in sorted(objs)
        for obj in [store[h]]
        # TODO: check that obj is a blob or tree
    ))

def make_commit(tree, message, author, committer=None, parents=()):
    contents = ['tree %s' % tree]
    for parent in parents:
        contents.append('parent %s' % parent)
    contents.append('author %s' % author)
    contents.append('committer %s' % committer or author)
    contents.append('')
    contents.append(message)

    return make_obj('commit', '\n'.join(contents).encode('utf-8'))

def walk_ancestors(store, commit, exclude_self=True):
    """
    :param store: mapping of hashes to commit objects (w/ a parents attribute)
    """
    q = [(commit, 0)]
    while q:
        node, distance = q.pop()
        q.extend((p, distance+1) for p in store[node].parents)
        if not (distance == 0 and exclude_self):
            yield (node, distance)

def is_ancestor(store, candidate, of):
    # could have candidate == of after all
    return any(
        current == candidate
        for current, _ in walk_ancestors(store, of, exclude_self=False)
    )


def merge_base(store, c1, c2):
    """ Find LCA between two commits. Brute-force: get all ancestors of A,
    all ancestors of B, intersect, and pick the one with the lowest distance
    """
    a1 = walk_ancestors(store, c1, exclude_self=False)
    # map of sha:distance
    a2 = dict(walk_ancestors(store, c2, exclude_self=False))
    # find lowest ancestor by distance(ancestor, c1) + distance(ancestor, c2)
    _distance, lca = min(
        (d1 + d2, a)
        for a, d1 in a1
        for d2 in [a2.get(a)]
        if d2 is not None
    )
    return lca

def merge_objects(store, b, o1, o2):
    """ Merges trees and blobs.

    Store = Mapping<Hash, (Blob | Tree)>
    Blob = bytes
    Tree = Mapping<Name, Hash>
    """
    # FIXME: handle None input (similarly named entry added in two
    #        branches, or delete in one branch & change in other)
    if not (b and o1 or o2):
        raise ValueError("Don't know how to merge additions/removals yet")
    b, o1, o2 = store[b], store[o1], store[o2]
    if any(isinstance(o, bytes) for o in [b, o1, o2]):
        raise TypeError("Don't know how to merge blobs")

    entries = sorted(set(b).union(o1, o2))

    t = {}
    for entry in entries:
        base = b.get(entry)
        e1 = o1.get(entry)
        e2 = o2.get(entry)
        if e1 == e2:
            merged = e1 # either no change or same change on both side
        elif base == e1:
            merged = e2 # e1 did not change, use e2
        elif base == e2:
            merged = e1 # e2 did not change, use e1
        else:
            merged = merge_objects(store, base, e1, e2)
        # None => entry removed
        if merged is not None:
            t[entry] = merged

    # FIXME: fix partial redundancy with make_tree
    tid, _ = make_tree(store, t)
    store[tid] = t
    return tid

def read_object(store, tid):
    # recursively reads tree of objects
    o = store[tid]
    if isinstance(o, bytes):
        return o
    return {
        k: read_object(store, v)
        for k, v in o.items()
    }

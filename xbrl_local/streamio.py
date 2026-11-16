"""Incremental, namespace-aware reading of one bounded local XML document.

The object-API parser in ``xmlio`` builds the whole tree; this module exposes the
same namespace/scope semantics one top-level child at a time so the file import
path can stay within a fixed memory budget.  The whole-file DTD/ENTITY guard and
the UTF-8 contract of ``xmlio.read_xml`` are preserved.
"""
import codecs
import re
import xml.etree.ElementTree as ET
from .errors import InputError

CHUNK = 1 << 16
# Keep a little overlap so a declaration split across chunks is still seen.
_OVERLAP = 4096
_DENY = re.compile(r'<!\s*(DOCTYPE|ENTITY)', re.I)
_EVENTS = ('start-ns', 'start', 'end')


def _child_from_event(event, obj, state, root_tag):
    """Apply one parser event; return the finished top-level child, if any.

    ``state`` holds the mutable scope stack, pending prefix bindings, the
    per-element scope map and the root element so both the streaming loop and
    the end-of-input flush share exactly one implementation.  The returned
    child is still intact: the caller consumes it, then releases the element.
    """
    scopes, stack, pending, holders = state
    if event == 'start-ns':
        pending.append(obj)
        return None
    if event == 'start':
        scope = dict(stack[-1]) if stack else {}
        scope.update(pending)
        pending.clear()
        scopes[obj] = scope
        stack.append(scope)
        if len(stack) == 1:
            if obj.tag != root_tag:
                raise InputError('unexpected XML root')
            holders[0] = obj
        return None
    stack.pop()
    if len(stack) != 1:
        return None
    return obj


def _release(child, scopes, root):
    for element in child.iter():
        scopes.pop(element, None)
    child.clear()
    # O(1) detach: drop the root's children list rather than scanning it.
    if root is not None:
        root.clear()


def stream_children(path, root_tag):
    """Yield ``(element, scopes)`` for each direct child of the document root.

    ``scopes`` maps every element of the current top-level subtree to its
    in-scope prefix mapping, exactly like ``xmlio.parse``.  The caller must
    finish using an element and its scopes before requesting the next item.
    """
    state = ({}, [], [], [None])
    with open(path, 'rb') as source:
        parser = ET.XMLPullParser(events=_EVENTS)
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        tail = ''
        try:
            while True:
                raw = source.read(CHUNK)
                final = not raw
                text = decoder.decode(raw, final)
                if text:
                    combined = tail + text
                    if _DENY.search(combined):
                        raise InputError(
                            'XML must be text without a DTD or entity declarations')
                    tail = combined[-_OVERLAP:]
                    parser.feed(text)
                for event, obj in parser.read_events():
                    child = _child_from_event(event, obj, state, root_tag)
                    if child is not None:
                        yield child, state[0]
                        _release(child, state[0], state[3][0])
                if final:
                    # Trailing events can be withheld until end of input; flush
                    # them through the same handler so no event is dropped.
                    parser.close()
                    for event, obj in parser.read_events():
                        child = _child_from_event(event, obj, state, root_tag)
                        if child is not None:
                            yield child, state[0]
                            _release(child, state[0], state[3][0])
                    break
        except ET.ParseError as exc:
            raise InputError('malformed XML') from exc
        except UnicodeError as exc:
            raise InputError('XML files must be UTF-8') from exc
        if state[3][0] is None or state[1]:
            raise InputError('malformed XML')

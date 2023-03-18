from __future__ import unicode_literals

import textwrap

from . import Member, Society, RESTRICTED


def summarise_member(member):
    """
    Returns a str summarising `member`

    Contains the member's details (name, crsid, email, status, join date,
    societies) in human-readable form.
    """

    if member.societies:
        socs = ["Societies:"] + _pretty_name_list((s.description, s.society) for s in member.societies)
    else:
        socs = ["No society memberships."]

    private = []
    if not RESTRICTED:
        private = ([member.email or '<no email>',
                    'Mail handler: %s' % member.mail_handler,
                    'Member: %s' % member.member,
                    'User: %s' % member.user,
                    'Joined: %s' % member.joined.strftime("%Y/%m"),
                    'UID: %s' % member.uid,
                    'GID: %s' % member.gid,
                    'Disk quota: %s/%s GB (updated: %s)' % (
                        member.disk_usage_gb, member.disk_quota_gb, member.disk_usage_updated
                    ),
                    'Danger: %s' % member.danger] +
                   _format_notes(member.notes) +
                   _format_domains(member.domains))

    lines = (
        ['%s (%s)' % (member.name or '<no name>', member.crsid)] +
        private +
        socs
    )

    return "\n".join(lines)


def summarise_society(society):
    """Returns a str summarising `society`"""
    if society.admins:
        admins = ["Admins:"] + _pretty_name_list((u.name, u.crsid) for u in society.admins)
    else:
        admins = ['Orphaned (no admins).']

    private = []
    if not RESTRICTED:
        if society.role_email:
            private.append('Role email: %s' % society.role_email)
        else:
            private.append('No role email.')
        private += (
            [
                'Joined: %s' % society.joined.strftime("%Y/%m"),
                'UID: %s' % society.uid,
                'GID: %s' % society.gid,
                'Disk quota: %s/%s GB (updated: %s)' % (
                    society.disk_usage_gb, society.disk_quota_gb, society.disk_usage_updated
                ),
                'Danger: %s' % society.danger
            ] +
            _format_notes(society.notes) +
            _format_domains(society.domains)
        )

    lines = (
        ['%s: %s' % (society.society, society.description)] +
        private +
        admins
    )

    return "\n".join(lines)


def summarise(thing):
    if isinstance(thing, Society):
        return summarise_society(thing)

    if isinstance(thing, Member):
        return summarise_member(thing)

    try:
        return "\n".join(_pretty_thing_list(thing))
    except TypeError:
        pass

    return str(thing)


def _pretty_thing_list(things):
    def f(thing):
        if isinstance(thing, Society):
            return thing.description, thing.society
        elif isinstance(thing, Member):
            return thing.name or '<no name>', thing.crsid
        else:
            raise TypeError("things should contain Societies and Members")

    return _pretty_name_list(map(f, things))


def _pretty_name_list(names):
    """
    Given a list of (a,b) pairs, output aligned columns with the
    items of the second column parenthised.

    Used for pretty-printing e.g. name (crsid) or socname (socid) lists.
    """
    # might be given an iterator, need a list, might as well sort it
    nameList = sorted(names)
    try:
        maxlen = max(len(col1) for (col1, col2) in nameList)
    except ValueError:  # empty sequence
        return ''

    return ['  %-*s  (%s)' % (maxlen, col1, col2)
            for (col1, col2) in nameList]


def _format_notes(notes):
    s = []
    if notes:
        tw = textwrap.TextWrapper(width=70, initial_indent='  ', subsequent_indent='  ')
        s.append("Notes:")
        for line in notes.splitlines():
            s += tw.wrap(line)
    return s


def _format_domains(domains):
    s = []
    if domains:
        s.append("Domains:")
        for d in domains:
            line = "  " + d.domain
            if d.wild:
                line += " (wild)"
            if d.root:
                line += " @ " + d.root
            s.append(line)
    return s

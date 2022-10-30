#!/usr/bin/python

"""
SRCF python library:

'argcomplete' functions for tab completion of specific groups

Usage:
  parser.add_argument('$FOO', ...).completer = complete_$FOO

Groups: complete_$GROUP()
  member, user, soc, activesoc, socadmin
"""

from subprocess import check_output, CalledProcessError
from argcomplete import warn as argcomplete_warn

from srcf import MEMBERLIST, SOCLIST


def complete_member(prefix, **kwargs):
    """
    Tabcomplete any member (has entry in the memberlist)
    """
    try:
        # Do not attempt to bother tabcompleting all crsids
        if prefix == "":
            return []

        out = check_output(
            ['grep', '-o', r'^%s\([^:]*\)' % (prefix,), MEMBERLIST])

        return out.split('\n')

    except Exception as e:
        argcomplete_warn("Error: " + e)
        return []


def complete_user(prefix, **kwargs):
    """
    Tabcomplete any user (member with 'user' status)
    """
    try:
        # Do not attempt to bother tabcompleting all crsids
        if prefix == "":
            return []

        out = check_output(
            ['grep', r'^%s[^:]*:' % (prefix,), MEMBERLIST])

        users = (x for x in out.split('\n') if ":user:" in x)
        return (x.split(':')[0] for x in users)

    except Exception as e:
        argcomplete_warn("Error: " + e)
        return []


def complete_soc(prefix, **kwargs):
    """
    Tabcomplete any society (has entry in the soclist)
    """
    try:
        # Do not attempt to bother tabcompleting all societies
        if prefix == "":
            return []

        out = check_output(
            ['grep', '-o', r'^%s\([^:]*\)' % (prefix,), SOCLIST])

        return out.split('\n')

    except Exception as e:
        argcomplete_warn("Error: " + e)
        return []


def complete_activesoc(prefix, **kwargs):
    """
    Tabcomplete any active society (society with admins)
    """
    try:
        # Do not attempt to bother tabcompleting all societies
        if prefix == "":
            return []

        out = check_output(
            ['grep', r'^%s[^:]*:' % (prefix,), SOCLIST])

        # "::" indicates that the admin list is empty
        active_socs = (x for x in out.split('\n') if "::" not in x)
        return (x.split(':')[0] for x in active_socs)

    except Exception as e:
        argcomplete_warn("Error: " + e)
        return []


def complete_socadmin(prefix, parsed_args, **kwargs):
    """
    Tabcomplete any society administrator.

    Designed to work when the parser already has a 'soc' option which has been
    completed to the appropriate society.  Without a valid 'soc' option, this
    degrades to complete_user()
    """
    try:
        try:
            if len(parsed_args.soc) > 0:
                socline = check_output(
                    ['grep', '-m1', r'^%s:' % (parsed_args.soc,), SOCLIST])

            #   No soc in args  Grep exited non-0
        except (AttributeError, CalledProcessError):
            # degrade to complete_user.
            return complete_user(prefix)

        admins = socline.split(':')[2].split(',')
        return (x for x in admins if x.startswith(prefix))

    except Exception as e:
        argcomplete_warn("Error: " + e)
        return []

# Local Variables:
# mode: python
# coding: utf-8
# tab-width: 4
# indent-tabs-mode: nil
# End:

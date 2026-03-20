"""
Scripts to manage sysadmins.
"""

from sqlalchemy.orm import Session

from srcf.database.schema import Member

from .utils import confirm, entrypoint, error
from ..plumbing import unix
from ..tasks import membership


@entrypoint
def create(sess: Session, member: Member):
    """
    Create a sysadmin account.

    Usage: {script} MEMBER
    """
    try:
        unix.get_user("{}-adm".format(member.crsid))
    except KeyError:
        pass
    else:
        error("Member {!r} already has an administrative account".format(member.crsid))
    confirm("Create {}'s administrative account?".format(member.name))
    confirm()
    membership.create_sysadmin(sess, member, True)


@entrypoint
def cancel(sess: Session, member: Member):
    """
    Cancel a sysadmin account.

    Usage: {script} MEMBER
    """
    try:
        unix.get_user("{}-adm".format(member.crsid))
    except KeyError:
        error("Member {!r} does not have an administrative account".format(member.crsid), exit=2)
    confirm("Cancel {}'s administrative account?".format(member.name))
    confirm()
    membership.cancel_sysadmin(sess, member)
    error("Manual action required: check for accounts on other systems!")

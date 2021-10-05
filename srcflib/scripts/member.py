"""
Scripts to manage users.
"""

from sqlalchemy.orm import Session

from srcf.database.schema import Member

from .utils import confirm, entrypoint
from ..tasks import membership


@entrypoint
def passwd(member: Member):
    """
    Reset a user's SRCF password.

    Usage: {script} MEMBER
    """
    confirm("Reset {}'s password?".format(member.crsid))
    membership.reset_password(member)
    print("Password changed")


@entrypoint
def cancel(sess: Session, member: Member):
    """
    Cancel a user account.

    Usage: {script} MEMBER
    """
    confirm("Cancel {}?".format(member.name))
    membership.cancel_member(sess, member)

import sys
import os
import pwd

from .database import Member, Session

def get_current_user(sess=None):
    """
    Return a srcf.database.Member for the current user.

    sess may be a srcf.database.Session, if you have one.
    """

    try:
        pw_name = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        pw_name = None

    attempts = {
        pw_name,
        os.environ.get("SUDO_USER"),
        os.environ.get("LOGNAME")
    } - {None, 'root'}

    if len(attempts) == 0:
        raise EnvironmentError("Unable to detect CRSID")
    elif len(attempts) != 1:
        raise EnvironmentError("Multiple CRSIDS? {0}"
                .format(', '.join(attempts)))

    crsid = list(attempts)[0]
    if crsid.endswith("-adm"):
        crsid = crsid[:-4]

    if sess is None:
        sess = Session()

    return sess.query(Member).get(crsid)